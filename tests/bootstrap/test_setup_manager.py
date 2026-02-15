import json
from pathlib import Path

import builtins
import types
import subprocess
import os

from daalu.bootstrap.setup_manager import SetupManager, SetupOptions
from daalu.bootstrap import hosts_inventory


class SpyRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, capture_output=False, text=False, check=False):
        self.calls.append(argv)
        # Simulate key commands
        if argv[:3] == ["clusterctl", "get", "kubeconfig"]:
            return types.SimpleNamespace(returncode=0, stdout="apiVersion: v1\nclusters: []\n", stderr="")
        if argv[:5] == ["kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf", "get", "nodes"]:
            data = {"items":[{"status":{"addresses":[{"type":"InternalIP","address":"10.0.0.10"}]}}]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if argv[:5] == ["kubectl", "--kubeconfig", "/var/lib/tmp/kubeconfig", "get", "pods"]:
            # simulate 5 ready cilium pods
            pod = {"status":{"phase":"Running","containerStatuses":[{"ready":True}]}}
            data = {"items":[pod,pod,pod,pod,pod]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if argv[:4] == ["kubectl", "--kubeconfig", "/var/lib/tmp/kubeconfig", "get"] and argv[-2:] == ["-o", "json"]:
            # 'kubectl get nodes -o json'
            data = {"items":[{"metadata":{"name":"node-1"}},{"metadata":{"name":"node-2"}}]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if argv[:3] == ["kubectl", "get", "machines"]:
            # mgmt context machines/<name> -> InternalIP
            name = argv[3]
            data = {"status":{"addresses":[{"type":"InternalIP","address": f"10.10.0.{1 if name=='node-1' else 2}"}]}}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        # default
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

def test_setup_manager_happy_path(monkeypatch, tmp_path: Path):
    # Monkeypatch subprocess.run
    spy = SpyRun()
    monkeypatch.setattr(subprocess, "run", spy)

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
