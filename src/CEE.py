"""Code Execution Engine (CEE) for running WebAssembly modules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import wasmtime

from tempdir import TemporaryDirectory


class JobExecutionError(Exception):
    """Raised when a WebAssembly job fails during a specific stage."""

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(slots=True)
class ExecutionResult:
    """Container for WebAssembly execution results."""

    exit_code: int
    stdout: str
    stderr: str


class WasmExecutor:
    """Executes WebAssembly modules using wasmtime with WASI support."""

    def __init__(self, engine: Optional[wasmtime.Engine] = None) -> None:
        self.engine = engine or wasmtime.Engine()
        self._logger = logging.getLogger("WasmExecutor")

    def execute(self, wasm_path: str | Path, function_name: str = "_start") -> ExecutionResult:
        """Run a compiled WebAssembly module and capture its output."""

        module_path = Path(wasm_path)

        with TemporaryDirectory(prefix="computenet_wasm_") as temp_dir:
            stdout_path = temp_dir / "stdout.log"
            stderr_path = temp_dir / "stderr.log"

            # Configure Input and Output for wasm
            wasi_config = wasmtime.WasiConfig()
            wasi_config.inherit_stdin()
            wasi_config.stdout_file = str(stdout_path)
            wasi_config.stderr_file = str(stderr_path)

            store = wasmtime.Store(self.engine)
            store.set_wasi(wasi_config)

            try:
                module = wasmtime.Module.from_file(self.engine, str(module_path))
            except Exception as exc:  # pragma: no cover - best-effort error reporting
                raise JobExecutionError(str(exc), stage="compilation") from exc

            linker = wasmtime.Linker(self.engine)
            linker.define_wasi()

            try:
                instance = linker.instantiate(store, module)
            except Exception as exc:
                raise JobExecutionError(str(exc), stage="instantiation") from exc

            start_func = instance.exports(store).get(function_name)
            if start_func is None:
                raise JobExecutionError(
                    f"Entry point '{function_name}' not found in module exports",
                    stage="execution",
                )

            exit_code = 0
            try:
                start_func(store)
            except wasmtime.ExitTrap as exc:
                exit_code = exc.code
            except Exception as exc:  # pragma: no cover - defensive guard
                self._logger.warning("Execution raised unexpected error: %s", exc)
                raise JobExecutionError(str(exc), stage="execution") from exc

            stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
            stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""

        return ExecutionResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


if __name__ == "__main__":
    from compiler import Compiler

    SAMPLE = """
    #include <stdio.h>
    int main(void) {
        printf("Hello World!\\n");
        return 0;
    }
    """

    with Compiler() as compiler:
        wasm = compiler.compile(SAMPLE)

        executor = WasmExecutor()
        try:
            result = executor.execute(wasm)
        except JobExecutionError as exc:  
            print(f"Execution failed during {exc.stage}: {exc}")
        else:
            print(f"exit_code={result.exit_code}")
            if result.stdout:
                print(result.stdout, end="")
