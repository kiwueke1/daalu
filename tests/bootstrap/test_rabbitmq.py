# tests/bootstrap/test_rabbitmq.py
# Tests for daalu.bootstrap.openstack.rabbitmq.RabbitMQServiceManager

from __future__ import annotations

import base64
import time
import pytest

from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager


# ---------------------------------------------------------------------------
# Fake kubectl
# ---------------------------------------------------------------------------

def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


class FakeKubectl:
    """Minimal kubectl fake tracking applied objects and returning canned secrets."""

    def __init__(self, secret_data: dict | None = None, missing_until_call: int = 0):
        self.applied: list[list] = []
        self._secret_data = secret_data or {}
        self._call_count = 0
        self._missing_until_call = missing_until_call

    def apply_objects(self, objs: list) -> None:
        self.applied.append(objs)

    def get_object(self, *, api_version, kind, name, namespace):
        self._call_count += 1
        if self._call_count <= self._missing_until_call:
            return None
        return self._secret_data.get(name)

    def b64decode_str(self, val: str) -> str:
        return base64.b64decode(val.encode()).decode()


# ---------------------------------------------------------------------------
# ensure_cluster
# ---------------------------------------------------------------------------

def test_ensure_cluster_applies_rabbitmqcluster_object():
    kubectl = FakeKubectl()
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    mgr.ensure_cluster("keystone")

    assert len(kubectl.applied) == 1
    obj = kubectl.applied[0][0]
    assert obj["kind"] == "RabbitmqCluster"
    assert obj["metadata"]["name"] == "rabbitmq-keystone"
    assert obj["metadata"]["namespace"] == "openstack"


def test_ensure_cluster_sets_replicas():
    kubectl = FakeKubectl()
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack", replicas=3)
    mgr.ensure_cluster("nova")
    assert kubectl.applied[0][0]["spec"]["replicas"] == 3


def test_ensure_cluster_idempotent_two_calls():
    kubectl = FakeKubectl()
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    mgr.ensure_cluster("neutron")
    mgr.ensure_cluster("neutron")
    # apply_objects called both times — idempotency is kubectl's responsibility
    assert len(kubectl.applied) == 2


# ---------------------------------------------------------------------------
# wait_for_default_user_secret
# ---------------------------------------------------------------------------

def test_wait_for_secret_found_immediately():
    secret = {
        "data": {
            "username": _b64("neutron_user"),
            "password": _b64("neutron_pw"),
        }
    }
    kubectl = FakeKubectl(secret_data={"rabbitmq-neutron-default-user": secret})
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    result = mgr.wait_for_default_user_secret("neutron", timeout=10, poll_interval=0)
    assert result == secret


def test_wait_for_secret_found_after_two_misses(monkeypatch):
    """Secret is absent for 2 polls then appears."""
    secret = {"data": {"username": _b64("u"), "password": _b64("p")}}
    kubectl = FakeKubectl(
        secret_data={"rabbitmq-nova-default-user": secret},
        missing_until_call=2,
    )
    monkeypatch.setattr(time, "sleep", lambda _: None)
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    result = mgr.wait_for_default_user_secret("nova", timeout=300, poll_interval=0)
    assert result["data"]["username"] == _b64("u")


def test_wait_for_secret_times_out(monkeypatch):
    """Secret never appears — should raise TimeoutError."""
    kubectl = FakeKubectl(secret_data={})

    # Make time.time() advance fast so the deadline is immediately exceeded
    call_counter = {"n": 0}
    original_time = time.time

    def fast_time():
        call_counter["n"] += 1
        return original_time() + call_counter["n"] * 1000

    monkeypatch.setattr(time, "time", fast_time)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    with pytest.raises(TimeoutError):
        mgr.wait_for_default_user_secret("glance", timeout=1, poll_interval=0)


# ---------------------------------------------------------------------------
# get_default_user_credentials
# ---------------------------------------------------------------------------

def test_get_credentials_decodes_base64():
    secret = {
        "data": {
            "username": _b64("glance_user"),
            "password": _b64("s3cr3t!"),
        }
    }
    kubectl = FakeKubectl(secret_data={"rabbitmq-glance-default-user": secret})
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    user, pw = mgr.get_default_user_credentials("glance")
    assert user == "glance_user"
    assert pw == "s3cr3t!"


def test_get_credentials_raises_when_username_missing():
    secret = {"data": {"password": _b64("p")}}
    kubectl = FakeKubectl(secret_data={"rabbitmq-heat-default-user": secret})
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    with pytest.raises(RuntimeError, match="missing username/password"):
        mgr.get_default_user_credentials("heat")


def test_get_credentials_raises_when_password_missing():
    secret = {"data": {"username": _b64("u")}}
    kubectl = FakeKubectl(secret_data={"rabbitmq-cinder-default-user": secret})
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    with pytest.raises(RuntimeError, match="missing username/password"):
        mgr.get_default_user_credentials("cinder")


# ---------------------------------------------------------------------------
# ensure_and_get_credentials
# ---------------------------------------------------------------------------

def test_ensure_and_get_credentials_calls_both():
    secret = {
        "data": {
            "username": _b64("user1"),
            "password": _b64("pass1"),
        }
    }
    kubectl = FakeKubectl(secret_data={"rabbitmq-barbican-default-user": secret})
    mgr = RabbitMQServiceManager(kubectl=kubectl, namespace="openstack")
    user, pw = mgr.ensure_and_get_credentials("barbican")
    # ensure_cluster was called (apply_objects was invoked)
    assert len(kubectl.applied) == 1
    assert user == "user1"
    assert pw == "pass1"
