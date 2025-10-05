from pathlib import Path
import textwrap
import tempfile

from daalu.config.loader import load_config

def test_load_config_minimal_ok(tmp_path: Path):
    cfg_text = textwrap.dedent("""
        environment: dev
        repos:
          - name: openstack
            url: https://tarballs.opendev.org/openstack/openstack-helm/
        releases:
          - name: keystone
            namespace: openstack
            chart: openstack/keystone
    """)
    f = tmp_path / "cluster.yaml"
    f.write_text(cfg_text)
    cfg = load_config(f)
    assert cfg.environment == "dev"
    assert cfg.releases[0].name == "keystone"

