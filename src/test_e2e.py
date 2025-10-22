import subprocess
import time
import asyncio
from submit import main as submit_main


def test_basic_submit():
    server = subprocess.Popen(
        ["python", "compute_node.py", "--host", "localhost", "--port", "8765"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        time.sleep(1)

        result = asyncio.run(submit_main("localhost", 8765))

        assert result.stdout.strip() == "hello world", f"Expected 'hello world', got '{result.stdout.strip()}'"

        print("Test passed!")

    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    test_basic_submit()
