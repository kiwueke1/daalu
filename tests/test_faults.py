"""daalu fault: catalog validity, safety refusals, inject → journal → revert."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from daalu_automation.faults.catalog import load_faults, load_recipes
from daalu_automation.faults.engine import Engine, FaultError, Journal, strip_snapshot
from daalu_automation.faults.model import Fault, FaultFileError, Inventory

SERVER = "https://10.0.0.1:6443"

INV = {
    "environments": {
        "lab": {
            "kind": "cluster",
            "kubeconfig": "/tmp/kc",
            "context": "lab",
            "server": SERVER,
            "targets": {
                "web": {"kind": "deployment", "name": "web", "namespace": "apps", "container": "web"},
                "edge": {"kind": "deployment", "name": "edge", "namespace": "daalu-edge"},
                "n1": {"node": "n1"},
            },
        },
        "fab": {
            "kind": "fabric", "nos": "cumulus", "transport": "containerlab", "lab": "x",
            "devices": ["leaf1", "gpu1"], "device_nos": {"gpu1": "linux"},
            "targets": {"leaf1-swp1": {"device": "leaf1", "interface": "swp1",
                                       "peer_device": "gpu1", "peer_interface": "eth1"}},
        },
        "eos": {"kind": "fabric", "nos": "eos", "transport": "ssh", "devices": ["s1"]},
    }
}

DEPLOY = {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {"name": "web", "namespace": "apps", "uid": "u", "resourceVersion": "9",
                 "generation": 4, "managedFields": [], "creationTimestamp": "x",
                 "annotations": {"deployment.kubernetes.io/revision": "3", "keep": "me"}},
    "spec": {"template": {"spec": {"containers": [{"name": "web", "image": "nginx:1"}]}}},
    "status": {"readyReplicas": 1},
}


class FakeRunner:
    def __init__(self, nodes: int = 2, server: str = SERVER):
        self.calls: list[tuple[list[str], str | None]] = []
        self.nodes = nodes
        self.server = server
        self.fail_on: str | None = None
        self.healthy = False

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        s = " ".join(argv)
        if self.fail_on and self.fail_on in s:
            return 1, "", "boom"
        if "config view" in s:
            return 0, self.server, ""
        if " get nodes" in s:
            return 0, "\n".join(f"node/n{i}" for i in range(self.nodes)), ""
        if " -o json" in s and " get " in s:
            return 0, json.dumps(DEPLOY), ""
        if "rollout status" in s:
            return (0 if self.healthy else 1), "", ""
        if "/mtu" in s:
            return 0, "9216\n", ""
        if "docker inspect" in s:
            return 0, "true\n", ""
        return 0, "", ""


@pytest.fixture
def eng(tmp_path: Path):
    r = FakeRunner()
    e = Engine(Inventory.model_validate(INV), Journal(tmp_path / "j.json"), run=r)
    return e, r


@pytest.fixture(scope="module")
def faults():
    return load_faults()


def test_catalog_and_recipes_load(faults):
    assert len(faults) >= 30
    assert {"kubernetes", "node", "network"} <= {f.group for f in faults.values()}
    recipes = load_recipes(faults=faults)
    assert {"openstack", "fabric", "lab-llm", "demo"} <= {r.group for r in recipes.values()}
    # every recipe's own params validate against its fault
    for r in recipes.values():
        f = faults[r.fault]
        for k, v in r.params.items():
            from daalu_automation.faults.model import validate_value
            validate_value(k, f.params[k], v)


def test_every_fault_is_revertible(faults):
    for f in faults.values():
        assert f.revert or f.self_heals or any(s.snapshot for s in f.inject), f.ref


def test_fault_without_revert_is_rejected():
    with pytest.raises(ValueError, match="revert"):
        Fault.model_validate({"id": "x", "title": "t", "scope": "cluster", "blast": "pod",
                              "inject": [{"kubectl": ["get", "pods"]}]})


def test_step_needs_one_action():
    with pytest.raises(ValueError, match="exactly one"):
        Fault.model_validate({"id": "x", "title": "t", "scope": "cluster", "blast": "pod",
                              "self_heals": True, "inject": [{"kubectl": ["a"], "sleep": 1}]})


def test_shell_injection_in_params_is_refused(eng, faults):
    e, _ = eng
    with pytest.raises(FaultError, match="not a valid"):
        e.plan(faults["kubernetes/bad-image"], "lab", target="web",
               overrides={"image": "nginx; rm -rf /"})
    with pytest.raises(FaultError, match="not a valid iface"):
        e.plan(faults["network/packet-loss"], "fab", target="leaf1-swp1",
               overrides={"interface": "swp1$(reboot)"})


def test_protected_namespace_refused(eng, faults):
    e, _ = eng
    with pytest.raises(FaultError, match="protected"):
        e.plan(faults["kubernetes/bad-image"], "lab", target="edge")


def test_scope_and_nos_checks(eng, faults):
    e, _ = eng
    with pytest.raises(FaultError, match="network fault"):
        e.plan(faults["network/packet-loss"], "lab", overrides={"device": "a", "interface": "b"})
    with pytest.raises(FaultError, match="runs eos"):
        e.plan(faults["network/packet-loss"], "eos", overrides={"device": "s1", "interface": "e1"})
    with pytest.raises(FaultError, match="not in fab"):
        e.plan(faults["network/packet-loss"], "fab", overrides={"device": "nope", "interface": "swp1"})


def test_server_pin_mismatch_refuses(tmp_path, faults):
    r = FakeRunner(server="https://prod.example:6443")
    e = Engine(Inventory.model_validate(INV), Journal(tmp_path / "j.json"), run=r)
    p = e.plan(faults["kubernetes/bad-image"], "lab", target="web")
    with pytest.raises(FaultError, match="Refusing"):
        e.inject(p)
    assert not any("set" in c[0] for c in r.calls)


def test_multi_node_required(tmp_path, faults):
    r = FakeRunner(nodes=1)
    e = Engine(Inventory.model_validate(INV), Journal(tmp_path / "j.json"), run=r)
    p = e.plan(faults["node/kubelet-stop"], "lab", target="n1")
    with pytest.raises(FaultError, match="two nodes"):
        e.inject(p)


def test_inject_snapshot_revert_roundtrip(eng, faults):
    e, r = eng
    p = e.plan(faults["kubernetes/bad-image"], "lab", target="web")
    entry = e.inject(p, ttl="30m")
    assert entry["state"] == "active" and entry["expires_at"]
    set_image = [c for c, _ in r.calls if "set" in c and "image" in c]
    assert set_image and set_image[0][-1] == "web=registry.k8s.io/pause:does-not-exist-9.9.9"
    assert set_image[0][:5] == ["kubectl", "--kubeconfig", "/tmp/kc", "--context", "lab"]
    # revert op is a replace of the stripped snapshot
    op = entry["revert_ops"][0]
    assert op["argv"][-3:] == ["replace", "-f", "-"]
    snap = json.loads(op["stdin"])
    assert "status" not in snap and "resourceVersion" not in snap["metadata"]
    assert snap["metadata"]["annotations"] == {"keep": "me"}
    # health: broken, then healthy → resolved
    assert e.check(entry) is False
    r.healthy = True
    entry = e.journal.get(entry["id"])
    assert e.check(entry) is True and e.journal.get(entry["id"])["resolved_at"]
    done = e.revert(entry["id"])
    assert done["state"] == "reverted"
    assert r.calls[-1][1] == op["stdin"]
    assert e.journal.active() == []


def test_failed_inject_is_rolled_back(eng, faults):
    e, r = eng
    r.fail_on = "set resources"
    p = e.plan(faults["kubernetes/oom-limit"], "lab", target="web")
    with pytest.raises(FaultError, match="partial fault was reverted"):
        e.inject(p)
    assert any(c[0][-3:] == ["replace", "-f", "-"] for c in r.calls)
    assert e.journal.active() == []


def test_capture_feeds_revert(eng, faults):
    e, r = eng
    p = e.plan(faults["network/mtu-mismatch"], "fab", target="leaf1-swp1")
    entry = e.inject(p)
    inj = [c for c, _ in r.calls if "mtu 1500" in " ".join(c)]
    assert inj and inj[0][:4] == ["docker", "exec", "-i", "clab-x-leaf1"]
    assert "ip link set swp1 mtu 9216" in entry["revert_ops"][0]["argv"][-1]


def test_peer_step_targets_peer_device(eng, faults):
    e, r = eng
    p = e.plan(faults["network/cable-pull"], "fab", target="leaf1-swp1")
    e.inject(p)
    devs = [c[3] for c, _ in r.calls if c[:2] == ["docker", "exec"]]
    assert devs == ["clab-x-leaf1", "clab-x-gpu1"]


def test_node_step_uses_privileged_helper_pod(eng, faults):
    e, r = eng
    p = e.plan(faults["node/gpu-persistence-off"], "lab", target="n1")
    e.inject(p)
    run = [c for c, _ in r.calls if "run" in c][0]
    ov = json.loads(next(a for a in run if a.startswith("--overrides=")).split("=", 1)[1])
    assert ov["spec"]["nodeName"] == "n1" and ov["spec"]["hostPID"] is True
    assert ov["spec"]["containers"][0]["command"][-1] == "nvidia-smi -pm 0"


def test_strip_snapshot_drops_runtime_fields():
    s = strip_snapshot(DEPLOY)
    assert set(s["metadata"]) == {"name", "namespace", "annotations"}


def test_inventory_example_is_valid():
    p = Path(__file__).resolve().parents[1] / "faults" / "inventory.example.yaml"
    inv = Inventory.model_validate(yaml.safe_load(p.read_text()))
    assert "openstack-lab" in inv.environments


def test_bad_fault_file_is_reported(tmp_path):
    (tmp_path / "catalog" / "g").mkdir(parents=True)
    (tmp_path / "catalog" / "g" / "x.yaml").write_text("id: y\ntitle: t\n")
    with pytest.raises(FaultFileError):
        load_faults(tmp_path)


def test_per_device_nos(eng, faults):
    e, _ = eng
    with pytest.raises(FaultError, match="gpu1 runs linux"):
        e.plan(faults["network/interface-shutdown"], "fab", overrides={"device": "gpu1", "interface": "eth1"})
    e.plan(faults["network/packet-loss"], "fab", overrides={"device": "gpu1", "interface": "eth1"})


def test_drills_load_and_resolve(faults):
    from daalu_automation.faults.catalog import load_drills

    recipes = load_recipes(faults=faults)
    drills = load_drills()
    assert {"openstack/gameday", "fabric/gameday"} <= set(drills)
    for d in drills.values():
        for it in d.items:
            assert (it.recipe in recipes) or (it.fault in faults), (d.id, it)


def test_guide_lists_the_whole_catalog(faults):
    """docs/content/lab/fault-injection.md must name every fault, recipe and drill."""
    from daalu_automation.faults.catalog import load_drills

    root = Path(__file__).resolve().parents[1]
    guide_path = next(p for p in (root / "docs/content/lab/fault-injection.md",
                                  root / "docs/fault-injection.md") if p.exists())
    guide = guide_path.read_text()
    recipes = load_recipes(faults=faults)
    drills = load_drills()
    missing = [k for k in [*faults, *recipes, *drills] if f"`{k}`" not in guide]
    assert not missing, f"add to the guide's catalog tables: {missing}"
    assert f"{len(faults)} faults, {len(recipes)} recipes, {len(drills)} drills" in guide
