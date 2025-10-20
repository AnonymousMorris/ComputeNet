import argparse, asyncio, json, uuid, random

class TcpChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self._handlers = []
        self._closed = False

    @classmethod
    async def connect(cls, host: str, port: int):
        r, w = await asyncio.open_connection(host, port)
        ch = cls(r, w)
        asyncio.create_task(ch._reader_loop())
        return ch

    async def send(self, obj: dict):
        self.writer.write((json.dumps(obj) + "\n").encode())
        await self.writer.drain()

    def on_message(self, cb):
        self._handlers.append(cb)

    async def close(self):
        if not self._closed:
            self._closed = True
            self.writer.close()
            await self.writer.wait_closed()

    async def _reader_loop(self):
        try:
            while not self._closed:
                line = await self.reader.readline()
                if not line:
                    break
                msg = json.loads(line.decode())
                for h in list(self._handlers):
                    h(msg)
        finally:
            await self.close()

# RPC Protocol
"""
Client -> Peer:
CAP_REQ
Peer -> Client:
CAP_RES

Client -> Peer:
SUBMIT
Peer -> Client:
ACK
Peer -> Client:
RESULT_PUSH

Sample:
{
  "type": "CAP_REQ",
  "req_id": "1234",
  "payload": {}
}
"""
class Rpc:
    def __init__(self, ch: TcpChannel):
        self.ch = ch # connected to a channel (abstraction)
        self.waiters = {} # req_id -> future. Tracks which request is waiting for which response
        self.subs = {} # 
        self.ch.on_message(self._on_msg)

    async def call(self, typ: str, payload: dict, expect=("ACK","CAP_RES","STATUS_RES","ERROR"), timeout=5.0):
        rid = str(uuid.uuid4()) # request id
        fut = asyncio.get_event_loop().create_future()
        self.waiters[rid] = fut

        await self.ch.send({"proto_ver":"0.1", "type":typ, "req_id":rid, "payload":payload})
        res = await asyncio.wait_for(fut, timeout=timeout)

        if res["type"] not in expect:
            raise RuntimeError(f"unexpected {res['type']}, expected {expect}")
        return res
    
    def subscribe(self, typ: str, handler):
        self.subs.setdefault(typ, []).append(handler)

    # message router, it decides where each incoming message should go
    def _on_msg(self, msg: dict):
        rid = msg.get("req_id")
        if rid and rid in self.waiters and msg["type"] in ("ACK","ERROR","CAP_RES","STATUS_RES"):
            fut = self.waiters.pop(rid)
            if not fut.done():
                fut.set_result(msg)
            return
        for h in self.subs.get(msg["type"], []):
            h(msg)

# Client Logic: probe peers -> pick peer -> submit code -> await result -> output result
PEERS = [
    {"peer_id":"peer-us",   "host":"127.0.0.1", "port":9101},
    {"peer_id":"peer-eu",   "host":"127.0.0.1", "port":9102},
    {"peer_id":"peer-asia", "host":"127.0.0.1", "port":9103},
]

# Probe all peers, and choose the first free one (currently does not include considering free CPU & memory)
async def choose_peer():
    candidates = []

    for p in PEERS:
        try:
            ch = await TcpChannel.connect(p["host"], p["port"]) # establish channel for current peer
            rpc = Rpc(ch)

            res = await rpc.call("CAP_REQ", {}) # send a CAP_REQ, receive a CAP_RES
            busy = res["payload"]["is_busy"]
            if not busy:
                candidates.append((p, ch, rpc))
            else:
                await ch.close() # not a valid candidate, just close the connection
        except Exception:
            pass

    # 
    if not candidates:
        raise RuntimeError("No available peers")
    
    idx = random.randint(0, len(candidates)-1) # select a peer on random based on idx
    return candidates[idx]  # (peer, ch, rpc)


async def main():
    # parse the CLI for any files to submit
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-file", type=str, help="Path to a C file; if omitted, uses a Hello World sample.")
    args = ap.parse_args()

    # default to hello world in C
    code = r'#include <stdio.h>\nint main(){ printf("Hello from Client Code!\\n"); return 0; }\n'

    if args.code_file:
        with open(args.code_file, "r") as f:
            code = f.read()
    
    peer, ch, rpc = await choose_peer()
    print(f"[client] picked {peer['peer_id']} at {peer['host']}:{peer['port']}")

    # submit a job
    job_id = "j-" + uuid.uuid4().hex[:6]

    # send an acknowledgement to the peer, to verify that the connection is established whilst submitting the job
    # send a SUBMIT type -> receive an ACK type
    ack = await rpc.call("SUBMIT", {"job": {"job_id": job_id, "entry": "_start", "code_c": code}})
    print(f"[client] submitted {ack['payload']['job_id']}")

    # wait for result push: peer -> client (happens after code exec)
    # the RESULT_PUSH, will be pushed asynchronously and not part of an RPC request/response pair
    done = asyncio.get_event_loop().create_future()
    def on_result(msg):
        if msg["payload"]["job_id"] == job_id:
            print(f"[client] RESULT exit={msg['payload']['exit_code']}")
            if msg["payload"]["stdout"]:
                print(msg["payload"]["stdout"], end="")
            if msg["payload"]["stderr"]:
                print(msg["payload"]["stderr"], end="")
            if not done.done(): 
                done.set_result(True)
    rpc.subscribe("RESULT_PUSH", on_result) # expect/subscribe to a push that the client will do

    await done
    await ch.close()

if __name__ == "__main__":
    asyncio.run(main())