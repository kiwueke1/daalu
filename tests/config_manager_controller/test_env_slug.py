"""Environment naming rules.

The slug becomes a namespace *and* a public DNS label, and it is immutable
after create — so every rule here can only be enforced once, at create time.
"""

from __future__ import annotations

import pytest

from daalu_automation.config_manager_controller import env_slug as es


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Production", "production"),
        ("  Staging  ", "staging"),
        ("Lab 2", "lab-2"),
        ("east/west", "east-west"),
        ("A--B", "a-b"),
        ("-edge-", "edge"),
    ],
)
def test_slugify(name, expected):
    assert es.slugify(name) == expected


@pytest.mark.parametrize("slug", ["prod", "staging", "lab-2", "a", "east-west-1"])
def test_valid_slugs_round_trip(slug):
    assert es.validate_slug(slug) == slug


@pytest.mark.parametrize(
    "bad",
    ["", "-prod", "prod-", "Prod", "pro_d", "pro d", "prod.env", "a" * 25],
)
def test_rejected_slugs(bad):
    with pytest.raises(es.InvalidEnvironmentName):
        es.validate_slug(bad)


@pytest.mark.parametrize(
    "bad", ["render", "render-prod", "nautobot-a", "config-store", "config-store-x",
            "workflow-1", "temporal-eu"],
)
def test_tool_prefixes_are_rejected(bad):
    """The collision that would silently route to the wrong service.

    The tool proxy strips a known tool prefix and treats the rest as the
    environment, so a `render-prod` environment's own browser host reads as
    "the render tool of environment prod".
    """
    with pytest.raises(es.InvalidEnvironmentName) as e:
        es.validate_slug(bad)
    assert "wrong service" in str(e.value)


def test_prefix_check_does_not_over_match():
    # `renderer` merely *starts with* the letters of `render`; it is not the
    # `render-` prefix, so it must remain usable.
    assert es.validate_slug("renderer") == "renderer"
    assert es.validate_slug("temporary") == "temporary"


def test_reserved_names_rejected():
    with pytest.raises(es.InvalidEnvironmentName):
        es.validate_slug("gateway")


def test_slug_from_name_reports_a_user_safe_reason():
    with pytest.raises(es.InvalidEnvironmentName) as e:
        es.slug_from_name("Render Prod")
    assert "render" in str(e.value)


def test_derived_identifiers_stay_single_label():
    ns = es.namespace_for("acme", "prod")
    host = es.base_hostname_for("acme", "prod", "cmtools.daalu.io")
    assert ns == "cm-acme-prod"
    assert host == "prod-acme.cmtools.daalu.io"
    # One label before the base domain — this is what keeps the existing
    # wildcard certificate, DNS record and ingress rule valid.
    assert host.split(".cmtools.daalu.io")[0].count(".") == 0


def test_max_length_leaves_room_for_tool_and_tenant_in_one_dns_label():
    # `<tool>-<env>-<tenant>` must stay under the 63-char DNS label limit.
    longest_tool = max(es.RESERVED_PREFIXES, key=len)
    label = f"{longest_tool}-{'e' * es.MAX_SLUG_LEN}-{'t' * 24}"
    assert len(label) <= 63
