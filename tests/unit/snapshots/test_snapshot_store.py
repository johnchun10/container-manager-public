"""Unit tests for cold snapshots that need no Docker (docker calls are stubbed)."""

import json
import shutil
import subprocess
from pathlib import Path
from threading import Lock

import pytest

from rlhf_server.snapshot import snapshot_store
from rlhf_server.snapshot.snapshot_store import (
    Snapshot,
    SnapshotNotFound,
    SnapshotStore,
    TerminalNotFound,
    retag_compose,
)


class FakePipeline:
    def __init__(self):
        self.state_lock = Lock()
        self.terminals = {}
        self.sessions = {}


def make_snapshot(tmp_path: Path) -> Snapshot:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "docker-compose.yaml").write_text("services: {}")
    return Snapshot(
        snapshot_id="snap_abc",
        token="abc",
        image_names=["task_abc_test_container", "task_abc_test_docker-db-1"],
        task_dir=task_dir,
        client_image_name="task_rid_test",
        parent_rollout_id="rid",
        task_name="task",
        pane_log="",
    )


def stub_docker(monkeypatch, results: dict[str, subprocess.CompletedProcess]):
    calls = []

    def fake(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        return results.get(args[-1], subprocess.CompletedProcess(args, 0, "", ""))

    monkeypatch.setattr(snapshot_store, "_docker", fake)
    return calls


def test_retag_compose_points_services_at_restore_tags_and_drops_builds():
    compose = {
        "services": {
            "client": {"image": "task_abc_test_container", "build": {"dockerfile": "Dockerfile"}},
            "db": {"image": "task_abc_test_docker-db-1"},
            "external": {"image": "postgres:16"},
        }
    }
    retag_compose(compose, "abc", "xyz")
    assert compose["services"]["client"] == {"image": "task_xyz_test_container"}
    assert compose["services"]["db"]["image"] == "task_xyz_test_docker-db-1"
    assert compose["services"]["external"]["image"] == "postgres:16"


def test_unknown_ids_raise_not_found():
    store = SnapshotStore(FakePipeline())
    with pytest.raises(SnapshotNotFound):
        store.restore("snap_missing")
    with pytest.raises(SnapshotNotFound):
        store.delete("snap_missing")
    with pytest.raises(TerminalNotFound):
        store.snapshot("term_missing")


def test_delete_removes_images_and_task_dir(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    calls = stub_docker(monkeypatch, {})

    store.delete(snapshot.snapshot_id)

    assert [c for c in calls if c[0] == "rmi"] == [("rmi", name) for name in snapshot.image_names]
    assert snapshot.snapshot_id not in store.snapshots
    assert not snapshot.task_dir.exists()


def fake_images(monkeypatch, images: dict[str, list[str]]):
    """A tiny image store: image id -> tags. Untagging a chained snapshot's last
    name leaves its image behind untagged, as Docker's containerd store does."""
    calls = []

    def fake(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        if args[:2] == ("image", "inspect"):
            ref, fmt = args[-1], args[3]
            image_id = ref if ref in images else next((i for i, t in images.items() if ref in t), None)
            if image_id is None:
                return subprocess.CompletedProcess(args, 1, "", f"Error: No such image: {ref}")
            return ok(image_id if fmt == "{{.Id}}" else str(len(images[image_id])))
        if args[0] == "rmi":
            ref = args[1]
            if ref in images:
                del images[ref]
                return ok()
            for tags in images.values():
                if ref in tags:
                    tags.remove(ref)  # untag only: the image stays
                    return ok()
            return subprocess.CompletedProcess(args, 1, "", f"Error: No such image: {ref}")
        return ok()

    monkeypatch.setattr(snapshot_store, "_docker", fake)
    return calls


def test_delete_removes_a_chained_snapshot_left_untagged(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    images = {f"sha256:{i}": [name] for i, name in enumerate(snapshot.image_names)}
    fake_images(monkeypatch, images)

    store.delete(snapshot.snapshot_id)

    assert images == {}


def test_delete_keeps_an_image_a_restore_still_tags(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    images = {f"sha256:{i}": [name] for i, name in enumerate(snapshot.image_names)}
    images["sha256:0"].append("task_restore_test_container")
    fake_images(monkeypatch, images)

    store.delete(snapshot.snapshot_id)

    assert images == {"sha256:0": ["task_restore_test_container"]}


def test_delete_treats_already_removed_images_as_removed(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    gone = subprocess.CompletedProcess((), 1, "", "Error: No such image: task_abc_test_container")
    stub_docker(monkeypatch, {"task_abc_test_container": gone})

    store.delete(snapshot.snapshot_id)

    assert snapshot.snapshot_id not in store.snapshots


def test_failed_delete_keeps_the_record_for_a_retry(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    busy = subprocess.CompletedProcess((), 1, "", "Error: conflict: unable to remove repository reference")
    stub_docker(monkeypatch, {"task_abc_test_docker-db-1": busy})

    with pytest.raises(RuntimeError, match="conflict"):
        store.delete(snapshot.snapshot_id)

    assert snapshot.snapshot_id in store.snapshots
    assert snapshot.task_dir.exists()


def test_snapshot_commit_relabels_the_compose_project(monkeypatch):
    # Without this, `docker compose down --rmi all` on the snapshotted terminal
    # deletes the snapshot, because commit copies the container's labels.
    calls = stub_docker(monkeypatch, {})
    snapshot_store._commit("task_rid_test_container", "task_abc_test_container")
    assert calls == [(
        "commit", "--pause=false",
        "--change", "LABEL com.docker.compose.project=tvcache-snapshot",
        "task_rid_test_container", "task_abc_test_container",
    )]


def stub_saving_docker(monkeypatch):
    """Every call succeeds, `save` writes its archive and every image exists."""
    calls = []

    def fake(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "save":
            Path(args[args.index("-o") + 1]).write_bytes(b"layers")
        out = "sha256:1" if args[:2] == ("image", "inspect") else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(snapshot_store, "_docker", fake)
    return calls


def test_save_then_load_moves_snapshots_to_another_manager(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    calls = stub_saving_docker(monkeypatch)
    monkeypatch.setenv("CONTAINER_CLI", "podman-hpc")
    out = tmp_path / "saved"

    saved = store.save(out, ["snap_abc", "snap_gone"])

    assert saved == {"snapshots": 1, "images": 2, "bytes": 6, "unknown": ["snap_gone"]}
    # Few images: one archive; podman needs the flag to take more than one.
    assert calls == [("save", "--multi-image-archive", "-o", str(out / "images-000.tar.partial"), *snapshot.image_names)]
    assert sorted(p.name for p in out.iterdir()) == ["images-000.tar", "snapshots.json", "task_dirs"]
    assert (out / "task_dirs" / "snap_abc" / "docker-compose.yaml").read_text() == "services: {}"
    assert snapshot.snapshot_id in store.snapshots  # still usable here

    # The first manager deletes the snapshot (as a run does when it ends); the
    # saved copy still loads, task dir included.
    store.delete(snapshot.snapshot_id)
    assert not snapshot.task_dir.exists()
    calls.clear()
    other = SnapshotStore(FakePipeline())
    assert other.load(out) == {"snapshots": 1, "missing": []}
    assert calls[0] == ("load", "-i", str(out / "images-000.tar"))
    assert other.snapshots == {snapshot.snapshot_id: snapshot}  # the same record
    assert (snapshot.task_dir / "docker-compose.yaml").read_text() == "services: {}"


def test_save_with_docker_and_no_ids_saves_everything(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    calls = stub_saving_docker(monkeypatch)
    monkeypatch.delenv("CONTAINER_CLI", raising=False)

    saved = store.save(tmp_path / "saved")

    assert (saved["snapshots"], saved["unknown"]) == (1, [])
    assert calls[0][:2] == ("save", "-o")  # docker saves several images without a flag


def test_failed_save_leaves_no_records_file(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    full = subprocess.CompletedProcess((), 1, "", "Error: no space left on device")
    stub_docker(monkeypatch, {snapshot.image_names[-1]: full})

    with pytest.raises(RuntimeError, match="no space left"):
        store.save(tmp_path / "saved")

    assert not (tmp_path / "saved" / "snapshots.json").exists()  # a loader sees no half-written save


def test_load_leaves_out_snapshots_whose_images_or_task_dir_are_gone(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    kept = make_snapshot(tmp_path)
    no_dir = Snapshot(**{**kept.__dict__, "snapshot_id": "snap_nodir", "task_dir": tmp_path / "other"})
    no_image = Snapshot(**{**kept.__dict__, "snapshot_id": "snap_noimage", "image_names": ["gone_image"]})
    no_dir.task_dir.mkdir()
    for snapshot in (kept, no_dir, no_image):
        store.snapshots[snapshot.snapshot_id] = snapshot
    stub_saving_docker(monkeypatch)
    store.save(tmp_path / "saved")
    # Neither the task dir nor its saved copy is there any more.
    shutil.rmtree(no_dir.task_dir)
    shutil.rmtree(tmp_path / "saved" / "task_dirs" / "snap_nodir")

    fake_images(monkeypatch, {"sha256:1": list(kept.image_names)})
    other = SnapshotStore(FakePipeline())
    assert other.load(tmp_path / "saved") == {"snapshots": 1, "missing": ["snap_nodir", "snap_noimage"]}
    assert list(other.snapshots) == ["snap_abc"]

    with pytest.raises(FileNotFoundError):
        other.load(tmp_path / "nothing-saved-here")


def test_save_writes_at_most_100_images_to_an_archive(monkeypatch, tmp_path):
    """podman cannot load an archive that lists too many images (a run's 2,704
    snapshots in one did not load), so the images go into several, with one
    task's snapshots next to each other."""
    store = SnapshotStore(FakePipeline())
    base = make_snapshot(tmp_path)
    for number in range(130):
        task = "task_b" if number % 2 else "task_a"
        snapshot = Snapshot(**{**base.__dict__, "snapshot_id": f"snap_{number:03d}", "task_name": task,
                               "image_names": [f"{task}_{number:03d}_client", f"{task}_{number:03d}_db"]})
        store.snapshots[snapshot.snapshot_id] = snapshot
    calls = stub_saving_docker(monkeypatch)
    monkeypatch.delenv("CONTAINER_CLI", raising=False)
    out = tmp_path / "saved"
    (out / "task_dirs").mkdir(parents=True)
    (out / "images.tar").write_bytes(b"an older save")  # replaced, not loaded next to the new ones

    saved = store.save(out)

    groups = [call[3:] for call in calls if call[0] == "save"]
    assert [len(group) for group in groups] == [100, 100, 60] and saved["images"] == 260
    assert len({name for group in groups for name in group}) == 260  # each image once
    assert all(name.startswith("task_a") for name in groups[0])     # a task's images together
    assert sorted(p.name for p in out.glob("images*")) == ["images-000.tar", "images-001.tar", "images-002.tar"]
    assert saved["bytes"] == 3 * len(b"layers")

    calls.clear()
    SnapshotStore(FakePipeline()).load(out)
    assert [call[2] for call in calls if call[0] == "load"] == [str(out / f"images-00{n}.tar") for n in range(3)]


def make_archive(path, images):
    """A docker-archive as podman writes one: manifest.json, a config per
    image and the layers, the shared base layer once. The last image's own
    layer is named through a link."""
    import io
    import tarfile

    def add(tar, name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    manifest = []
    with tarfile.open(path, "w") as tar:
        add(tar, "base.tar", b"base layer")
        for number in range(images):
            add(tar, f"config{number}.json", f"config {number}".encode())
            add(tar, f"layer{number}.tar", f"layer {number}".encode())
            layer = f"layer{number}.tar"
            if number == images - 1:
                link = tarfile.TarInfo(f"legacy{number}/layer.tar")
                link.type, link.linkname = tarfile.SYMTYPE, f"../layer{number}.tar"
                tar.addfile(link)
                layer = link.name
            manifest.append({"Config": f"config{number}.json", "RepoTags": [f"image{number}:latest"], "Layers": ["base.tar", layer]})
        add(tar, "manifest.json", json.dumps(manifest).encode())  # podman writes it last
    return manifest


def read_archive(path):
    import tarfile

    with tarfile.open(path) as tar:
        return {member.name: tar.extractfile(member).read() for member in tar}


def test_split_archive_gives_each_part_its_images_configs_and_layers(tmp_path):
    manifest = make_archive(tmp_path / "images.tar", images=5)

    parts = list(snapshot_store._split_archive(tmp_path / "images.tar", tmp_path / "parts", per_archive=2))

    assert [part.name for part in parts] == ["images-part000.tar", "images-part001.tar", "images-part002.tar"]
    contents = [read_archive(part) for part in parts]
    assert [json.loads(content["manifest.json"]) for content in contents] == [manifest[0:2], manifest[2:4], manifest[4:5]]
    assert contents[0] == {"manifest.json": contents[0]["manifest.json"], "base.tar": b"base layer",
                           "config0.json": b"config 0", "layer0.tar": b"layer 0",
                           "config1.json": b"config 1", "layer1.tar": b"layer 1"}
    # The shared layer is in every part; a layer named through a link is the file itself there.
    assert all(content["base.tar"] == b"base layer" for content in contents)
    assert contents[2]["legacy4/layer.tar"] == b"layer 4"
    # The size of the list of images splits it too.
    by_size = list(snapshot_store._split_archive(tmp_path / "images.tar", tmp_path / "by_size", manifest_bytes=1))
    assert len(by_size) == 5
    assert (tmp_path / "images.tar").exists()  # the archive itself is left alone


def test_load_splits_an_archive_that_lists_too_many_images(monkeypatch, tmp_path):
    """An images.tar from before archives were limited: podman refuses it,
    so it is loaded as parts, which are removed afterwards."""
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    stub_saving_docker(monkeypatch)
    out = tmp_path / "saved"
    store.save(out)
    (out / "images-000.tar").unlink()
    make_archive(out / "images.tar", images=5)
    monkeypatch.setattr(snapshot_store, "IMAGES_PER_ARCHIVE", 2)
    loaded = []

    def fake(*args: str) -> subprocess.CompletedProcess:
        if args[0] == "load":
            if Path(args[2]).name == "images.tar":
                return subprocess.CompletedProcess(args, 125, "", "Error: payload does not match any of the supported image formats:\n"
                                                   " * docker-archive: exceeded maximum allowed size of 1048576 bytes")
            loaded.append((Path(args[2]).name, [image["RepoTags"][0] for image in json.loads(read_archive(args[2])["manifest.json"])]))
        return subprocess.CompletedProcess(args, 0, "sha256:1" if args[:2] == ("image", "inspect") else "", "")

    monkeypatch.setattr(snapshot_store, "_docker", fake)
    monkeypatch.setattr(snapshot_store, "_split_archive",
                        lambda archive, out_dir, split=snapshot_store._split_archive: split(archive, out_dir, per_archive=2))

    assert SnapshotStore(FakePipeline()).load(out) == {"snapshots": 1, "missing": []}

    assert loaded == [("images-part000.tar", ["image0:latest", "image1:latest"]),
                      ("images-part001.tar", ["image2:latest", "image3:latest"]),
                      ("images-part002.tar", ["image4:latest"])]
    assert sorted(p.name for p in out.iterdir()) == ["images.tar", "snapshots.json", "task_dirs"]  # parts gone, archive kept


def test_load_reports_any_other_failure_of_an_archive(monkeypatch, tmp_path):
    store = SnapshotStore(FakePipeline())
    snapshot = make_snapshot(tmp_path)
    store.snapshots[snapshot.snapshot_id] = snapshot
    stub_saving_docker(monkeypatch)
    store.save(tmp_path / "saved")
    stub_docker(monkeypatch, {str(tmp_path / "saved" / "images-000.tar"): subprocess.CompletedProcess((), 125, "", "Error: no space left on device")})

    with pytest.raises(RuntimeError, match="images-000.tar failed: Error: no space left"):
        SnapshotStore(FakePipeline()).load(tmp_path / "saved")
