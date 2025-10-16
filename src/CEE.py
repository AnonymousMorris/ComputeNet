"""
Code Execution Engine (CEE)
Executes WebAssembly modules using wasmtime.
"""

from pathlib import Path
import wasmtime


class WasmExecutor:
    """Executes WebAssembly modules using wasmtime."""

    def __init__(self):
        self.engine = wasmtime.Engine()

    def execute(self, wasm_path: str | Path, function_name: str = "_start"):
        """
        Execute a WASM module.

        Args:
            wasm_path: Path to WASM file
            function_name: Name of function to call (default: "_start")
        """
        # Setup WASI
        wasi_config = wasmtime.WasiConfig()
        wasi_config.inherit_stdout()
        wasi_config.inherit_stderr()
        wasi_config.inherit_stdin()

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

    compiler = Compiler()
    wasm_path = compiler.compile(code)

    executor = WasmExecutor()
    executor.execute(wasm_path)

    compiler.cleanup()
