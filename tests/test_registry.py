import json
import os
import time

from conductor.registry import (
    PodRecord, add_pod, get_pod, list_pods, load_registry,
    remove_pod, save_registry, update_pod, REGISTRY_FILE,
)


def test_round_trip(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    pods = [
        PodRecord(pod_id="abc123", name="my-pod", gpu_type="RTX 4090",
                  gpu_cost_per_hour=0.74, status="running", created_at=time.time()),
        PodRecord(pod_id="def456", name="other", status="terminated"),
    ]
    save_registry(pods)
    loaded = load_registry()
    assert len(loaded) == 2
    assert loaded[0].pod_id == "abc123"
    assert loaded[0].name == "my-pod"
    assert loaded[0].gpu_cost_per_hour == 0.74
    assert loaded[1].status == "terminated"


def test_empty_registry(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)
    assert load_registry() == []


def test_add_pod(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    r = PodRecord(pod_id="abc123", name="pod-a")
    add_pod(r)
    assert len(load_registry()) == 1

    # Adding same pod_id replaces
    r2 = PodRecord(pod_id="abc123", name="pod-a-updated")
    add_pod(r2)
    pods = load_registry()
    assert len(pods) == 1
    assert pods[0].name == "pod-a-updated"


def test_get_pod_by_name(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    add_pod(PodRecord(pod_id="abc123", name="my-pod"))
    add_pod(PodRecord(pod_id="def456", name="other"))

    assert get_pod("my-pod").pod_id == "abc123"
    assert get_pod("other").pod_id == "def456"


def test_get_pod_by_id_prefix(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    add_pod(PodRecord(pod_id="abc123xyz", name="pod-a"))
    assert get_pod("abc12").pod_id == "abc123xyz"


def test_get_pod_not_found(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)
    assert get_pod("nonexistent") is None


def test_update_pod(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    add_pod(PodRecord(pod_id="abc123", name="pod-a", status="running"))
    updated = update_pod("abc123", status="terminated", cost_usd=1.50)
    assert updated.status == "terminated"
    assert updated.cost_usd == 1.50

    reloaded = get_pod("abc123")
    assert reloaded.status == "terminated"


def test_update_pod_not_found(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)
    assert update_pod("nonexistent", status="terminated") is None


def test_remove_pod(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    add_pod(PodRecord(pod_id="abc123", name="pod-a"))
    add_pod(PodRecord(pod_id="def456", name="pod-b"))

    assert remove_pod("abc123") is True
    assert len(load_registry()) == 1
    assert remove_pod("abc123") is False


def test_list_pods_filter(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    add_pod(PodRecord(pod_id="a", name="running-pod", status="running"))
    add_pod(PodRecord(pod_id="b", name="dead-pod", status="terminated"))

    assert len(list_pods()) == 2
    assert len(list_pods(status="running")) == 1
    assert list_pods(status="running")[0].name == "running-pod"
    assert len(list_pods(status="terminated")) == 1


def test_atomic_write(tmp_dir, monkeypatch):
    reg_file = os.path.join(tmp_dir, "pods.json")
    monkeypatch.setattr("conductor.registry.REGISTRY_DIR", tmp_dir)
    monkeypatch.setattr("conductor.registry.REGISTRY_FILE", reg_file)

    save_registry([PodRecord(pod_id="x", name="test")])
    files = [f for f in os.listdir(tmp_dir) if not f.startswith(".")]
    assert files == ["pods.json"]
