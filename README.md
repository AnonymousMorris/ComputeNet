# ComputeNet
A prototype network that allows easy sharing of compute resources to others on the network with a decentralized architecture. 

# Architecture
src
├── CEE.py
├── client2.py
├── client.py
├── compiler.py
├── compute_node.py
├── job.py
├── messages.py
└── peers.json

## Code Execution
*CEE.py*: Runs the compiled wasm module.
*compiler.py*: Compiles the code passed in to wasm
*compute_node.py*: Listens and parses the jobs requests

## Schema
*messages.py*: The message schema for compute_node.py
*job.py*: The compute request and associated meta data

## Interface
*client.py*: Exposes the CLI to submit tasks to run

## Config
*peers.json*: address and ports of peers


# Installation
This project relies on WASM for sandboxing. This means that there's a few WASM dependencies needed.

`wasi_libc`: Standard C Library for WASM module
`wasi-compiler-rt`: LLVM low level builtin for WASM
`clang`: C compiler frontend for LLVM, we need the LLVM for the WASM support

Install all python libraries
```bash
uv sync
```

# Usage

## Compute Node
```bash
python compute_node.py
python compute_node.py --host localhost --port 8888
```
## Submit Job
```bash
python client.py
python client.py --code-file test.c
```

# Tests

## Code Execution
The CEE.py has a builtin test case where it compiles and run hello world
```bash
python CEE.py
```

## End to End
Spins up a compute node and submits a job
```bash
python test_e2e.py
```

# Functionalities
1. Compute Node server that accepts job request
2. Setup compilation for C code to WASM
3. client to submit compute job

# TODO
1. Central Server for peer discovery
2. Peer to Peer protocol for direct file transfer across different LAN
