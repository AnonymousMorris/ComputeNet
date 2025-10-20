from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

from client import Client, ClientConfig


@dataclass(slots=True)
class TestSettings:
    server_url: str
    peer_a: str
    peer_b: str
    message: str = "ping"
    response: str = "pong"
    timeout: float = 10.0
    spawn_delay: float = 0.5
    post_spawn_delay: float = 0.5


async def _exercise_data_channel(initiator: Client, responder: Client, settings: TestSettings) -> None:
    logging.info("Requesting connection from %s to %s", initiator.peer_id, responder.peer_id)
    init_peer = await initiator.connect_to_peer(responder.peer_id, settings.timeout)
    logging.info("Initiator channel to %s open", responder.peer_id)

    resp_peer = await responder.wait_for_peer(initiator.peer_id, settings.timeout)
    await resp_peer.wait_channel_ready(settings.timeout, label="responder")
    logging.info("Responder channel to %s open", initiator.peer_id)

    logging.info("Sending test message '%s'", settings.message)
    await init_peer.send_with_retry(settings.message, settings.timeout, label="initiator")
    received = await asyncio.wait_for(resp_peer.recv(), settings.timeout)
    logging.info("Responder received '%s'", received)
    if received != settings.message:
        raise AssertionError(f"Responder got unexpected payload: {received!r}")

    logging.info("Sending response '%s'", settings.response)
    await resp_peer.send_with_retry(settings.response, settings.timeout, label="responder")
    echoed = await asyncio.wait_for(init_peer.recv(), settings.timeout)
    logging.info("Initiator received '%s'", echoed)
    if echoed != settings.response:
        raise AssertionError(f"Initiator got unexpected payload: {echoed!r}")

    logging.info("WebRTC round-trip succeeded between %s and %s", initiator.peer_id, responder.peer_id)


async def _run(settings: TestSettings) -> None:
    meta_a = {"role": "initiator", "label": "test-client-a"}
    meta_b = {"role": "responder", "label": "test-client-b"}

    config_a = ClientConfig(server_url=settings.server_url, peer_id=settings.peer_a, metadata=meta_a)
    config_b = ClientConfig(server_url=settings.server_url, peer_id=settings.peer_b, metadata=meta_b)

    async with Client(config_a) as client_a:
        logging.info("First client registered as %s", client_a.peer_id)
        await asyncio.sleep(settings.spawn_delay)
        async with Client(config_b) as client_b:
            logging.info("Second client registered as %s", client_b.peer_id)
            await asyncio.sleep(settings.post_spawn_delay)
            await _exercise_data_channel(client_a, client_b, settings)

    logging.info("Test clients closed cleanly")


def _build_settings() -> TestSettings:
    return TestSettings(
        server_url=ClientConfig().server_url,
        peer_a=f"test-a-{uuid4().hex[:6]}",
        peer_b=f"test-b-{uuid4().hex[:6]}",
    )


def main() -> None:  # pragma: no cover - simple harness
    settings = _build_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        logging.info("Test interrupted by user")


if __name__ == "__main__":
    main()
