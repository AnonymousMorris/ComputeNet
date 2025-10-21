from __future__ import annotations

import logging
from typing import Callable

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcdatachannel import RTCDataChannel

from peer import Peer

logger = logging.getLogger(__name__)


def create_initiator_connection(
    target_id: str,
    peer: Peer,
    on_state_change: Callable[[str], None] | None = None,
) -> RTCPeerConnection:
    """Create and configure an RTCPeerConnection as the initiator.

    Args:
        target_id: The peer ID we're connecting to
        peer: The Peer object to attach the channel to
        on_state_change: Optional callback for connection state changes

    Returns:
        Configured RTCPeerConnection
    """
    pc = RTCPeerConnection()

    @pc.on("connectionstatechange")  # type: ignore[misc]
    def _on_state_change() -> None:
        state = pc.connectionState
        if state == "connected":
            peer.mark_connected()
        elif state in {"failed", "closed"}:
            peer._ready.clear()
        logger.info("Connection state with %s: %s", target_id, state)
        if on_state_change:
            on_state_change(state)

    # Create data channel (initiator creates the channel)
    channel = pc.createDataChannel("compute-net")
    peer.attach_channel(channel)

    return pc


def create_responder_connection(
    source_id: str,
    peer: Peer,
    on_state_change: Callable[[str], None] | None = None,
) -> RTCPeerConnection:
    """Create and configure an RTCPeerConnection as the responder.

    Args:
        source_id: The peer ID that initiated the connection
        peer: The Peer object to attach the channel to
        on_state_change: Optional callback for connection state changes

    Returns:
        Configured RTCPeerConnection
    """
    pc = RTCPeerConnection()

    @pc.on("connectionstatechange")  # type: ignore[misc]
    def _on_state_change() -> None:
        state = pc.connectionState
        if state == "connected":
            peer.mark_connected()
        elif state in {"failed", "closed"}:
            peer._ready.clear()
        logger.info("Connection state with %s: %s", source_id, state)
        if on_state_change:
            on_state_change(state)

    @pc.on("datachannel")  # type: ignore[misc]
    def _on_datachannel(channel: RTCDataChannel) -> None:
        peer.attach_channel(channel)

    return pc


async def create_offer(pc: RTCPeerConnection) -> dict[str, str]:
    """Create and set a local offer description.

    Args:
        pc: The RTCPeerConnection to create the offer for

    Returns:
        Dictionary with 'sdp' and 'type' keys
    """
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    return {"sdp": offer.sdp, "type": offer.type}


async def handle_offer_and_create_answer(
    pc: RTCPeerConnection,
    offer_description: dict[str, str],
) -> dict[str, str]:
    """Process an incoming offer and create an answer.

    Args:
        pc: The RTCPeerConnection to use
        offer_description: Dictionary with 'sdp' and 'type' from the offer

    Returns:
        Dictionary with 'sdp' and 'type' keys for the answer
    """
    offer = RTCSessionDescription(**offer_description)
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


async def set_remote_answer(
    pc: RTCPeerConnection,
    answer_description: dict[str, str],
) -> None:
    """Set the remote answer description on the peer connection.

    Args:
        pc: The RTCPeerConnection to update
        answer_description: Dictionary with 'sdp' and 'type' from the answer
    """
    answer = RTCSessionDescription(**answer_description)
    await pc.setRemoteDescription(answer)
