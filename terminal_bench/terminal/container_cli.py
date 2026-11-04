"""Which container CLI the manager drives.

Docker by default. Set these to use another Docker-compatible CLI, e.g.
podman-hpc on NERSC Perlmutter, which has no Docker daemon:

  CONTAINER_CLI  the CLI ("docker" by default), e.g. "podman-hpc"
  COMPOSE_CMD    the compose command ("<CONTAINER_CLI> compose" by default),
                 e.g. "docker-compose" pointed at podman's socket via DOCKER_HOST

The Docker SDK calls (docker.from_env()) follow DOCKER_HOST as usual.
"""

import os
import shlex


def container_cli() -> list[str]:
    return shlex.split(os.environ.get("CONTAINER_CLI", "docker"))


def compose_cmd() -> list[str]:
    value = os.environ.get("COMPOSE_CMD")
    return shlex.split(value) if value else container_cli() + ["compose"]


def is_docker() -> bool:
    return container_cli() == ["docker"]
