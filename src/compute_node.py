import asyncio
import websockets
import argparse
from websockets.asyncio.server import serve
from messages import BaseMessage, Message, RegisterMessage, SubmitCodeMessage, ResultResponse
from job import Job, JobStatus
from CEE import Executor

class ComputeNode:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.q: asyncio.Queue[Job] = asyncio.Queue()
        self.exe = Executor()

    async def _handle_register(self):
        pass

    async def _handle_submit_code(self, msg: SubmitCodeMessage, conn: websockets.ServerConnection):
        code = msg.code
        # TODO: add error handling for case of queue being full
        await self.q.put(Job(code, conn))

    async def handle_client(self, websocket):
        async for message in websocket:
            assert(isinstance(message , str))
            msg: Message = BaseMessage.from_json(message)

            if isinstance(msg, SubmitCodeMessage):
                await self._handle_submit_code(msg, websocket)
            else:
                raise ValueError(f"Received unexpected message type: {message}")

    async def _compute_runner(self):
        while True:
            job = await self.q.get()
            self.exe.execute(job)
            job.status = JobStatus.SUCCESS
            msg: ResultResponse = ResultResponse.from_job(job)
            await job.conn.send(msg.to_json())
            

    async def run(self):
        assert(self.host is not None)
        assert(self.port is not None)
        asyncio.create_task(self._compute_runner())
        async with serve(self.handle_client, self.host, self.port) as server:
            await server.serve_forever()

async def main(host: str, port: int):
    node = ComputeNode(host, port)
    await node.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", default="8765")
    args = parser.parse_args()
    asyncio.run(main(args.host, int(args.port)))
