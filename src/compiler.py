"""
Code Compiler
Compiles C code to WebAssembly.
"""

import subprocess
import tempfile
from pathlib import Path


class Compiler:
    """Compiles C code to WebAssembly using clang."""

    def __init__(self):
        pass

    def compile(self, code: str, temp_dir: Path) -> Path:
        """
        Compile C code to WASM.

        Args:
            code: C source code string

        Returns:
            Path to compiled WASM file
        """
        # Write source file
        source_file = Path(temp_dir) / "source.c"
        source_file.write_text(code)

        # Compile to WASM
        wasm_file = Path(temp_dir) / "output.wasm"

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
