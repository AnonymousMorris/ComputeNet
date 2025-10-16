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
        self.temp_dir = tempfile.mkdtemp(prefix='compiler_')

    def compile(self, code: str) -> Path:
        """
        Compile C code to WASM.

        Args:
            code: C source code string

        Returns:
            Path to compiled WASM file
        """
        # Write source file
        source_file = Path(self.temp_dir) / "source.c"
        source_file.write_text(code)

        # Compile to WASM
        wasm_file = Path(self.temp_dir) / "output.wasm"

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

    def cleanup(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir)

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
