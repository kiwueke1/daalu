import json
import shutil
from pathlib import Path

import builtins
import types
import subprocess
import os

from daalu.bootstrap.setup_manager import SetupManager, SetupOptions
from daalu.bootstrap import hosts_inventory


_FAKE_KUBECONFIG = (
    "apiVersion: v1\n"
    "clusters:\n"
    "- cluster:\n"
    "    server: https://10.0.0.100:6443\n"
    "  name: test-cluster\n"
    "contexts: []\n"
    "current-context: test-cluster\n"
    "kind: Config\n"
    "users: []\n"
)


class SpyRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, capture_output=False, text=False, check=False, cwd=None, **kwargs):
        if not isinstance(argv, list):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        self.calls.append(argv)

        # clusterctl get kubeconfig -> valid kubeconfig YAML with a server entry
        if argv[0] == "clusterctl" and "get" in argv and "kubeconfig" in argv:
            return types.SimpleNamespace(returncode=0, stdout=_FAKE_KUBECONFIG, stderr="")

        # kubectl get nodes -> list of two nodes
        if argv[0] == "kubectl" and "get" in argv and "nodes" in argv:
            data = {"items": [{"metadata": {"name": "node-1"}}, {"metadata": {"name": "node-2"}}]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        # kubectl get node <name> -> InternalIP
        if argv[0] == "kubectl" and "get" in argv and "node" in argv:
            data = {"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.11"}]}}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        # kubectl get pods -> 5 ready cilium pods (for wait_for_cilium)
        if argv[0] == "kubectl" and "get" in argv and "pods" in argv:
            pod = {"status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}
            data = {"items": [pod, pod, pod, pod, pod]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        # kubectl get machines -> InternalIP
        if argv[0] == "kubectl" and "get" in argv and "machines" in argv:
            data = {"status": {"addresses": [{"type": "InternalIP", "address": "10.10.0.1"}]}}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        # sudo cp <src> <dst> -> actually copy so file writes work in tests
        if argv[:2] == ["sudo", "cp"] and len(argv) == 4:
            shutil.copy(argv[2], argv[3])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        # sudo chmod -> no-op
        if argv[:2] == ["sudo", "chmod"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        # default (label, taint, helm, etc.)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

def test_setup_manager_happy_path(monkeypatch, tmp_path: Path):
    # Monkeypatch subprocess.run
    spy = SpyRun()
    monkeypatch.setattr(subprocess, "run", spy)

    # install_cilium is called by run() but the actual implementation
    # (install_cilium_test) invokes Helm over subprocess; patch it to a no-op
    # so this unit test focuses on orchestration, not Cilium deployment.
    from daalu.bootstrap.setup_manager import SetupManager as _SM
    monkeypatch.setattr(_SM, "install_cilium", lambda self, opts, host, port: None, raising=False)

    # Ensure paths exist
    etc = tmp_path / "etc"
    (etc / "kubernetes").mkdir(parents=True)
    os.environ.pop("KUBECONFIG", None)

    # Use temp hosts file and template dir
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "hosts.ini.j2").write_text("[k8s_cluster]\n{% for h in hosts_entries %}{{ h.hostname }} ansible_host={{ h.ip }}\n{% endfor %}")
    (templates / "openstack_hosts.ini.j2").write_text("[servers:vars]\nansible_user={{ ansible_user }}\n")

    opts = SetupOptions(
        workload_kubeconfig=tmp_path / "kubeconfig",
        admin_conf=etc / "kubernetes" / "admin.conf",
        hosts_file=tmp_path / "hosts",
        templates_dir=templates.relative_to(tmp_path)  # Simulate repo-root relative
    )

    sm = SetupManager(repo_root=tmp_path)  # no mgmt_context required for this stub
    sm.run(opts)

    # Kubeconfig got written
    assert opts.workload_kubeconfig.exists()
    # Hosts file updated with FQDN
    assert "node-1" in opts.hosts_file.read_text()
    # Inventory files rendered
    inv_dir = tmp_path / "cloud-config" / "inventory"
    assert (inv_dir / "hosts.ini").exists()
    assert (inv_dir / "openstack_hosts.ini").exists()

def test_hosts_inventory_helpers(monkeypatch, tmp_path: Path):
    # stub kubectl json helpers
    def fake_json_nodes(args, kube_context=None, kubeconfig=None):
        if "nodes" in args:
            return {"items":[{"metadata":{"name":"n1"}},{"metadata":{"name":"n2"}}]}
        raise RuntimeError("unexpected args")

    def fake_json_machine(args, kube_context=None, kubeconfig=None):
        return {"status":{"addresses":[{"type":"InternalIP","address":"10.20.0.9"}]}}

    monkeypatch.setattr(hosts_inventory, "_kubectl_json", lambda args, kube_context=None, kubeconfig=None:
                        fake_json_nodes(args, kube_context, kubeconfig) if "nodes" in args else fake_json_machine(args, kube_context, kubeconfig))

    entries = hosts_inventory.build_hosts_entries("mgmt-ctx", "/path/to/kubeconfig")
    assert entries == [("10.20.0.9","n1"), ("10.20.0.9","n2")]

    hosts = tmp_path / "hosts"
    hosts_inventory.update_hosts_file(entries, hosts, domain_suffix="example.test")
    text = hosts.read_text()
    assert "n1 example.test" not in text  # format is "ip host fqdn"
    assert "n1" in text and "n2" in text
