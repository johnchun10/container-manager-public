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

To use snapshots after the manager, or the node holding its image store, is
gone:

    save(directory)   write the records (snapshots.json), every snapshot's
                      images (images-NNN.tar, at most IMAGES_PER_ARCHIVE in
                      each) and task dir (task_dirs/) to the directory
    load(directory)   in another manager: load the images and take the records

A saved snapshot can be deleted here and loaded again later: load puts back a
task dir that is gone, at the path it had.
"""

import io
import json
import posixpath
import shutil
import re
import subprocess
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock, Semaphore
from typing import Iterator

import yaml

from rlhf_server.fork.fork_pipeline import ForkPipeline
from rlhf_server.fork.fork_utils import get_all_container_by_id
from rlhf_server.networks.network_utils import does_container_need_bridge_network
from terminal_bench.handlers.trial_handler import TaskPaths, TrialPaths
from terminal_bench.terminal import Terminal
from terminal_bench.terminal.container_cli import container_cli, is_docker

MAX_PARALLELISM = 20
MAX_COMMIT_ATTEMPTS = 3
EXIT_SCRIPT_TIMEOUT_SEC = 30
SNAPSHOT_PROJECT_LABEL = "tvcache-snapshot"
RECORDS_FILE = "snapshots.json"
# Images are saved IMAGES_PER_ARCHIVE to an archive (images-000.tar, ...):
# podman cannot load an archive whose list of images (manifest.json) is over
# 1 MiB, which one archive of a run's 2,704 snapshots was. An archive that is
# over it anyway (images.tar, from before this) is split when it is loaded,
# into parts whose lists are at most MANIFEST_BYTES.
IMAGES_PER_ARCHIVE = 100
MANIFEST_BYTES = 512 * 1024
MANIFEST_TOO_LARGE = "exceeded maximum allowed size"
TASK_DIRS = "task_dirs"


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
    # Bytes the snapshot adds: the top (committed) layer of each image. The
    # base layers are shared with the task image and other snapshots.
    layer_bytes: int = 0


def _archives(directory: Path) -> list[Path]:
    """The image archives in a saved directory: images-NNN.tar, or the one
    images.tar of an older save."""
    return sorted(directory.glob("images*.tar"))


def _image_groups(snapshots: list["Snapshot"], per_archive: int) -> list[list[str]]:
    """The snapshots' images, each once, in groups of at most `per_archive`
    (more only when one snapshot alone has more). Snapshots of one task are
    next to each other, so the task image they share is in few archives."""
    groups: list[list[str]] = [[]]
    seen: set[str] = set()
    for snapshot in sorted(snapshots, key=lambda s: (s.task_name, s.snapshot_id)):
        new = [name for name in dict.fromkeys(snapshot.image_names) if name not in seen]
        seen.update(new)
        if groups[-1] and len(groups[-1]) + len(new) > per_archive:
            groups.append([])
        groups[-1] += new
    return [group for group in groups if group]


def _split_archive(archive: Path, out_dir: Path, manifest_bytes: int = MANIFEST_BYTES,
                   per_archive: int = IMAGES_PER_ARCHIVE) -> Iterator[Path]:
    """Writes a docker-archive of many images as several smaller ones in
    `out_dir`, each with part of the list of images (manifest.json) and the
    config and layer files those images name. The archive itself is left as
    it is. Yields each part once it is written, so the caller can load and
    delete one before the next takes its space."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        members = {posixpath.normpath(member.name): member for member in tar}
        manifest = json.load(tar.extractfile(members["manifest.json"]))
        groups, size = [[]], 0
        for image in manifest:
            entry_bytes = len(json.dumps(image))
            if groups[-1] and (len(groups[-1]) >= per_archive or size + entry_bytes > manifest_bytes):
                groups.append([])
                size = 0
            groups[-1].append(image)
            size += entry_bytes
        for number, group in enumerate(groups):
            if not group:
                continue
            part = out_dir / f"{archive.stem}-part{number:03d}.tar"
            with tarfile.open(part, "w") as out:
                data = json.dumps(group).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
                for name in dict.fromkeys(name for image in group for name in [image["Config"], *image["Layers"]]):
                    member = members[posixpath.normpath(name)]
                    for _ in range(8):  # a layer can be a link to the file that holds it
                        if member.issym():
                            member = members[posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))]
                        elif member.islnk():
                            member = members[posixpath.normpath(member.linkname)]
                        else:
                            break
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = member.size, member.mode, member.mtime
                    out.addfile(info, tar.extractfile(member))
            yield part


