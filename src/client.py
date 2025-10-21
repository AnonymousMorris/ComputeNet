from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aiortc import RTCPeerConnection, RTCSessionDescription
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

    async def request_conn_to_peer(self, target_id: str) -> RTCPeerConnection:
        await self.connect()
        if self._connection is None:
            raise RuntimeError("Client is not connected")

        if target_id == self.peer_id:
            raise ValueError("Cannot connect to self")

        if target_id in self._pending_signals:
            raise RuntimeError(f"Connection attempt to {target_id} already pending")

        if target_id in self.peers:
            raise RuntimeError(f"Peer connection with {target_id} already exists")

        pc = RTCPeerConnection()
        peer = Peer(peer_id=target_id, rtc=pc, initiator=True)
        self.peers[target_id] = peer

        channel = pc.createDataChannel("compute-net")
        peer.attach_channel(channel)

        @pc.on("connectionstatechange")  # type: ignore[misc]
        def _on_state_change() -> None:
            state = pc.connectionState
            if state == "connected":
                peer.mark_connected()
            elif state in {"failed", "closed"}:
                peer.connected = False
            logger.info("Connection state with %s: %s", target_id, state)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        description = {"sdp": offer.sdp, "type": offer.type}
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

        answer = RTCSessionDescription(**answer_description)
        await pc.setRemoteDescription(answer)
        return pc

    async def connect_to_peer(
        self,
        target_id: str,
        timeout: float,
        *,
        poll_interval: float = 0.05,
    ) -> Peer:
        """High-level helper to negotiate and wait for a ready RTC data channel."""

        await self.request_conn_to_peer(target_id)
        peer = await self.wait_for_peer(target_id, timeout, poll_interval=poll_interval)
        await peer.wait_channel_ready(
            timeout,
            label=f"initiator->{target_id}",
            poll_interval=poll_interval,
        )
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
                if peer is not None:
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
                await existing.rtc.close()

        pc = RTCPeerConnection()
        peer = Peer(peer_id=source_id, rtc=pc)
        self.peers[source_id] = peer

        @pc.on("connectionstatechange")  # type: ignore[misc]
        def _on_state_change() -> None:
            state = pc.connectionState
            if state == "connected":
                peer.mark_connected()
            elif state in {"failed", "closed"}:
                peer.connected = False
            logger.info("Connection state with %s: %s", source_id, state)

        @pc.on("datachannel")  # type: ignore[misc]
        def _on_datachannel(channel: RTCDataChannel) -> None:
            peer.attach_channel(channel)

        try:
            offer = RTCSessionDescription(**description)
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as exc:
            logger.exception("Failed to process offer from %s", source_id)
            self.peers.pop(source_id, None)
            await pc.close()
            await self._send(build_connection_reject(source_id, str(exc)))
            return

        response = build_connection_answer(
            source_id,
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        )
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
        if peer is not None:
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


if __name__ == "__main__":  # pragma: no cover - convenience demo
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    async def _demo() -> None:
        server_url = os.getenv("COMPUTENET_SERVER_URL", ClientConfig().server_url)
        config = ClientConfig(server_url=server_url)
        async with Client(config) as client:
            try:
                peers = await client.request_peer_list()
            except Exception:
                logger.exception("Failed to fetch peer list in demo")
                return

            if not peers:
                logger.info("No peers currently registered")
                return

            target_id = next(iter(peers))
            logger.info("Attempting demo connection to %s", target_id)
            try:
                peer = await client.connect_to_peer(
                    target_id,
                    timeout=config.connect_timeout,
                )
            except Exception:
                logger.exception("Failed to establish demo connection with %s", target_id)
                return

            try:
                await peer.send_with_retry("ping", timeout=5, label="demo")
                logger.info("Demo ping sent to %s", target_id)
            except Exception:
                logger.exception("Demo ping failed for %s", target_id)

    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        logger.info("Demo interrupted by user")
