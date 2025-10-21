from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from message import (
    Message,
    MessageType,
    build_connection_answer,
    build_connection_offer,
    build_connection_reject,
    build_peer_list_request,
    build_register,
    extract_error,
    extract_forwarded_rejection,
    extract_forwarded_signal,
    extract_peer_list,
    extract_register_ack,
)

from peer import Peer
from webrtc_connection import (
    create_initiator_connection,
    create_offer,
    create_responder_connection,
    handle_offer_and_create_answer,
    set_remote_answer,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ClientConfig:
    server_url: str = "ws://localhost:8765/ws"
    connect_timeout: float = 10.0
    peer_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Client:
    def __init__(self, config: ClientConfig | None = None) -> None:
        self.config = config or ClientConfig()
        self.peer_id = self.config.peer_id or f"peer-{uuid4().hex[:8]}"
        self.directory: dict[str, dict[str, Any]] = {}
        self.peers: dict[str, Peer] = {}

        self._connection: ClientConnection | None = None
        self._listener: asyncio.Task[None] | None = None
        self._register_future: asyncio.Future[dict[str, dict[str, Any]]] | None = None
        self._pending_peer_list: asyncio.Future[dict[str, dict[str, Any]]] | None = None
        self._pending_signals: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def __aenter__(self) -> "Client":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._connection is not None:
            return

        logger.info("Connecting to signaling hub at %s", self.config.server_url)
        connection = await websockets.connect(
            self.config.server_url,
            open_timeout=self.config.connect_timeout,
        )
        self._connection = connection
        loop = asyncio.get_running_loop()
        self._register_future = loop.create_future()
        self._listener = loop.create_task(self._listen_loop())

        register_message = build_register(self.peer_id, self.config.metadata)
        await self._send(register_message)
        await self._register_future

    async def close(self) -> None:
        if self._connection is None:
            return

        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None

        if self._connection is not None:
            await self._connection.close()
            self._connection = None

        for peer in list(self.peers.values()):
            with contextlib.suppress(Exception):
                if peer.rtc is not None:
                    await peer.rtc.close()
            self.peers.pop(peer.peer_id, None)

        logger.info("Client shut down")

    async def request_peer_list(self) -> dict[str, dict[str, Any]]:
        await self.connect()
        if self._connection is None:
            raise RuntimeError("Client is not connected")

        loop = asyncio.get_running_loop()
        if self._pending_peer_list is not None and not self._pending_peer_list.done():
            raise RuntimeError("Peer list request already in flight")

        self._pending_peer_list = loop.create_future()
        await self._send(build_peer_list_request())
        peers = await self._pending_peer_list
        self.directory = peers
        return peers

    async def wait_for_peer(
        self,
        peer_id: str,
        timeout: float,
        *,
        poll_interval: float = 0.05,
    ) -> Peer:
        """Poll until the given peer appears in the local peer cache."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            peer = self.peers.get(peer_id)
            if peer is not None:
                return peer
            if loop.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for peer {peer_id}")
            await asyncio.sleep(poll_interval)

    async def request_conn_to_peer(self, target_id: str):
        await self.connect()
        if self._connection is None:
            raise RuntimeError("Client is not connected")

        if target_id == self.peer_id:
            raise ValueError("Cannot connect to self")

        if target_id in self._pending_signals:
            raise RuntimeError(f"Connection attempt to {target_id} already pending")

        if target_id in self.peers:
            raise RuntimeError(f"Peer connection with {target_id} already exists")

        peer = Peer(peer_id=target_id, initiator=True)
        pc = create_initiator_connection(target_id, peer)
        peer.rtc = pc
        self.peers[target_id] = peer

        description = await create_offer(pc)
        loop = asyncio.get_running_loop()
        answer_future = loop.create_future()
        self._pending_signals[target_id] = answer_future
        await self._send(build_connection_offer(target_id, description))

        try:
            answer_description = await answer_future
        except Exception:
            await pc.close()
            self.peers.pop(target_id, None)
            raise

        await set_remote_answer(pc, answer_description)
        return pc

    async def connect_to_peer(
        self,
        target_id: str,
        timeout: float,
        *,
        poll_interval: float = 0.05,
    ) -> Peer:
        """High-level helper to negotiate and wait for a ready RTC data channel."""

        existing = self.peers.get(target_id)
        if existing is not None:
            channel_state = getattr(existing.channel, "readyState", None)
            connection_state = (
                getattr(existing.rtc, "connectionState", None)
                if existing.rtc is not None
                else None
            )

            if channel_state == "closed" or connection_state in {"failed", "closed"}:
                logger.debug(
                    "Dropping stale connection to %s (channel=%s, state=%s)",
                    target_id,
                    channel_state,
                    connection_state,
                )
                with contextlib.suppress(Exception):
                    if existing.rtc is not None:
                        await existing.rtc.close()
                self.peers.pop(target_id, None)
            else:
                try:
                    await existing.connect(timeout)
                except TimeoutError:
                    logger.info(
                        "Timed out waiting for existing connection to %s; rebuilding",
                        target_id,
                    )
                    with contextlib.suppress(Exception):
                        if existing.rtc is not None:
                            await existing.rtc.close()
                    self.peers.pop(target_id, None)
                else:
                    logger.debug(
                        "Reusing established connection to %s (initiator=%s)",
                        target_id,
                        existing.initiator,
                    )
                    return existing

        await self.request_conn_to_peer(target_id)
        peer = await self.wait_for_peer(target_id, timeout, poll_interval=poll_interval)
        await peer.connect(timeout)
        return peer

    async def _send(self, message: Message) -> None:
        if self._connection is None:
            raise RuntimeError("Client is not connected")
        logger.debug("Sending message type %s", message.type.value)
        await self._connection.send(message.to_json())

    async def _listen_loop(self) -> None:
        assert self._connection is not None
        connection = self._connection
        try:
            while True:
                payload = await connection.recv()
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                logger.debug("Received raw payload: %s", payload)
                try:
                    message = Message.from_json(payload)
                except Exception as exc:
                    logger.error("Dropping malformed message: %s", exc)
                    continue
                await self._dispatch(message)
        except ConnectionClosed:
            logger.info("Server connection closed")
        finally:
            self._handle_disconnect()

    async def _dispatch(self, message: Message) -> None:
        logger.debug("Dispatching message type %s", message.type.value)

        if message.type is MessageType.REGISTER_ACK:
            self._handle_register_ack(message)
            return

        if message.type is MessageType.PEER_LIST_RESPONSE:
            self._handle_peer_list(message)
            return

        if message.type in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
            await self._handle_signaling(message)
            return

        if message.type is MessageType.CONNECTION_REJECT:
            self._handle_rejection(message)
            return

        if message.type is MessageType.ERROR:
            reason = extract_error(message)
            if self._register_future and not self._register_future.done():
                self._register_future.set_exception(RuntimeError(reason))
            if self._pending_peer_list and not self._pending_peer_list.done():
                self._pending_peer_list.set_exception(RuntimeError(reason))
                self._pending_peer_list = None
            for peer_id, future in list(self._pending_signals.items()):
                if not future.done():
                    future.set_exception(RuntimeError(reason))
                self._pending_signals.pop(peer_id, None)
                peer = self.peers.pop(peer_id, None)
                if peer is not None and peer.rtc is not None:
                    asyncio.create_task(peer.rtc.close())
            logger.error("Server error: %s", reason)
            return

        logger.warning("Unhandled message from server: %s", message.type.value)

    def _handle_register_ack(self, message: Message) -> None:
        if self._register_future is None or self._register_future.done():
            return
        peer_id, existing_peers = extract_register_ack(message)
        self.peer_id = peer_id
        directory = {pid: data for pid, data in existing_peers.items() if pid != peer_id}
        self.directory = directory
        self._register_future.set_result(directory)
        logger.info("Registered with peer id %s", peer_id)

    def _handle_peer_list(self, message: Message) -> None:
        peers = extract_peer_list(message)
        directory = {pid: data for pid, data in peers.items() if pid != self.peer_id}
        self.directory = directory
        if self._pending_peer_list is not None and not self._pending_peer_list.done():
            self._pending_peer_list.set_result(directory)
            self._pending_peer_list = None

    async def _handle_signaling(self, message: Message) -> None:
        if message.type is MessageType.CONNECTION_OFFER:
            await self._handle_incoming_offer(message)
            return

        self._handle_incoming_answer(message)

    async def _handle_incoming_offer(self, message: Message) -> None:
        source_id, description = extract_forwarded_signal(message)
        logger.info("Received offer from %s", source_id)

        existing = self.peers.pop(source_id, None)
        if existing is not None:
            with contextlib.suppress(Exception):
                if existing.rtc is not None:
                    await existing.rtc.close()

        peer = Peer(peer_id=source_id)
        pc = create_responder_connection(source_id, peer)
        peer.rtc = pc
        self.peers[source_id] = peer

        try:
            answer_description = await handle_offer_and_create_answer(pc, description)
        except Exception as exc:
            logger.exception("Failed to process offer from %s", source_id)
            self.peers.pop(source_id, None)
            await pc.close()
            await self._send(build_connection_reject(source_id, str(exc)))
            return

        response = build_connection_answer(source_id, answer_description)
        await self._send(response)

    def _handle_incoming_answer(self, message: Message) -> None:
        source_id, description = extract_forwarded_signal(message)
        future = self._pending_signals.pop(source_id, None)
        if future is None or future.done():
            logger.warning("No pending offer for answer from %s", source_id)
            return
        future.set_result(description)

    def _handle_rejection(self, message: Message) -> None:
        source_id, reason = extract_forwarded_rejection(message)
        future = self._pending_signals.pop(source_id, None)
        if future and not future.done():
            future.set_exception(RuntimeError(reason))
        peer = self.peers.pop(source_id, None)
        if peer is not None and peer.rtc is not None:
            asyncio.create_task(peer.rtc.close())
        logger.warning("Connection rejected by %s: %s", source_id, reason)

    def _handle_disconnect(self) -> None:
        error = RuntimeError("Connection to server lost")
        if self._register_future and not self._register_future.done():
            self._register_future.set_exception(error)
        if self._pending_peer_list and not self._pending_peer_list.done():
            self._pending_peer_list.set_exception(error)
            self._pending_peer_list = None
        for future in self._pending_signals.values():
            if not future.done():
                future.set_exception(error)
        self._pending_signals.clear()
        self._connection = None
        self._listener = None
        logger.info("Listener stopped")
