from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class MessageType(str, Enum):
    """Enumeration of the control messages exchanged between clients and the signaling hub."""

    REGISTER = "register"
    REGISTER_ACK = "register_ack"
    PEER_LIST_REQUEST = "peer_list_request"
    PEER_LIST_RESPONSE = "peer_list_response"
    CONNECTION_OFFER = "connection_offer"
    CONNECTION_ANSWER = "connection_answer"
    CONNECTION_REJECT = "connection_reject"
    ERROR = "error"


@dataclass(slots=True)
class Message:
    """Representation of a structured message sent over the websocket."""

    type: MessageType
    data: dict[str, Any] | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {"type": self.type.value}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload)

    def require_data(self) -> dict[str, Any]:
        if self.data is None:
            raise ValueError(f"Message '{self.type.value}' requires a data payload")
        return self.data

    @classmethod
    def from_json(cls, payload: str) -> "Message":
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("Message payload must be a JSON object")

        raw_type = raw.get("type")
        if not isinstance(raw_type, str):
            raise ValueError("Missing message type")

        try:
            message_type = MessageType(raw_type)
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"Unsupported message type: {raw_type}") from exc

        raw_data = raw.get("data")
        if raw_data is not None and not isinstance(raw_data, dict):
            raise ValueError("Message data must be a JSON object when present")

        return cls(type=message_type, data=raw_data)


def build_register(peer_id: str, metadata: Mapping[str, Any] | None = None) -> Message:
    if not isinstance(peer_id, str) or not peer_id:
        raise ValueError("peer_id must be a non-empty string")
    payload: dict[str, Any] = {"peer_id": peer_id}
    if metadata is not None:
        payload["metadata"] = dict(metadata)
    return Message(type=MessageType.REGISTER, data=payload)


def build_register_ack(peer_id: str, peers: Mapping[str, Mapping[str, Any]]) -> Message:
    if not isinstance(peer_id, str) or not peer_id:
        raise ValueError("peer_id must be a non-empty string")
    return Message(
        type=MessageType.REGISTER_ACK,
        data={
            "peer_id": peer_id,
            "peers": {pid: dict(data) for pid, data in peers.items()},
        },
    )


def build_peer_list_request() -> Message:
    return Message(type=MessageType.PEER_LIST_REQUEST)


def build_peer_list_response(peers: Mapping[str, Mapping[str, Any]]) -> Message:
    return Message(
        type=MessageType.PEER_LIST_RESPONSE,
        data={"peers": {peer_id: dict(data) for peer_id, data in peers.items()}},
    )


def build_connection_offer(target_id: str, description: Mapping[str, Any]) -> Message:
    return _build_signal(MessageType.CONNECTION_OFFER, target_id, description)


def build_connection_answer(target_id: str, description: Mapping[str, Any]) -> Message:
    return _build_signal(MessageType.CONNECTION_ANSWER, target_id, description)


def build_connection_offer_forward(source_id: str, description: Mapping[str, Any]) -> Message:
    return _build_forward(MessageType.CONNECTION_OFFER, source_id, description)


def build_connection_answer_forward(source_id: str, description: Mapping[str, Any]) -> Message:
    return _build_forward(MessageType.CONNECTION_ANSWER, source_id, description)


def build_connection_reject(target_id: str, reason: str) -> Message:
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("target_id must be a non-empty string")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return Message(type=MessageType.CONNECTION_REJECT, data={"target_id": target_id, "reason": reason})


def build_connection_reject_forward(source_id: str, reason: str) -> Message:
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return Message(
        type=MessageType.CONNECTION_REJECT,
        data={"source_id": source_id, "reason": reason},
    )


def build_error(reason: str) -> Message:
    if not isinstance(reason, str) or not reason:
        raise ValueError("Error messages require a non-empty reason text")
    return Message(type=MessageType.ERROR, data={"error": reason})