def _load_archive(archive: Path) -> None:
    """Loads an image archive into the image store. One that podman refuses
    because its list of images is too long is split and loaded in parts."""
    result = _docker("load", "-i", str(archive))
    if result.returncode == 0:
        return
    if MANIFEST_TOO_LARGE not in result.stderr:
        raise RuntimeError(f"loading {archive} failed: {result.stderr.strip()}")
    parts_dir = archive.with_name(f"{archive.stem}.parts")
    try:
        print(f"{archive} lists too many images to load at once; loading it in parts")
        for part in _split_archive(archive, parts_dir):
            result = _docker("load", "-i", str(part))
            if result.returncode != 0:
                raise RuntimeError(f"loading {part} (a part of {archive}) failed: {result.stderr.strip()}")
            part.unlink()
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)


def _layer_bytes(image_names: list[str]) -> int:
    """Sums the size of each image's newest layer (what the commit added);
    0 for an image whose history can't be read."""
    total = 0
    for image in image_names:
        # docker honors --human=false (bytes); podman prints e.g. "22kB" anyway.
        result = _docker("history", "--human=false", "--format", "{{.Size}}", image)
        lines = result.stdout.splitlines()
        if result.returncode == 0 and lines:
            total += _parse_size(lines[0])
    return total


_SIZE_UNITS = {"": 1, "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
               "kib": 2**10, "mib": 2**20, "gib": 2**30}


def _parse_size(text: str) -> int:
    """'22kB', '1.5 MB', '4096' -> bytes (0 if unreadable)."""
    match = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", text)
    if not match:
        return 0
    return int(float(match.group(1)) * _SIZE_UNITS.get(match.group(2).lower(), 0))


def retag_compose(compose: dict, old_token: str, new_token: str) -> dict:
    """Points every service image that carries old_token at the new_token tag."""
    for service in compose.get("services", {}).values():
        image = service.get("image")
        if isinstance(image, str) and old_token in image:
            service["image"] = image.replace(old_token, new_token)
        service.pop("build", None)
    return compose


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*container_cli(), *args], capture_output=True, text=True)


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
            "commit", "--pause=false",
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


def _image_ids(image_names: list[str]) -> list[str]:
    """The image ids behind these names (names that no longer exist are skipped)."""
    ids = []
    for image in image_names:
        result = _docker("image", "inspect", "--format", "{{.Id}}", image)
        if result.returncode == 0 and result.stdout.strip():
            ids.append(result.stdout.strip())
    return ids


