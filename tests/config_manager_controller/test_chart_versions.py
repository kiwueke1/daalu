"""Chart-version enumeration + semver ordering.

The ordering cases here are the ones naive string comparison gets wrong, which
is the whole reason this module exists: the live pins span a prerelease
(``1.2.2-rc.23``) and a release (``1.3.0``), and an "upgrade available" badge
driven by ``str.__lt__`` would eventually offer a downgrade.
"""

from __future__ import annotations

import os

import pytest

from daalu_automation.config_manager_controller import chart_versions as cv


def _make_charts(tmp_path, *names: str) -> str:
    for name in names:
        os.makedirs(tmp_path / name, exist_ok=True)
    return str(tmp_path)


# ── Enumeration ────────────────────────────────────────────────────────


def test_lists_only_chart_directories(tmp_path):
    chart_dir = _make_charts(
        tmp_path,
        "nv-config-manager-1.2.2-rc.23",
        "nv-config-manager-1.3.0",
        # Siblings that share the prefix but are not installable charts.
        "nv-config-manager-1.3.0-api-specs",
        "nv-config-manager-1.2.2-rc.23-api-specs",
        "some-other-chart-9.9.9",
    )
    # A manifest file, not a directory — must not be mistaken for a version.
    (tmp_path / "nv-config-manager-1.3.0.images.txt").write_text("x")

    assert cv.available_versions(chart_dir) == ["1.2.2-rc.23", "1.3.0"]


def test_missing_chart_dir_is_empty_not_an_error(tmp_path):
    assert cv.available_versions(str(tmp_path / "nope")) == []
    assert cv.latest_version(str(tmp_path / "nope")) is None


def test_unparseable_directory_is_ignored(tmp_path):
    chart_dir = _make_charts(
        tmp_path, "nv-config-manager-1.3.0", "nv-config-manager-not-a-version!"
    )
    assert cv.available_versions(chart_dir) == ["1.3.0"]


def test_latest_is_the_newest_not_the_last_alphabetically(tmp_path):
    chart_dir = _make_charts(
        tmp_path, "nv-config-manager-1.9.0", "nv-config-manager-1.10.0"
    )
    # Lexicographically "1.10.0" < "1.9.0"; semver says otherwise.
    assert cv.latest_version(chart_dir) == "1.10.0"


# ── Ordering ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        # The live case this feature ships for.
        ("1.2.2-rc.23", "1.3.0", True),
        ("1.3.0", "1.2.2-rc.23", False),
        # Same version is never an upgrade — guards a no-op button.
        ("1.3.0", "1.3.0", False),
        # Numeric segments compare as numbers, not text.
        ("1.9.0", "1.10.0", True),
        ("1.10.0", "1.9.0", False),
        ("1.2.9", "1.2.10", True),
        # A release outranks any prerelease of the same numbers.
        ("1.3.0-rc.1", "1.3.0", True),
        ("1.3.0", "1.3.0-rc.1", False),
        # Prerelease identifiers compare numerically where numeric.
        ("1.3.0-rc.2", "1.3.0-rc.10", True),
        ("1.3.0-rc.10", "1.3.0-rc.2", False),
        # Differing depth.
        ("1.3", "1.3.1", True),
        ("1.3.1", "1.3", False),
    ],
)
def test_is_upgrade(current, candidate, expected):
    assert cv.is_upgrade(current, candidate) is expected


@pytest.mark.parametrize("bad", ["", "latest", "v1.3.0", "not-a-version"])
def test_unparseable_versions_never_claim_an_upgrade(bad):
    """A garbage pin must not produce an upgrade prompt in either direction."""
    assert cv.is_upgrade(bad, "1.3.0") is False
    assert cv.is_upgrade("1.3.0", bad) is False


def test_empty_current_version_is_not_an_upgrade_source():
    # Rows default chart_version to "" before first provision; that must not
    # render as "upgrade available" on a stack that doesn't exist yet.
    assert cv.is_upgrade("", "1.3.0") is False