def extract_error(message: Message) -> str:
    if message.type is not MessageType.ERROR:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    error = data.get("error")
    if not isinstance(error, str):
        raise ValueError("error payload must contain an 'error' string")
    return error


def extract_peer_list(message: Message) -> dict[str, Any]:
    if message.type is not MessageType.PEER_LIST_RESPONSE:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    peers = data.get("peers")
    if not isinstance(peers, dict):
        raise ValueError("peer_list_response payload must contain a 'peers' object")
    sanitized: dict[str, dict[str, Any]] = {}
    for peer_id, info in peers.items():
        if not isinstance(peer_id, str):
            continue
        if isinstance(info, dict):
            sanitized[peer_id] = dict(info)
    return sanitized


def extract_register_ack(message: Message) -> tuple[str, dict[str, Any]]:
    if message.type is not MessageType.REGISTER_ACK:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    peer_id = data.get("peer_id")
    peers = data.get("peers")
    if not isinstance(peer_id, str) or not peer_id:
        raise ValueError("register_ack payload missing 'peer_id'")
    if not isinstance(peers, dict):
        raise ValueError("register_ack payload must include a 'peers' object")
    sanitized: dict[str, dict[str, Any]] = {}
    for other_id, info in peers.items():
        if not isinstance(other_id, str):
            continue
        if isinstance(info, dict):
            sanitized[other_id] = dict(info)
    return peer_id, sanitized


def extract_registration(message: Message) -> tuple[str, dict[str, Any]]:
    if message.type is not MessageType.REGISTER:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    peer_id = data.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id:
        raise ValueError("register payload requires a non-empty 'peer_id'")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("register metadata must be an object when provided")
    return peer_id, metadata or {}


def extract_signal(message: Message) -> tuple[str, dict[str, Any]]:
    if message.type not in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    target_id = data.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("signaling payload requires a non-empty 'target_id'")
    description = data.get("description")
    if not isinstance(description, dict):
        raise ValueError("signaling payload requires a 'description' object")
    return target_id, dict(description)


def extract_forwarded_signal(message: Message) -> tuple[str, dict[str, Any]]:
    if message.type not in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    source_id = data.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("forwarded signaling payload requires a non-empty 'source_id'")
    description = data.get("description")
    if not isinstance(description, dict):
        raise ValueError("forwarded signaling payload requires a 'description' object")
    return source_id, dict(description)


def extract_rejection(message: Message) -> tuple[str, str]:
    if message.type is not MessageType.CONNECTION_REJECT:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    target_id = data.get("target_id")
    reason = data.get("reason", "")
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("connection_reject payload requires a non-empty 'target_id'")
    if not isinstance(reason, str):
        raise ValueError("connection_reject reason must be a string")
    return target_id, reason


def extract_forwarded_rejection(message: Message) -> tuple[str, str]:
    if message.type is not MessageType.CONNECTION_REJECT:
        raise ValueError(f"Unexpected message type: {message.type.value}")
    data = message.require_data()
    source_id = data.get("source_id")
    reason = data.get("reason", "")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("forwarded rejection requires a non-empty 'source_id'")
    if not isinstance(reason, str):
        raise ValueError("forwarded rejection reason must be a string")
    return source_id, reason


def _build_signal(message_type: MessageType, target_id: str, description: Mapping[str, Any]) -> Message:
    if message_type not in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
        raise ValueError("Invalid message type for _build_signal")
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("target_id must be a non-empty string")
    if not isinstance(description, Mapping):
        raise ValueError("description must be a mapping object")
    return Message(
        type=message_type,
        data={"target_id": target_id, "description": dict(description)},
    )


def _build_forward(message_type: MessageType, source_id: str, description: Mapping[str, Any]) -> Message:
    if message_type not in {MessageType.CONNECTION_OFFER, MessageType.CONNECTION_ANSWER}:
        raise ValueError("Invalid message type for _build_forward")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(description, Mapping):
        raise ValueError("description must be a mapping object")
    return Message(
        type=message_type,
        data={"source_id": source_id, "description": dict(description)},
    )