def _remove_untagged(image_ids: list[str]) -> None:
    """Removes each image that is left with no tag at all.

    Untagging a snapshot's last name normally deletes its image, but under
    Docker's containerd image store a snapshot committed from a restored
    snapshot (a chain) can survive as an untagged `<none>` image, which
    leaks its disk. Images some restore still holds a tag on are kept."""
    for image_id in image_ids:
        tags = _docker("image", "inspect", "--format", "{{len .RepoTags}}", image_id)
        if tags.returncode == 0 and tags.stdout.strip() == "0":
            _docker("rmi", image_id)


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
            layer_bytes=_layer_bytes(image_names),
        )
        with self.snapshots_lock:
            self.snapshots[snapshot.snapshot_id] = snapshot
        print(f"Snapshot {snapshot.snapshot_id} of {terminal_id} took {time.perf_counter() - st:.2f}s, "
              f"{snapshot.layer_bytes} bytes")
        return snapshot.snapshot_id

    def layer_bytes(self, snapshot_id: str) -> int:
        with self.snapshots_lock:
            snapshot = self.snapshots.get(snapshot_id)
        return snapshot.layer_bytes if snapshot else 0

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
            image_ids = _image_ids(snapshot.image_names)
            errors = _remove_images(snapshot.image_names)
            if errors:
                # Keep the record so the caller can retry.
                raise RuntimeError(f"docker rmi failed: {'; '.join(errors)}")
            _remove_untagged(image_ids)
            del self.snapshots[snapshot_id]
        shutil.rmtree(snapshot.task_dir, ignore_errors=True)

    def save(self, directory: Path, snapshot_ids: list[str] | None = None) -> dict:
        """Writes the snapshots (all of them, or the listed ones) to
        `directory`: their records, their images in archives of at most
        IMAGES_PER_ARCHIVE and a copy of each task dir. The snapshots stay usable here. Holds the snapshot
        lock throughout, so nothing is restored or deleted halfway through.

        Returns {"snapshots", "images", "bytes" (the archives'), "unknown"
        (listed ids this manager does not have)}."""
        directory = Path(directory)
        with self.snapshots_lock:
            if snapshot_ids is None:
                chosen, unknown = list(self.snapshots.values()), []
            else:
                chosen = [self.snapshots[i] for i in dict.fromkeys(snapshot_ids) if i in self.snapshots]
                unknown = [i for i in dict.fromkeys(snapshot_ids) if i not in self.snapshots]
            st = time.perf_counter()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / RECORDS_FILE).unlink(missing_ok=True)  # not whole again until the end
            for old in _archives(directory):
                old.unlink()
            groups = _image_groups(chosen, IMAGES_PER_ARCHIVE)
            images = [name for group in groups for name in group]
            for number, group in enumerate(groups):
                archive = directory / f"images-{number:03d}.tar"
                partial = archive.with_name(f"{archive.name}.partial")
                # The images of an archive share layers (the task image under
                # each snapshot), written once per archive. podman takes
                # several images only with --multi-image-archive.
                many = [] if is_docker() else ["--multi-image-archive"]
                result = _docker("save", *many, "-o", str(partial), *group)
                if result.returncode != 0:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError(f"saving {len(group)} images failed: {result.stderr.strip()}")
                partial.replace(archive)
            shutil.rmtree(directory / TASK_DIRS, ignore_errors=True)
            for snapshot in chosen:
                shutil.copytree(snapshot.task_dir, directory / TASK_DIRS / snapshot.snapshot_id)
            records = [{**asdict(snapshot), "task_dir": str(Path(snapshot.task_dir).resolve())} for snapshot in chosen]
            partial = directory / f"{RECORDS_FILE}.partial"
            partial.write_text(json.dumps({"snapshots": records}))
            partial.replace(directory / RECORDS_FILE)  # written last: its presence means the save is whole
        size = sum(archive.stat().st_size for archive in _archives(directory))
        print(f"Saved {len(chosen)} snapshots ({len(images)} images, {size} bytes) to {directory} "
              f"in {time.perf_counter() - st:.2f}s")
        return {"snapshots": len(chosen), "images": len(images), "bytes": size, "unknown": unknown}

    def load(self, directory: Path) -> dict:
        """Loads what save() wrote: the images into this node's image store
        and the records into this manager. A task dir that no longer exists is
        copied back to the path it had. A snapshot whose images or task dir
        are not there afterwards is left out.

        Returns {"snapshots" (now restorable), "missing" (ids left out)}."""
        directory = Path(directory)
        records_path = directory / RECORDS_FILE
        if not records_path.exists():
            raise FileNotFoundError(f"no {RECORDS_FILE} in {directory}")
        st = time.perf_counter()
        records = json.loads(records_path.read_text())["snapshots"]
        for archive in _archives(directory):
            _load_archive(archive)
        loaded, missing = [], []
        for record in records:
            snapshot = Snapshot(**{**record, "task_dir": Path(record["task_dir"])})
            saved_task_dir = directory / TASK_DIRS / snapshot.snapshot_id
            if not snapshot.task_dir.is_dir() and saved_task_dir.is_dir():
                try:
                    shutil.copytree(saved_task_dir, snapshot.task_dir)
                except OSError as error:
                    print(f"Could not put back {snapshot.task_dir}: {error}")
            if not snapshot.task_dir.is_dir() or len(_image_ids(snapshot.image_names)) != len(snapshot.image_names):
                missing.append(snapshot.snapshot_id)
                continue
            with self.snapshots_lock:
                self.snapshots[snapshot.snapshot_id] = snapshot
            loaded.append(snapshot.snapshot_id)
        print(f"Loaded {len(loaded)} snapshots from {directory} in {time.perf_counter() - st:.2f}s"
              + (f"; left out {len(missing)} with no images or task dir" if missing else ""))
        return {"snapshots": len(loaded), "missing": missing}
