import websockets
import asyncio
import argparse
from websockets.sync.client import connect

from messages import MessageType, Message, BaseMessage, ResultResponse, SubmitCodeMessage

code = """
#include <stdio.h>

int main() {
    printf("hello world");
}
"""

async def main(host, port):
    uri = f"ws://{host}:{port}"
    async with websockets.connect(uri) as websocket:
        msg: SubmitCodeMessage = SubmitCodeMessage(MessageType.SUBMIT_CODE, code)
        await websocket.send(msg.to_json())
        data: str | bytes = await websocket.recv()
        assert(isinstance(data, str))
        result: Message = BaseMessage.from_json(data)
        assert(isinstance(result, ResultResponse))
        print("stdout")
        print("----------")
        print(result.stdout)
        print("stderr")
        print("----------")
        print(result.stderr)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", default=8765)
    args = parser.parse_args()
    asyncio.run(main(args.host, args.port))

