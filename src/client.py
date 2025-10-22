import asyncio, argparse, uuid, json
from pathlib import Path
import websockets

from messages import Message, BaseMessage, ResultResponse, SubmitCodeMessage, MessageType

def _load_peers():
    """Load peers from peers.json (same dir as this file)."""
    path = Path(__file__).with_name("peers.json")
    if not path.exists():
        raise ValueError(f"No peers.json file in directory.")
    try:
        data = json.loads(path.read_text())
        # Expect list of {"host": str, "port": int, "peer_id": Optional[str]}
        peers = []
        for i, p in enumerate(data):
            host = p.get("host")
            port = p.get("port")
            pid  = p.get("peer_id") or f"{host}:{port}"
            if not isinstance(host, str) or not isinstance(port, int):
                raise ValueError(f"Invalid peer at index {i}: {p!r}")
            peers.append({"peer_id": pid, "host": host, "port": port})
        return peers
    except Exception:
        raise ValueError(f"Parse failure: peers.json not formatted correctly.")


PEERS = _load_peers()
print(PEERS)

async def choose_peer():
    last_err = None
    for p in PEERS:
        uri = f"ws://{p['host']}:{p['port']}"
        try:
            ws = await websockets.connect(uri)
            await ws.ping()
            return p, ws
        except Exception as e:
            last_err = e
    raise RuntimeError(f"No available peers (last error: {last_err})")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-file", type=str)
    args = ap.parse_args()

    # default C program fallback
    code = """
    #include <stdio.h>

    int main() {
        printf("hello world2");
    }
    """

    if args.code_file:
        with open(args.code_file, "r") as f:
            code = f.read()

    peer, ws = await choose_peer()
    print(f"[client] picked {peer['peer_id']} at {peer['host']}:{peer['port']}")

    # submit code
    job_id = "j-" + uuid.uuid4().hex[:6]
    msg = SubmitCodeMessage(MessageType.SUBMIT_CODE, code)
    await ws.send(msg.to_json())
    print(f"[client] submitted {job_id} (logical id on client; server assigns internally)")

    data = await ws.recv()
    assert isinstance(data, str)
    res: Message = BaseMessage.from_json(data)
    assert isinstance(res, ResultResponse), f"Unexpected message type: {type(res)}"

    print("stdout")
    print("----------")
    print(res.stdout or "", end="")
    print("\nstderr")
    print("----------")
    print(res.stderr or "", end="")

    await ws.close()

if __name__ == "__main__":
    asyncio.run(main())
