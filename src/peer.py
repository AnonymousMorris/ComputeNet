from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from aiortc import RTCDataChannel, RTCPeerConnection
from aiortc.exceptions import InvalidStateError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Peer:
    peer_id: str
    rtc: RTCPeerConnection | None = None
    channel: RTCDataChannel | None = None
    initiator: bool = False
    _incoming: asyncio.Queue[Any] = field(default_factory=asyncio.Queue, init=False, repr=False)
    _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def attach_channel(self, channel: RTCDataChannel) -> None:
        self.channel = channel

        @channel.on("open")  # type: ignore[misc]
        def _on_open() -> None:
            self.mark_connected()

        @channel.on("close")  # type: ignore[misc]
        def _on_close() -> None:
            self._ready.clear()

        @channel.on("message")  # type: ignore[misc]
        def _on_message(message: Any) -> None:
            self._incoming.put_nowait(message)

    def mark_connected(self) -> None:
        self._ready.set()

    async def connect(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._ready.wait()
            return
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def send(self, data: Any) -> None:
        if self.channel is None:
            raise RuntimeError("Data channel not negotiated yet")
        self.channel.send(data)

    async def recv(self) -> Any:
        return await self._incoming.get()


    async def send_with_retry(
        self,
        data: Any,
        timeout: float,
        *,
        label: str | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        """Attempt to send data, retrying until the channel is ready or times out."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            channel = self.channel
            if channel is None:
                raise RuntimeError(f"{label or 'Data'} channel not established")

            state = getattr(channel, "readyState", None)
            if state != "open":
                if loop.time() >= deadline:
                    raise TimeoutError(
                        f"{label or 'Data'} channel never became open (state={state})"
                    )
                logger.debug(
                    "%s channel state %s; retrying send",
                    label or self.peer_id,
                    state,
                )
                await asyncio.sleep(poll_interval)
                continue

            try:
                await self.send(data)
                return
            except InvalidStateError as exc:  # pragma: no cover - transient race guard
                if loop.time() >= deadline:
                    raise exc
                logger.debug("%s send hit InvalidStateError; retrying", label or self.peer_id)
                await asyncio.sleep(poll_interval)
