"""Cold snapshots: committed images that outlive the container they came from.

/fork commits a terminal's containers, starts a child, and then deletes the
committed images, so the only reusable thing it leaves is a running container.
A snapshot keeps the committed images instead and starts nothing:

    snapshot(terminal_id) -> snapshot_id   commit images + copied task dir
    restore(snapshot_id)  -> terminal_id   new running terminal from the images
    delete(snapshot_id)                    remove the images and task dir

One snapshot can be restored any number of times. Each restore tags the
snapshot's images under its own name and starts from that tag, because
stopping a terminal runs `docker compose down --rmi all`: the restore's stop
then removes only its own tag, never the snapshot's. For the same reason the
snapshot images get a compose project label of their own (see _commit).

What a snapshot holds is what `docker commit` captures (each container's
filesystem, not volumes or processes) plus, with ENABLE_CACHING=1, the cwd and
exported variables that tmux-resurrect's exit.sh writes into the filesystem.
Snapshot records live in memory, like terminals; a manager restart loses the
records but leaves the images.
"""

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Semaphore

import yaml

from rlhf_server.fork.fork_pipeline import ForkPipeline
from rlhf_server.fork.fork_utils import get_all_container_by_id
from rlhf_server.networks.network_utils import does_container_need_bridge_network
from terminal_bench.handlers.trial_handler import TaskPaths, TrialPaths
from terminal_bench.terminal import Terminal

MAX_PARALLELISM = 20
MAX_COMMIT_ATTEMPTS = 3
EXIT_SCRIPT_TIMEOUT_SEC = 30
SNAPSHOT_PROJECT_LABEL = "tvcache-snapshot"


class SnapshotNotFound(Exception):
    pass


