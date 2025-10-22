"""
Code Execution Engine (CEE)
Executes WebAssembly modules using wasmtime.
"""

from pathlib import Path
import wasmtime
import websockets
from job import Job
import tempfile
from compiler import Compiler


class Executor:
    def __init__(self):
        self.wasm = WasmExecutor()
        self.compiler = Compiler()

    def execute(self, job: Job):
        with tempfile.TemporaryDirectory() as tmp_dir:
            code = job.code
            path = self.compiler.compile(code, Path(tmp_dir))
            stdout_path =  Path(tmp_dir) / "output.out"
            stderr_path = Path(tmp_dir) / "error.out"
            self.wasm.execute(path, stdout_path, stderr_path)
            job.stdout = stdout_path.read_text(encoding="utf-8")
            job.stderr = stderr_path.read_text(encoding="utf-8")


class WasmExecutor:
    """Executes WebAssembly modules using wasmtime."""

    def __init__(self):
        self.engine = wasmtime.Engine()

    def execute(self, wasm_path: Path, stdout_path: Path, stderr_path: Path, function_name: str = "_start"):
        """
        Execute a WASM module.

        Args:
            wasm_path: Path to WASM file
            function_name: Name of function to call (default: "_start")
        """
        # Setup WASI
        wasi_config = wasmtime.WasiConfig()
        wasi_config.inherit_stdout()
        wasi_config.stdout_file = stdout_path
        wasi_config.stderr_file = stderr_path
        # wasi_config.inherit_stderr()
        # wasi_config.inherit_stdin()

        store = wasmtime.Store(self.engine)
        store.set_wasi(wasi_config)

        # Load module
        module = wasmtime.Module.from_file(self.engine, str(wasm_path))
        linker = wasmtime.Linker(self.engine)
        linker.define_wasi()

        # Instantiate and run
        instance = linker.instantiate(store, module)
        func = instance.exports(store).get(function_name)

        if func:
            func(store)


if __name__ == "__main__":
    from compiler import Compiler

    # Test with C code
    code = """
    #include <stdio.h>
    int main() {
        printf("Hello World!\\n");
        return 0;
    }
    """

    job: Job = Job(code, None)
    exe: Executor = Executor()
    exe.execute(job)
    assert(job.stdout == "Hello World!\n")
