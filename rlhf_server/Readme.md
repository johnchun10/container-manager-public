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

## Cold snapshots

`/fork` returns a running copy and deletes the images it committed. These
endpoints keep the images instead, so a saved state can be restored many times
without keeping a container running for it:

| Endpoint | Request | Response |
|---|---|---|
| `POST /snapshot` | `{"id": terminal_id}` | `{"snapshot_id": ...}`; 404 for an unknown terminal |
| `POST /restore` | `{"snapshot_id": ...}` | `{"id": new_terminal_id}`; 404 for an unknown snapshot |
| `POST /delete_snapshot` | `{"snapshot_id": ...}` | `{"status": "deleted"}`; 404 for an unknown snapshot |
| `POST /save_snapshots` | `{"dir": path, "snapshot_ids": [...]}` (ids optional: all) | `{"snapshots": n, "images": n, "bytes": n, "unknown": [...]}` |
| `POST /load_snapshots` | `{"dir": path}` | `{"snapshots": n, "missing": [...]}`; 404 if nothing was saved there |

A snapshot holds what `docker commit` captures (each container's filesystem,
not volumes or processes) plus, with `ENABLE_CACHING=1`, the cwd and exported
variables saved by tmux-resurrect. The terminal keeps running. Each restore is
independent of the others and of the snapshot. Snapshot records are kept in
memory, so a server restart forgets them but leaves their images.

`/save_snapshots` writes the records (`snapshots.json`), one archive of the
snapshots' images (`images.tar`) and their task directory copies
(`task_dirs/`) to a directory, and `/load_snapshots` reads them into another
manager, for example on another node after the first one's image store is
gone. A saved snapshot can be deleted and loaded again later. Both calls take
as long as the container CLI's `save` and `load` do: minutes for many
gigabytes.
Implementation: `snapshot/snapshot_store.py`; tests: `tests/unit/snapshots`.
