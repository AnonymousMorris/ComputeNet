# Installation Guide

## System Requirements

ComputeNet requires additional system packages to compile C code to WebAssembly.

### Arch Linux

Install the following packages:

```bash
sudo pacman -S clang wasi-libc wasi-compiler-rt
```

### Package Descriptions

- **clang** - The LLVM C/C++ compiler with WebAssembly support. Unlike GCC, clang has a built-in WebAssembly backend that can target `wasm32-wasi`.

- **wasi-libc** - The C standard library (libc) implementation for WASI (WebAssembly System Interface). Provides headers like `stdio.h`, `stdlib.h`, etc. that are needed for standard C programs.

- **wasi-compiler-rt** - The LLVM compiler runtime library for WASI. Contains low-level runtime support functions (like `libclang_rt.builtins.a`) that the compiler needs during the linking phase.

## Python Dependencies

Python dependencies are managed via `pyproject.toml` and can be installed using:

```bash
uv sync
```

Key dependencies:
- `wasmtime>=37.0.0` - Python bindings for the Wasmtime WebAssembly runtime

## Running the Project

After installing dependencies, test the installation:

```bash
uv run python src/CEE.py
```

You should see `Hello World!` output if everything is configured correctly.
