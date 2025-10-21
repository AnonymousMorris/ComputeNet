from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from CEE import JobExecutionError, WasmExecutor
from client import Client, ClientConfig, Peer
from compiler import Compiler
@dataclass(slots=True)
class ComputeConfig:
    """Defaults for running a compute node."""

    peer_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: {"role": "compute"})
    entry_point: str = "_start"
    poll_interval: float = 0.2
    recv_timeout: float = 1.0
    send_timeout: float = 5.0


@dataclass(slots=True)
class RequestConfig:
    """Defaults for requesting compute work."""

    entry_point: str = "_start"
    language: str = "c"
    timeout: float = 60.0


@dataclass(slots=True)
class AppConfig:
    """Top-level application defaults for the simplified CLI."""

    server_url: str = ClientConfig().server_url
    verbose: bool = False
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    request: RequestConfig = field(default_factory=RequestConfig)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


class ComputeNode:
    """Accepts incoming job submissions over established peer channels."""

    def __init__(
        self,
        client: Client,
        *,
        entry_point: str,
        poll_interval: float = 0.2,
        recv_timeout: float = 1.0,
        send_timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._entry_point = entry_point
        self._poll_interval = poll_interval
        self._recv_timeout = recv_timeout
        self._send_timeout = send_timeout
        self._executor = WasmExecutor()
        self._peer_tasks: dict[str, asyncio.Task[None]] = {}
        self._logger = logging.getLogger("ComputeNode")

    async def run(self) -> None:
        self._logger.info("Compute node ready; waiting for peer submissions")
        try:
            while True:
                await self._refresh_peer_handlers()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise
        finally:
            await self._shutdown_handlers()

    async def _refresh_peer_handlers(self) -> None:
        # Reap finished handlers first
        for peer_id, task in list(self._peer_tasks.items()):
            if not task.done():
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive logging
                self._logger.warning("Peer handler %s terminated with error: %s", peer_id, exc)
            self._peer_tasks.pop(peer_id, None)

        # Launch handlers for connected peers without an active task
        for peer_id, peer in list(self._client.peers.items()):
            if not peer.connected:
                continue
            if peer.initiator:
                continue
            if peer_id in self._peer_tasks:
                continue
            self._logger.info("Spawning job handler for peer %s", peer_id)
            task = asyncio.create_task(self._handle_peer(peer), name=f"compute-peer-{peer_id}")
            self._peer_tasks[peer_id] = task

    async def _shutdown_handlers(self) -> None:
        if not self._peer_tasks:
            return
        self._logger.debug("Shutting down %d peer handler(s)", len(self._peer_tasks))
        for task in self._peer_tasks.values():
            task.cancel()
        await asyncio.gather(*self._peer_tasks.values(), return_exceptions=True)
        self._peer_tasks.clear()

    async def _handle_peer(self, peer: Peer) -> None:
        logger = logging.getLogger(f"ComputeNode[{peer.peer_id}]")
        try:
            await peer.wait_channel_ready(self._send_timeout, label="compute-node")
        except TimeoutError:
            logger.warning("Data channel never became ready; abandoning peer")
            return

        logger.info("Channel open; awaiting jobs")
        while True:
            if not peer.connected:
                logger.info("Peer disconnected")
                return

            try:
                raw = await asyncio.wait_for(peer.recv(), timeout=self._recv_timeout)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("Failed to receive message: %s", exc)
                return

            message, error = self._parse_message(raw)
            if error is not None:
                await self._send_message(peer, {
                    "type": "error",
                    "error": error,
                })
                continue

            assert message is not None
            msg_type = message.get("type")

            if msg_type == "submit":
                await self._handle_job_submission(peer, message, logger)
                continue

            if msg_type == "ping":
                await self._send_message(peer, {"type": "pong", "echo": message.get("echo")})
                continue

            # Ignore response messages meant for the requesting side
            if msg_type in ("ack", "result", "pong"):
                logger.debug("Ignoring %s message (meant for requesting side)", msg_type)
                continue

            await self._send_message(
                peer,
                {
                    "type": "error",
                    "error": f"Unsupported message type: {msg_type}",
                },
            )

    async def _handle_job_submission(self, peer: Peer, message: dict[str, Any], logger: logging.Logger) -> None:
        job_id = message.get("job_id") or f"job-{uuid4().hex[:6]}"
        language = (message.get("language") or "c").lower()
        source = message.get("source")
        entry = message.get("entry") or self._entry_point

        if language != "c":
            await self._send_message(
                peer,
                {"type": "result", "job_id": job_id, "status": "error", "error": f"Unsupported language '{language}'"},
            )
            return

        if not isinstance(source, str) or not source.strip():
            await self._send_message(
                peer,
                {"type": "result", "job_id": job_id, "status": "error", "error": "Missing C source code"},
            )
            return

        await self._send_message(peer, {"type": "ack", "job_id": job_id, "status": "accepted"})

        try:
            exit_code, stdout, stderr = await asyncio.to_thread(
                compile_and_execute,
                source,
                entry,
                self._executor,
            )
        except JobExecutionError as exc:
            logger.warning("Job %s failed during %s: %s", job_id, exc.stage, exc)
            await self._send_message(
                peer,
                {
                    "type": "result",
                    "job_id": job_id,
                    "status": "error",
                    "stage": exc.stage,
                    "error": str(exc),
                },
            )
            return
        except Exception as exc:  # pragma: no cover - unexpected failure guard
            logger.exception("Unhandled failure while processing job %s", job_id)
            await self._send_message(
                peer,
                {
                    "type": "result",
                    "job_id": job_id,
                    "status": "error",
                    "error": f"Unhandled execution failure: {exc}",
                },
            )
            return

        logger.info("Job %s completed with exit code %s", job_id, exit_code)
        await self._send_message(
            peer,
            {
                "type": "result",
                "job_id": job_id,
                "status": "ok",
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    async def _send_message(self, peer: Peer, payload: dict[str, Any]) -> None:
        try:
            message = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            self._logger.error("Failed to encode message for %s: %s", peer.peer_id, exc)
            return
        try:
            await peer.send_with_retry(
                message,
                timeout=self._send_timeout,
                label=f"compute->{peer.peer_id}",
            )
        except Exception as exc:
            self._logger.warning("Failed to send message to %s: %s", peer.peer_id, exc)

    @staticmethod
    def _parse_message(payload: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if not isinstance(payload, str):
            return None, "Payload must be a UTF-8 string"
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON payload: {exc}"
        if not isinstance(message, dict):
            return None, "Message must be a JSON object"
        return message, None


def compile_and_execute(source: str, entry_point: str, executor: WasmExecutor) -> tuple[int, str, str]:
    try:
        with Compiler() as compiler:
            wasm_path = compiler.compile(source)
            result = executor.execute(wasm_path, entry_point)
            return result.exit_code, result.stdout, result.stderr
    except JobExecutionError:
        raise
    except Exception as exc:
        raise JobExecutionError(str(exc), stage="compilation") from exc


async def submit_job_to_peer(
    peer: Peer,
    job_id: str,
    source: str,
    request_cfg: RequestConfig,
    logger: logging.Logger,
) -> tuple[dict[str, Any], bool]:
    payload = {
        "type": "submit",
        "job_id": job_id,
        "language": request_cfg.language.lower(),
        "entry": request_cfg.entry_point,
        "source": source,
    }

    await peer.send_with_retry(
        json.dumps(payload),
        timeout=request_cfg.timeout,
        label=f"request->{peer.peer_id}",
    )
    logger.info("Submitted job %s to %s", job_id, peer.peer_id)

    ack_received = False
    result: Optional[dict[str, Any]] = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_cfg.timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for job result")

        try:
            raw = await asyncio.wait_for(peer.recv(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Timed out waiting for job result") from exc

        message, error = ComputeNode._parse_message(raw)
        if error is not None:
            logger.warning("Received malformed response: %s", error)
            continue
        assert message is not None

        msg_type = message.get("type")
        if msg_type == "ack" and message.get("job_id") == job_id:
            ack_received = True
            logger.info("Peer %s acknowledged job %s", peer.peer_id, job_id)
            continue
        if msg_type == "result" and message.get("job_id") == job_id:
            result = message
            break
        if msg_type == "error":
            error_text = message.get("error") or "Remote error"
            raise RuntimeError(error_text)

    if result is None:
        raise RuntimeError("Did not receive job result")

    return result, ack_received


async def close_peer_connection(client: Client, peer: Peer, logger: logging.Logger) -> None:
    logger.debug("Closing connection to %s", peer.peer_id)
    try:
        await peer.rtc.close()
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.debug("Error closing connection to %s: %s", peer.peer_id, exc)
    existing = client.peers.get(peer.peer_id)
    if existing is peer:
        client.peers.pop(peer.peer_id, None)


async def attempt_job_dispatch(client: Client, source_path: Path, app_config: AppConfig) -> None:
    logger = logging.getLogger("main.request")

    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read source file '%s': %s", source_path, exc)
        return

    if not source.strip():
        logger.warning("Source file '%s' is empty; skipping dispatch", source_path)
        return

    try:
        await client.request_peer_list()
    except Exception as exc:
        logger.error("Failed to refresh peer directory: %s", exc)
        return

    candidate_ids = [pid for pid in client.directory if pid != client.peer_id]
    if not candidate_ids:
        logger.warning("No available peers to handle job from '%s'", source_path)
        return

    for target_id in candidate_ids:
        logger.info("Attempting to submit '%s' to %s", source_path, target_id)
        try:
            peer = await client.connect_to_peer(target_id, timeout=app_config.request.timeout)
        except Exception as exc:
            logger.warning("Failed to connect to %s: %s", target_id, exc)
            continue

        job_id = f"job-{uuid4().hex[:6]}"
        try:
            result, ack_received = await submit_job_to_peer(
                peer,
                job_id,
                source,
                app_config.request,
                logger,
            )
        except TimeoutError as exc:
            logger.warning("Timed out while waiting on %s: %s", target_id, exc)
            continue
        except RuntimeError as exc:
            logger.warning("Peer %s rejected job %s: %s", target_id, job_id, exc)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled failure while communicating with %s", target_id)
            continue
        finally:
            await close_peer_connection(client, peer, logger)

        status = result.get("status")
        if status != "ok":
            error_text = result.get("error", "Unknown error")
            stage = result.get("stage")
            if stage:
                logger.warning(
                    "Peer %s reported job failure during %s: %s",
                    target_id,
                    stage,
                    error_text,
                )
            else:
                logger.warning("Peer %s reported job failure: %s", target_id, error_text)
            continue

        exit_code = result.get("exit_code")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        print(f"Job {job_id} completed via {target_id} (exit_code={exit_code})")
        if stdout:
            print("--- stdout ---")
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print("--- stderr ---")
            print(stderr, end="" if stderr.endswith("\n") else "\n")

        if not ack_received:
            logger.warning("Job %s completed via %s but no ACK was observed", job_id, target_id)
        return

    logger.error("All peers rejected job sourced from '%s'", source_path)


async def stdin_loop(client: Client, app_config: AppConfig, stop_event: asyncio.Event) -> None:
    logger = logging.getLogger("main.stdin")

    while not stop_event.is_set():
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except Exception as exc:  # pragma: no cover - stdin failure is unexpected
            logger.error("Failed to read from stdin: %s", exc)
            stop_event.set()
            return

        if line == "":
            logger.info("Standard input closed; stopping job submission loop")
            stop_event.set()
            return

        path_text = line.strip()
        if not path_text:
            continue

        await attempt_job_dispatch(client, Path(path_text), app_config)


async def run_node(app_config: AppConfig) -> None:
    client_config = ClientConfig(
        server_url=app_config.server_url,
        peer_id=app_config.compute.peer_id,
        metadata=dict(app_config.compute.metadata),
    )

    async with Client(client_config) as client:
        logger = logging.getLogger("main")
        logger.info("Registered node as %s", client.peer_id)

        service = ComputeNode(
            client,
            entry_point=app_config.compute.entry_point,
            poll_interval=app_config.compute.poll_interval,
            recv_timeout=app_config.compute.recv_timeout,
            send_timeout=app_config.compute.send_timeout,
        )

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            if not stop_event.is_set():
                logger.info("Stop signal received; shutting down")
                stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _signal_handler)

        compute_task = asyncio.create_task(service.run(), name="computenet-compute-service")
        stdin_task = asyncio.create_task(stdin_loop(client, app_config, stop_event), name="computenet-stdin-listener")

        def _on_compute_done(task: asyncio.Task[None]) -> None:
            if stop_event.is_set():
                return
            if task.cancelled():
                stop_event.set()
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Compute service stopped unexpectedly: %s", exc)
            stop_event.set()

        compute_task.add_done_callback(_on_compute_done)

        logger.info("Waiting for file paths on stdin to dispatch jobs")

        try:
            await stop_event.wait()
        finally:
            compute_task.cancel()
            stdin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await compute_task
            with contextlib.suppress(asyncio.CancelledError):
                await stdin_task


def load_config(argv: Optional[list[str]] = None) -> AppConfig:
    parser = argparse.ArgumentParser(description="ComputeNet hybrid node")
    parser.add_argument(
        "--server-url",
        default=ClientConfig().server_url,
        help="Signaling server URL.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args(argv)

    return AppConfig(server_url=args.server_url, verbose=args.verbose)


def main(argv: Optional[list[str]] = None, config: Optional[AppConfig] = None) -> None:
    app_config = config or load_config(argv)
    configure_logging(app_config.verbose)

    try:
        asyncio.run(run_node(app_config))
    except KeyboardInterrupt:
        logging.getLogger("main").info("Interrupted by user")
    except Exception as exc:
        logging.getLogger("main").error("Fatal error: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
