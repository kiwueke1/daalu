# tests/bootstrap/test_secrets_manager.py
# Tests for daalu.bootstrap.openstack.secrets_manager.SecretsManager

from __future__ import annotations

import base64
import pytest
from pathlib import Path

from daalu.bootstrap.openstack.secrets_manager import SecretsManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "secrets.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Load / parse
# ---------------------------------------------------------------------------

def test_load_flat_yaml(tmp_path):
    p = _write_yaml(tmp_path, "keystone_admin_password: hunter2\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("keystone_admin_password") == "hunter2"


def test_load_nested_yaml_flattens_one_level(tmp_path):
    p = _write_yaml(tmp_path, """
openstack_secrets:
  keystone_database_password: dbpass1
  nova_database_password: dbpass2
""")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("keystone_database_password") == "dbpass1"
    assert sm.get("nova_database_password") == "dbpass2"


def test_load_non_dict_raises(tmp_path):
    p = _write_yaml(tmp_path, "- just_a_list\n")
    with pytest.raises(ValueError, match="Expected mapping"):
        SecretsManager(secrets_file=p).load()


def test_load_empty_yaml_returns_empty(tmp_path):
    p = _write_yaml(tmp_path, "")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.raw() == {}


# ---------------------------------------------------------------------------
# get() / require()
# ---------------------------------------------------------------------------

def test_get_returns_value_when_present(tmp_path):
    p = _write_yaml(tmp_path, "foo: bar\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("foo") == "bar"


def test_get_returns_default_when_missing(tmp_path):
    p = _write_yaml(tmp_path, "foo: bar\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("nonexistent", "default_val") == "default_val"


def test_get_returns_none_when_missing_no_default(tmp_path):
    p = _write_yaml(tmp_path, "foo: bar\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("nonexistent") is None


def test_get_strips_openstack_helm_endpoints_prefix(tmp_path):
    p = _write_yaml(tmp_path, "keystone_admin_password: secret123\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("openstack_helm_endpoints_keystone_admin_password") == "secret123"


def test_get_strips_openstack_secrets_prefix(tmp_path):
    p = _write_yaml(tmp_path, "nova_database_password: nova_pw\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.get("openstack_secrets_nova_database_password") == "nova_pw"


def test_require_returns_value_when_present(tmp_path):
    p = _write_yaml(tmp_path, "keystone_admin_password: secure!\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.require("keystone_admin_password") == "secure!"


def test_require_raises_when_missing(tmp_path):
    p = _write_yaml(tmp_path, "foo: bar\n")
    sm = SecretsManager(secrets_file=p).load()
    with pytest.raises(ValueError, match="Missing required secret key"):
        sm.require("nonexistent_key")


def test_require_raises_when_value_empty_string(tmp_path):
    p = _write_yaml(tmp_path, "empty_key: ''\n")
    sm = SecretsManager(secrets_file=p).load()
    with pytest.raises(ValueError, match="Missing required secret key"):
        sm.require("empty_key")


# ---------------------------------------------------------------------------
# Password discovery
# ---------------------------------------------------------------------------

def test_discover_db_passwords_from_flat_keys(tmp_path):
    p = _write_yaml(tmp_path, """
keystone_database_password: ks_db_pw
nova_db_password: nova_db_pw
glance_mariadb_password: glance_db_pw
""")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.service_db_passwords["keystone"] == "ks_db_pw"
    assert sm.service_db_passwords["nova"] == "nova_db_pw"
    assert sm.service_db_passwords["glance"] == "glance_db_pw"


def test_discover_db_passwords_with_openstack_helm_prefix(tmp_path):
    p = _write_yaml(tmp_path, "openstack_helm_endpoints_neutron_database_password: ntron_pw\n")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.service_db_passwords.get("neutron") == "ntron_pw"


def test_discover_rabbit_passwords(tmp_path):
    p = _write_yaml(tmp_path, """
keystone_rabbitmq_password: ks_rmq_pw
nova_rabbit_password: nova_rmq_pw
""")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.service_rabbit_passwords["keystone"] == "ks_rmq_pw"
    assert sm.service_rabbit_passwords["nova"] == "nova_rmq_pw"


def test_operator_rabbit_secret_discovered(tmp_path):
    # Must be nested under a top-level section so SecretsManager's one-level
    # flattening promotes the rabbitmq key (as a dict value) into raw[].
    p = _write_yaml(tmp_path, """
k8s_secrets:
  rabbitmq-barbican-default-user:
    username: barbican_user
    password: barbican_rmq_pw
""")
    sm = SecretsManager(secrets_file=p).load()
    assert sm.service_rabbit_passwords.get("barbican") == "barbican_rmq_pw"


def test_operator_rabbit_secret_skipped_when_missing_fields(tmp_path):
    p = _write_yaml(tmp_path, """
k8s_secrets:
  rabbitmq-octavia-default-user:
    username: octavia_user
""")
    sm = SecretsManager(secrets_file=p).load()
    assert "octavia" not in sm.service_rabbit_passwords


# ---------------------------------------------------------------------------
# Kubernetes Secret object builders
# ---------------------------------------------------------------------------

def test_build_bundle_secret_includes_all_keys(tmp_path):
    p = _write_yaml(tmp_path, "key1: val1\nkey2: val2\n")
    sm = SecretsManager(secrets_file=p, default_namespace="openstack").load()
    obj = sm.build_bundle_secret_object()
    assert obj["kind"] == "Secret"
    assert obj["metadata"]["namespace"] == "openstack"
    assert obj["stringData"]["key1"] == "val1"
    assert obj["stringData"]["key2"] == "val2"


def test_build_bundle_secret_uses_custom_namespace(tmp_path):
    p = _write_yaml(tmp_path, "k: v\n")
    sm = SecretsManager(secrets_file=p, default_namespace="openstack").load()
    obj = sm.build_bundle_secret_object(namespace="custom-ns")
    assert obj["metadata"]["namespace"] == "custom-ns"


def test_build_specific_secret_base64_encodes_values(tmp_path):
    p = _write_yaml(tmp_path, "k: v\n")
    sm = SecretsManager(secrets_file=p).load()
    obj = sm.build_specific_secret_object(
        name="my-secret",
        key_to_value={"password": "hunter2"},
        source_keys={"password": "some_key"},
    )
    assert obj["kind"] == "Secret"
    assert obj["metadata"]["name"] == "my-secret"
    # data values must be base64
    decoded = base64.b64decode(obj["data"]["password"]).decode()
    assert decoded == "hunter2"


def test_build_specific_secret_records_traceability(tmp_path):
    p = _write_yaml(tmp_path, "k: v\n")
    sm = SecretsManager(secrets_file=p).load()
    sm.build_specific_secret_object(
        name="trace-secret",
        key_to_value={"pw": "abc"},
        source_keys={"pw": "original_source_key"},
    )
    refs = sm.traceability()
    assert len(refs) == 1
    assert refs[0].name == "trace-secret"
    assert refs[0].key == "pw"
    assert refs[0].source_key == "original_source_key"


# ---------------------------------------------------------------------------
# from_yaml classmethod
# ---------------------------------------------------------------------------

def test_from_yaml_classmethod(tmp_path):
    p = _write_yaml(tmp_path, "admin_password: pw123\n")
    sm = SecretsManager.from_yaml(path=p, namespace="openstack")
    assert sm.get("admin_password") == "pw123"
    assert sm.default_namespace == "openstack"
