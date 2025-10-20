import argparse, asyncio, json, time, uuid
from dataclasses import dataclass

@dataclass
class Result:
    job_id: str
    exit_code: int
    stdout_text: str
    stderr_text: str = ""

# ---- executor ---------------
class FakeExecutor:
    def run(self, code_c: str | None, wasm_b64: str | None, entry: str) -> Result:
        # Simulate some work
        time.sleep(0.3)
        if code_c:
            out = "Hello from C!\n"
        elif wasm_b64:
            out = "Hello from precompiled WASM (simulated)!\n"
        else:
            out = "No code provided.\n"
        return Result(job_id="j-"+uuid.uuid4().hex[:6], exit_code=0, stdout_text=out)

# ---- Peer Agent: handles messages per connection -----------------------------
class PeerAgent:
    def __init__(self, peer_id: str):
        self.peer_id = peer_id
        self.busy = False
        self.exec = FakeExecutor()

    async def handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line.decode())
                t = msg.get("type"); rid = msg.get("req_id")

                # helpers
                async def reply(typ, payload):
                    out = {"proto_ver":"0.1","type":typ,"req_id":rid,"payload":payload}
                    writer.write((json.dumps(out) + "\n").encode()); await writer.drain()
                async def push(typ, payload):
                    out = {"proto_ver":"0.1","type":typ,"payload":payload}
                    writer.write((json.dumps(out) + "\n").encode()); await writer.drain()

                if t == "CAP_REQ":
                    await reply("CAP_RES", {"peer_id": self.peer_id, "is_busy": self.busy})
                elif t == "SUBMIT":
                    if self.busy:
                        await reply("ERROR", {"code":"PEER_BUSY","message":"peer is busy"})
                        continue
                    job = msg["payload"]["job"]
                    job_id = job.get("job_id") or ("j-"+uuid.uuid4().hex[:6])
                    await reply("ACK", {"job_id": job_id, "accepted": True})
                    # Run in background
                    asyncio.create_task(self._run_and_push(job_id, job, push))
                elif t == "STATUS_REQ":
                    await reply("STATUS_RES", {"state": "running" if self.busy else "idle"})
                else:
                    await reply("ERROR", {"code":"BAD_TYPE","message": f"unknown type {t}"})
        finally:
            writer.close()
            await writer.wait_closed()

    async def _run_and_push(self, job_id: str, job: dict, push):
        try:
            self.busy = True
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.exec.run(job.get("code_c"), job.get("wasm_bytes_b64"), job.get("entry","_start"))
            )
            await push("RESULT_PUSH", {"job_id": job_id, "exit_code": res.exit_code, "stdout": res.stdout_text, "stderr": res.stderr_text})
        except Exception as e:
            await push("RESULT_PUSH", {"job_id": job_id, "exit_code": 1, "stdout": "", "stderr": str(e)})
        finally:
            self.busy = False

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--peer-id", type=str, default="peer-us")
    args = ap.parse_args()

    agent = PeerAgent(args.peer_id)
    server = await asyncio.start_server(agent.handle_conn, host="127.0.0.1", port=args.port)
    print(f"[{args.peer_id}] listening on 127.0.0.1:{args.port}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
