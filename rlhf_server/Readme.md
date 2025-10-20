# Terminal Bench HTTP Server

A terminal bench based HTTP server exposing APIs to create and manage containers and execute code inside them, enabling RLHF frameworks like VERL to execute tool calls during training on different compute environments.

## Overview

This server provides a RESTful API layer on top of Terminal Bench, allowing remote container orchestration and code execution. It's designed to support reinforcement learning from human feedback (RLHF) frameworks that need to evaluate tool calls across distributed compute environments during model training.

## Integration with VERL

This server can be integrated with VERL or other RLHF frameworks to enable tool execution during training. Configure your framework to point to this server's endpoints for container-based code execution.


## Prerequisites

- Docker (for container management)
- Git
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer

## Installation

### 1. Install Terminal Bench

Clone the Terminal Bench repository and install it in editable mode at the specified commit:

```bash
git clone https://github.com/laude-institute/terminal-bench.git
cd terminal-bench
git checkout 9f7cb4f4e1363db9c07e331ba81c99a02e5bafcd
uv pip install -e .
```

The editable install (`-e` flag) allows you to modify the Terminal Bench source code without reinstalling, which is useful for development and customization.

### 3. Execute server 

Execute the server in the same environment as terminal-bench

```bash
cd /path/to/server
uv run server.py
```
