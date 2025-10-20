from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

from message import (
    Message,
    MessageType,
    build_connection_answer_forward,
    build_connection_offer_forward,
    build_connection_reject_forward,
    build_error,
    build_peer_list_response,
    build_register_ack,
    extract_rejection,
    extract_registration,
    extract_signal,
)

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    yield


app = FastAPI(lifespan=lifespan)


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    websocket_path: str = "/ws"


@dataclass(slots=True)
class ClientRecord:
    peer_id: str
    socket: WebSocket
    host: str
    port: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"address": self.host, "port": self.port}
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


class SignalingHub:
    """State container for connected peers and helper utilities."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or ServerConfig()
        self._clients: Dict[str, ClientRecord] = {}
        self._lock = asyncio.Lock()

    async def register(self, peer_id: str, socket: WebSocket, metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
        async with self._lock:
            if peer_id in self._clients:
                raise ValueError(f"peer id '{peer_id}' already registered")
            client = socket.client
            host = client.host if client and client.host else ""
            port = client.port if client and client.port else 0
            record = ClientRecord(
                peer_id=peer_id,
                socket=socket,
                host=host,
                port=port,
                metadata=dict(metadata),
            )
            self._clients[peer_id] = record
            snapshot = self._snapshot_locked()
            others = [rec for pid, rec in self._clients.items() if pid != peer_id]
        await self._broadcast_peer_snapshot(snapshot, recipients=others)
        return snapshot

    async def unregister(self, peer_id: str) -> None:
        async with self._lock:
            removed = self._clients.pop(peer_id, None)
            snapshot = self._snapshot_locked()
            recipients = list(self._clients.values())
        if removed is not None:
            await self._broadcast_peer_snapshot(snapshot, recipients=recipients)

    async def request_peer_list(self, socket: WebSocket) -> None:
        snapshot = await self.peer_snapshot()
        await self._safe_send(socket, build_peer_list_response(snapshot))

    async def request_conn_to_peer(self, sender_id: str, message: Message) -> None:
        target_id, description = extract_signal(message)
        target = await self._get_client(target_id)
        if target is None:
            logger.warning("Peer %s requested unknown target %s", sender_id, target_id)
            await self._fail_signal_request(sender_id, target_id, f"Target peer '{target_id}' is not connected")
            return

        forwarder = (
            build_connection_offer_forward(sender_id, description)
            if message.type is MessageType.CONNECTION_OFFER
            else build_connection_answer_forward(sender_id, description)
        )
        await self._safe_send(target.socket, forwarder)

    async def forward_rejection(self, sender_id: str, message: Message) -> None:
        target_id, reason = extract_rejection(message)
        target = await self._get_client(target_id)
        if target is None:
            await self._send_error(sender_id, f"Target peer '{target_id}' is not connected")
            return
        payload = build_connection_reject_forward(sender_id, reason)
        await self._safe_send(target.socket, payload)

    async def peer_snapshot(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return self._snapshot_locked()

    async def _get_client(self, peer_id: str) -> ClientRecord | None:
        async with self._lock:
            return self._clients.get(peer_id)

    async def _broadcast_peer_snapshot(
        self,
        snapshot: dict[str, dict[str, Any]],
        *,
        recipients: list[ClientRecord] | None = None,
    ) -> None:
        recipients = recipients if recipients is not None else await self._collect_clients()
        if not recipients:
            return

        message = build_peer_list_response(snapshot)
        await asyncio.gather(*(self._safe_send(client.socket, message) for client in recipients), return_exceptions=True)

    async def _collect_clients(self) -> list[ClientRecord]:
        async with self._lock:
            return list(self._clients.values())

    async def _send_error(self, peer_id: str, reason: str) -> None:
        target = await self._get_client(peer_id)
        if target is None:
            return
        await self._safe_send(target.socket, build_error(reason))

    async def _fail_signal_request(self, sender_id: str, target_id: str, reason: str) -> None:
        sender = await self._get_client(sender_id)
        if sender is None:
            return
        rejection = build_connection_reject_forward(target_id, reason)
        await self._safe_send(sender.socket, rejection)

    async def _safe_send(self, socket: WebSocket, message: Message) -> None:
        try:
            await socket.send_text(message.to_json())
        except Exception:
            logger.exception("Failed to send message to %s", socket.client)

    def _snapshot_locked(self) -> dict[str, dict[str, Any]]:
        return {peer_id: record.payload() for peer_id, record in self._clients.items()}


hub = SignalingHub()
@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "ComputeNet signaling hub active"}


@app.websocket(hub.config.websocket_path)
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    peer_id: str | None = None
    try:
        while True:
            try:
                raw_message = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("Websocket disconnected: %s", peer_id or "unregistered")
                break

            try:
                message = Message.from_json(raw_message)
            except Exception as exc:
                logger.exception("Malformed message received")
                await websocket.send_text(build_error(f"Invalid message payload: {exc}").to_json())
                continue

            if peer_id is None:
                if message.type is not MessageType.REGISTER:
                    await websocket.send_text(build_error("First message must be register").to_json())
                    await websocket.close(code=1008)
                    return

                try:
                    requested_id, metadata = extract_registration(message)
                    snapshot = await hub.register(requested_id, websocket, metadata)
                except ValueError as exc:
                    await websocket.send_text(build_error(str(exc)).to_json())
                    await websocket.close(code=1008)
                    return

                peer_id = requested_id
                logger.info("Peer registered: %s", peer_id)
                ack = build_register_ack(peer_id, snapshot)
                await websocket.send_text(ack.to_json())
                continue

            if message.type is MessageType.PEER_LIST_REQUEST:
                await hub.request_peer_list(websocket)
                continue

            if message.type in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
                await hub.request_conn_to_peer(peer_id, message)
                continue

            if message.type is MessageType.CONNECTION_REJECT:
                await hub.forward_rejection(peer_id, message)
                continue

            logger.warning("Unhandled message type from %s: %s", peer_id, message.type.value)
            await websocket.send_text(build_error(f"Unhandled message type: {message.type.value}").to_json())
    finally:
        if peer_id is not None:
            await hub.unregister(peer_id)
            logger.info("Peer disconnected: %s", peer_id)
        with contextlib.suppress(RuntimeError):
            await websocket.close()


def main() -> None:  # pragma: no cover - thin runtime shim
    config = hub.config
    host = os.getenv("COMPUTENET_HOST", config.host)
    port_env = os.getenv("COMPUTENET_PORT")
    port = config.port
    if port_env:
        try:
            port = int(port_env)
        except ValueError:
            logging.warning("Invalid COMPUTENET_PORT '%s'; falling back to %s", port_env, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
