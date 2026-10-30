"""Unit tests for cold snapshots that need no Docker (docker calls are stubbed)."""

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

    assert calls == [("rmi", name) for name in snapshot.image_names]
    assert snapshot.snapshot_id not in store.snapshots
    assert not snapshot.task_dir.exists()


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
        "commit", "--no-pause",
        "--change", "LABEL com.docker.compose.project=tvcache-snapshot",
        "task_rid_test_container", "task_abc_test_container",
    )]
