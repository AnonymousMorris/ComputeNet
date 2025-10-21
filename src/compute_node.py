from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from uuid import uuid4

from CEE import JobExecutionError, WasmExecutor
from client import Client, Peer
from compiler import Compiler


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
            if not peer._ready.is_set():
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
            await peer.connect(self._send_timeout)
        except TimeoutError:
            logger.warning("Data channel never became ready; abandoning peer")
            return

        logger.info("Channel open; awaiting jobs")
        while True:
            if not peer._ready.is_set():
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
