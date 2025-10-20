"""Code Compiler
Compiles C code to WebAssembly.
"""

import subprocess
from pathlib import Path
from typing import Optional

from tempdir import TemporaryDirectory


class Compiler:
    """Compiles C code to WebAssembly using clang."""

    def __init__(self) -> None:
        self._tempdir_ctx: Optional[TemporaryDirectory] = TemporaryDirectory(prefix="compiler_")
        self._temp_path = self._tempdir_ctx.__enter__()
        self.temp_dir = str(self._temp_path)

    def compile(self, code: str) -> Path:
        """
        Compile C code to WASM.

        Args:
            code: C source code string

        Returns:
            Path to compiled WASM file
        """
        # Write source file
        if self._temp_path is None:
            raise RuntimeError("Compiler temporary directory is not available")

        source_file = self._temp_path / "source.c"
        source_file.write_text(code)

        # Compile to WASM
        wasm_file = self._temp_path / "output.wasm"

        result = subprocess.run([
            'clang',
            '--target=wasm32-wasi',
            '--sysroot=/usr/share/wasi-sysroot',
            '-rtlib=compiler-rt',
            '-o', str(wasm_file),
            str(source_file),
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(f"Compilation failed:\n{result.stderr}")

        return wasm_file

    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self._tempdir_ctx is None:
            return

        self._tempdir_ctx.__exit__(None, None, None)
        self._tempdir_ctx = None
        self._temp_path = None
        self.temp_dir = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


if __name__ == "__main__":
    code = """
    #include <stdio.h>
    int main() {
        printf("Hello World!\\n");
        return 0;
    }
    """

    with Compiler() as compiler:
        wasm_path = compiler.compile(code)
        print(f"Compiled to: {wasm_path}")