class TerminalNotFound(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    # Unique token in every image name, replaced by a restore's own token.
    token: str
    image_names: list[str]
    task_dir: Path
    # Parent's client image name and rollout id; a restore's names are this
    # name with the rollout id replaced, the same scheme /fork uses.
    client_image_name: str
    parent_rollout_id: str
    task_name: str
    pane_log: str


def retag_compose(compose: dict, old_token: str, new_token: str) -> dict:
    """Points every service image that carries old_token at the new_token tag."""
    for service in compose.get("services", {}).values():
        image = service.get("image")
        if isinstance(image, str) and old_token in image:
            service["image"] = image.replace(old_token, new_token)
        service.pop("build", None)
    return compose


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _commit(container_name: str, image_name: str) -> subprocess.CompletedProcess:
    """Commits like /fork does, but relabels the image's compose project.

    `docker commit` copies the container's labels, including
    com.docker.compose.project. `docker compose down --rmi all` removes every
    image carrying its project's label, so without this, stopping the
    snapshotted terminal would delete the snapshot.
    """
    result = None
    for _ in range(MAX_COMMIT_ATTEMPTS):
        result = _docker(
            "commit", "--no-pause",
            "--change", f"LABEL com.docker.compose.project={SNAPSHOT_PROJECT_LABEL}",
            container_name, image_name,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    return result


def _remove_images(image_names: list[str]) -> list[str]:
    """Untags/removes each image; an image that is already gone counts as removed.
    Returns the errors."""
    errors = []
    for image in image_names:
        result = _docker("rmi", image)
        if result.returncode != 0 and "No such image" not in result.stderr:
            errors.append(result.stderr.strip())
    return errors


class SnapshotStore:
    def __init__(self, fork_pipeline: ForkPipeline):
        # Shares the fork pipeline's state dicts and lock, and reuses its
        # commit and task-dir helpers so snapshots match forks exactly.
        self.fork_pipeline = fork_pipeline
        self.snapshots: dict[str, Snapshot] = {}
        self.snapshots_lock = Lock()
        self.semaphore = Semaphore(MAX_PARALLELISM)

    def snapshot(self, terminal_id: str) -> str:
        pipeline = self.fork_pipeline
        with pipeline.state_lock:
            if terminal_id not in pipeline.terminals or terminal_id not in pipeline.sessions:
                raise TerminalNotFound(terminal_id)
            client_image_name = pipeline.terminals[terminal_id]._compose_manager._client_image_name
            task_name = pipeline.task_paths[terminal_id].input_path.parts[-1]
            rollout_id = pipeline.rollout_env_id[terminal_id]
            session = pipeline.sessions[terminal_id]

        with self.semaphore:
            st = time.perf_counter()
            pane_log = ""
            if pipeline.enable_caching:
                pane_log = pipeline.get_pane_logs_local(terminal_id)
                # Unlike /fork, wait for exit.sh so the commit includes the
                # cwd and environment it saves.
                session.send_keys(
                    [f"{pipeline.container_resurrect_path}/scripts/exit.sh", "Enter"],
                    block=True,
                    max_timeout_sec=EXIT_SCRIPT_TIMEOUT_SEC,
                )

            token = uuid.uuid4().hex
            container_names = get_all_container_by_id(rollout_id)
            if not container_names:
                raise RuntimeError(f"no running containers found for terminal {terminal_id}")
            image_names = [name.replace(rollout_id, token) for name in container_names]
            for container_name, image_name in zip(container_names, image_names):
                result = _commit(container_name, image_name)
                if result.returncode != 0:
                    _remove_images(image_names)
                    raise RuntimeError(f"docker commit failed: {result.stderr}")
            try:
                task_dir = pipeline.create_forked_task_dir(image_names, task_name, terminal_id)
            except Exception:
                _remove_images(image_names)
                raise

        snapshot = Snapshot(
            snapshot_id=f"snap_{token}",
            token=token,
            image_names=image_names,
            task_dir=Path(task_dir),
            client_image_name=client_image_name,
            parent_rollout_id=rollout_id,
            task_name=task_name,
            pane_log=pane_log,
        )
        with self.snapshots_lock:
            self.snapshots[snapshot.snapshot_id] = snapshot
        print(f"Snapshot {snapshot.snapshot_id} of {terminal_id} took {time.perf_counter() - st:.2f}s")
        return snapshot.snapshot_id

    def restore(self, snapshot_id: str) -> str:
        pipeline = self.fork_pipeline
        restore_id = str(uuid.uuid4())
        terminal_id = f"term_{restore_id}"
        restore_token = restore_id.replace("-", "")

        with self.snapshots_lock:
            snapshot = self.snapshots.get(snapshot_id)
            if snapshot is None:
                raise SnapshotNotFound(snapshot_id)
            # Tag while holding the lock so a concurrent delete cannot remove
            # the images first; afterwards the restore's tags protect them.
            restore_images = [image.replace(snapshot.token, restore_token) for image in snapshot.image_names]
            for image, restore_image in zip(snapshot.image_names, restore_images):
                result = _docker("tag", image, restore_image)
                if result.returncode != 0:
                    _remove_images(restore_images)
                    raise RuntimeError(f"docker tag failed: {result.stderr}")

        task_dir = Path(f"{pipeline.path}/{snapshot.task_name}_{restore_id}")
        try:
            self._start_restore(snapshot, terminal_id, restore_id, restore_token, task_dir)
        except Exception:
            # Undo everything a failed restore created; a partially started
            # terminal is stopped by its compose down.
            with pipeline.state_lock:
                terminal = pipeline.terminals.pop(terminal_id, None)
                for table in (pipeline.sessions, pipeline.task_dir_env_id, pipeline.rollout_env_id,
                              pipeline.trial_path, pipeline.task_paths, pipeline.panelog_forked_terminal_dict):
                    table.pop(terminal_id, None)
                network_name = pipeline.terminal_to_network.pop(terminal_id, None)
            if terminal is not None:
                try:
                    terminal.stop()
                except Exception as error:
                    print(f"Failed to stop half-restored terminal {terminal_id}: {error}")
            if network_name is not None:
                pipeline.bridge_networks.put_network(network_name)
            _remove_images(restore_images)
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        return terminal_id

    def _start_restore(
        self, snapshot: Snapshot, terminal_id: str, restore_id: str, restore_token: str, task_dir: Path
    ) -> None:
        pipeline = self.fork_pipeline
        with self.semaphore:
            st = time.perf_counter()
            shutil.copytree(snapshot.task_dir, task_dir)
            compose_path = task_dir / "docker-compose.yaml"
            compose = yaml.safe_load(compose_path.read_text())
            compose_path.write_text(yaml.dump(retag_compose(compose, snapshot.token, restore_token)))

            client_image_name = snapshot.client_image_name.replace(snapshot.parent_rollout_id, restore_id)
            trial_paths = TrialPaths(Path(f"../runs/{client_image_name}"), snapshot.task_name, f"trial_{restore_id}")
            trial_paths.mkdir()

            network_name = None
            if does_container_need_bridge_network(compose_path):
                network_name = pipeline.bridge_networks.get_network()
                with pipeline.state_lock:
                    pipeline.terminal_to_network[terminal_id] = network_name

            terminal = Terminal(
                client_container_name=f"{client_image_name}_container",
                client_image_name=client_image_name,
                docker_image_name_prefix=f"{client_image_name}_docker",
                docker_compose_path=compose_path,
                sessions_logs_path=trial_paths.sessions_path,
                agent_logs_path=trial_paths.agent_logging_dir,
                commands_path=trial_paths.commands_path,
                no_rebuild=False,
                cleanup=True,
                livestream=False,
                disable_recording=True,
                bridge_network_name=network_name,
            )
            with pipeline.state_lock:
                # Registered before start() so a failed start can be undone.
                # task_dir_env_id makes /stop delete the copied task dir.
                pipeline.terminals[terminal_id] = terminal
                pipeline.task_dir_env_id[terminal_id] = str(task_dir)
            terminal.start()
            session = terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)

            with pipeline.state_lock:
                pipeline.rollout_env_id[terminal_id] = restore_id
                pipeline.sessions[terminal_id] = session
                pipeline.trial_path[terminal_id] = trial_paths
                pipeline.task_paths[terminal_id] = TaskPaths(input_path=task_dir)
                pipeline.panelog_forked_terminal_dict[terminal_id] = snapshot.pane_log

            if pipeline.enable_caching:
                session.send_keys(
                    [f"source {pipeline.container_resurrect_path}/scripts/startup.sh", "Enter"],
                    min_timeout_sec=0.1,
                )

        print(f"Restored {snapshot.snapshot_id} as {terminal_id} in {time.perf_counter() - st:.2f}s")

    def delete(self, snapshot_id: str) -> None:
        with self.snapshots_lock:
            snapshot = self.snapshots.get(snapshot_id)
            if snapshot is None:
                raise SnapshotNotFound(snapshot_id)
            # Not forced: running restores hold their own tags, so removing
            # the snapshot's tag never touches their images.
            errors = _remove_images(snapshot.image_names)
            if errors:
                # Keep the record so the caller can retry.
                raise RuntimeError(f"docker rmi failed: {'; '.join(errors)}")
            del self.snapshots[snapshot_id]
        shutil.rmtree(snapshot.task_dir, ignore_errors=True)
