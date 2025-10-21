"""
End-to-end tests for ComputeNet.

Tests the full workflow:
1. Start signaling server (server.py)
2. Start compute nodes (main.py instances)
3. Submit C code from client
4. Verify execution results

## Running the tests

Run all e2e tests:
    pytest test_e2e.py -v

Run a specific test:
    pytest test_e2e.py::test_basic_hello_world -v

Run with output (to see server logs):
    pytest test_e2e.py -v -s

## Test Structure

- ServerProcess: Manages server.py subprocess
- ComputeNodeProcess: Manages main.py subprocess (compute node)
- submit_and_get_result(): Helper to submit jobs and get results

## Tests Included

1. test_basic_hello_world: Tests basic C code execution
2. test_non_zero_exit_code: Tests non-zero exit codes
3. test_stderr_output: Tests stderr capture
4. test_compilation_error: Tests handling of compilation errors
"""

import asyncio
import json
import pytest
import pytest_asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from client import Client, ClientConfig


# Constants
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
CLIENT_TIMEOUT = 30  # seconds


class ServerProcess:
    """Manages the signaling server subprocess."""

    def __init__(self, host: str = SERVER_HOST, port: int = SERVER_PORT):
        self.host = host
        self.port = port
        self.process: Optional[subprocess.Popen] = None

    async def start(self):
        """Start the server subprocess."""
        # Start server.py
        self.process = subprocess.Popen(
            [sys.executable, "server.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Wait for server to be ready
        await asyncio.sleep(2)

        # Check if process is still running
        if self.process.poll() is not None:
            stdout, stderr = self.process.communicate()
            raise RuntimeError(
                f"Server failed to start.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

    async def stop(self):
        """Stop the server subprocess."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class ComputeNodeProcess:
    """Manages a compute node (main.py) subprocess."""

    def __init__(
        self,
        server_url: str,
        verbose: bool = False
    ):
        self.server_url = server_url
        self.verbose = verbose
        self.process: Optional[subprocess.Popen] = None

    async def start(self):
        """Start the compute node subprocess."""
        # Start main.py with stdin pipe for sending code
        args = [
            sys.executable,
            "main.py",
            "--server-url", self.server_url
        ]

        if self.verbose:
            args.append("--verbose")

        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Wait for node to connect to server
        await asyncio.sleep(3)

        # Check if process is still running
        if self.process.poll() is not None:
            stdout, stderr = self.process.communicate()
            raise RuntimeError(
                f"Compute node failed to start.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

    async def stop(self):
        """Stop the compute node subprocess."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def submit_code_via_stdin(self, code_file_path: str):
        """Submit a code file path via stdin."""
        if self.process and self.process.stdin:
            self.process.stdin.write(f"{code_file_path}\n")
            self.process.stdin.flush()


@pytest_asyncio.fixture
async def signaling_server():
    """Fixture to start/stop the signaling server."""
    server = ServerProcess()
    await server.start()

    yield server

    await server.stop()


@pytest_asyncio.fixture
async def compute_node(signaling_server):
    """Fixture to start/stop a single compute node."""
    server_url = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
    node = ComputeNodeProcess(server_url=server_url)
    await node.start()

    yield node

    await node.stop()


@pytest_asyncio.fixture
async def two_compute_nodes(signaling_server):
    """Fixture to start/stop two compute nodes."""
    server_url = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
    node1 = ComputeNodeProcess(server_url=server_url)
    node2 = ComputeNodeProcess(server_url=server_url)

    await node1.start()
    await node2.start()

    yield node1, node2

    await node1.stop()
    await node2.stop()


# Helper functions

def create_temp_c_file(source_code: str) -> Path:
    """Create a temporary C file with the given source code."""
    temp_file = tempfile.NamedTemporaryFile(
        mode='w',
        suffix='.c',
        delete=False
    )
    temp_file.write(source_code)
    temp_file.close()
    return Path(temp_file.name)


async def submit_and_get_result(
    client: Client,
    c_code: str,
    job_id: str,
    timeout: float = 30.0
) -> dict:
    """
    Helper function to submit C code to a compute node and get the result.

    Returns the result message dict.
    """
    # Get available peers
    peers = await client.request_peer_list()
    assert len(peers) >= 1, f"Expected at least 1 peer, got {len(peers)}"

    # Connect to first available peer
    target_peer_id = next(iter(peers.keys()))
    peer = await client.connect_to_peer(target_peer_id, timeout=10.0)

    # Submit the job
    message = {
        "type": "submit",
        "job_id": job_id,
        "language": "c",
        "entry": "_start",
        "source": c_code
    }

    await peer.send_with_retry(json.dumps(message), timeout=5.0)

    # Wait for ack
    ack_response = await asyncio.wait_for(peer.recv(), timeout=10.0)
    ack_msg = json.loads(ack_response)

    # If first message is not ack, it might be result (for early errors)
    if ack_msg["type"] != "ack":
        return ack_msg

    assert ack_msg["job_id"] == job_id

    # Wait for result
    result_response = await asyncio.wait_for(peer.recv(), timeout=timeout)
    result_msg = json.loads(result_response)

    return result_msg


# Test cases

@pytest.mark.asyncio
async def test_basic_hello_world(signaling_server, two_compute_nodes):
    """Test submitting a basic Hello World program."""
    node1, node2 = two_compute_nodes

    # Create a simple C program
    c_code = """
#include <stdio.h>

int main() {
    printf("Hello from ComputeNet!\\n");
    return 0;
}
"""

    # Create a client to connect to the network
    config = ClientConfig(
        server_url=f"ws://{SERVER_HOST}:{SERVER_PORT}/ws",
        peer_id="test-client",
        connect_timeout=10.0
    )

    async with Client(config) as client:
        # Wait a bit for compute nodes to be discoverable
        await asyncio.sleep(2)

        # Submit the job and get result
        result_msg = await submit_and_get_result(client, c_code, "test-job-1")

        # Verify the response
        assert result_msg is not None, "No response received from compute node"
        assert result_msg["type"] == "result"
        assert result_msg["job_id"] == "test-job-1"
        assert result_msg["status"] == "ok", f"Job failed: {result_msg}"
        assert result_msg["exit_code"] == 0, f"Non-zero exit code: {result_msg['exit_code']}"
        assert "Hello from ComputeNet!" in result_msg["stdout"]


@pytest.mark.asyncio
async def test_non_zero_exit_code(signaling_server, two_compute_nodes):
    """Test a program with a non-zero exit code."""
    node1, node2 = two_compute_nodes

    c_code = """
#include <stdio.h>

int main() {
    printf("This program will exit with code 42\\n");
    return 42;
}
"""

    config = ClientConfig(
        server_url=f"ws://{SERVER_HOST}:{SERVER_PORT}/ws",
        peer_id="test-client-2",
        connect_timeout=10.0
    )

    async with Client(config) as client:
        await asyncio.sleep(2)

        result_msg = await submit_and_get_result(client, c_code, "test-job-2")

        assert result_msg is not None
        assert result_msg["status"] == "ok"
        assert result_msg["exit_code"] == 42


@pytest.mark.asyncio
async def test_stderr_output(signaling_server, two_compute_nodes):
    """Test a program that writes to stderr."""
    node1, node2 = two_compute_nodes

    c_code = """
#include <stdio.h>

int main() {
    printf("This goes to stdout\\n");
    fprintf(stderr, "This goes to stderr\\n");
    return 0;
}
"""

    config = ClientConfig(
        server_url=f"ws://{SERVER_HOST}:{SERVER_PORT}/ws",
        peer_id="test-client-3",
        connect_timeout=10.0
    )

    async with Client(config) as client:
        await asyncio.sleep(2)

        result_msg = await submit_and_get_result(client, c_code, "test-job-3")

        assert result_msg is not None
        assert result_msg["status"] == "ok"
        assert "stdout" in result_msg["stdout"]
        assert "stderr" in result_msg["stderr"]


@pytest.mark.asyncio
async def test_compilation_error(signaling_server, two_compute_nodes):
    """Test submitting invalid C code that fails compilation."""
    node1, node2 = two_compute_nodes

    # Invalid C code (missing semicolon)
    c_code = """
#include <stdio.h>

int main() {
    printf("This is missing a semicolon")
    return 0;
}
"""

    config = ClientConfig(
        server_url=f"ws://{SERVER_HOST}:{SERVER_PORT}/ws",
        peer_id="test-client-4",
        connect_timeout=10.0
    )

    async with Client(config) as client:
        await asyncio.sleep(2)

        result_msg = await submit_and_get_result(client, c_code, "test-job-4")

        assert result_msg is not None
        assert result_msg["status"] == "error"
        assert result_msg.get("stage") == "compilation"
        assert "error" in result_msg


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])
