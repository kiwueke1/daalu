"""Installable observability stores: manifest loading + provider gating."""

import pytest

from daalu_automation.core.observability_install import (
    INSTALLABLE,
    ObsInstallError,
    load_manifests,
)


def test_all_installable_manifests_parse():
    for provider in INSTALLABLE:
        docs = load_manifests(provider)
        assert docs, provider
        # Every doc is a complete object the apply loop can address.
        for d in docs:
            assert d["kind"] and d["metadata"]["name"], (provider, d.get("kind"))


def test_opensearch_bundle_includes_shipper():
    kinds = {(d["kind"], d["metadata"]["name"]) for d in load_manifests("opensearch")}
    assert ("StatefulSet", "opensearch") in kinds
    assert ("DaemonSet", "fluent-bit") in kinds


def test_unknown_provider_rejected():
    with pytest.raises(ObsInstallError):
        load_manifests("prometheus")  # connectable, but not installable
