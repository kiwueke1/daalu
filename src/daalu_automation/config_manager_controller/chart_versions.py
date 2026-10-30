"""Enumerate + order the vendored NV-CM chart versions.

The controller ships every supported NV-CM release as a vendored directory
under ``config_manager_chart_dir`` (``deploy/charts/nv-config-manager-<v>``),
alongside ``-api-specs`` siblings and ``.images.txt`` manifests which are *not*
charts. This module is the single place that decides which of those directories
count as installable versions and how they order, so ``_chart_path`` can refuse
a version that isn't on disk and the API can answer "is an upgrade available?".

Ordering is semver-aware rather than lexicographic: the live pins include a
prerelease (``1.2.2-rc.23``) and naive string compare gets pairs like
``1.9.0`` vs ``1.10.0`` backwards.
"""

from __future__ import annotations

import os
import re

_PREFIX = "nv-config-manager-"
# Sibling artefacts that share the chart prefix but aren't charts themselves.
_NON_CHART_SUFFIXES = ("-api-specs",)

_VERSION_RE = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"  # 1 / 1.3 / 1.2.2
    r"(?:[-+.](?P<pre>[0-9A-Za-z.\-]+))?$"  # -rc.23, -alpha1, …
)


def parse_version(version: str) -> tuple[tuple[int, ...], int, tuple[object, ...]] | None:
    """Parse ``version`` into a sort key, or ``None`` if unparseable.

    The key is ``(release_numbers, is_final, prerelease_identifiers)``. Per
    semver, a release outranks any prerelease of the same numbers, so
    ``is_final`` is 1 for ``1.3.0`` and 0 for ``1.3.0-rc.1``.
    """
    m = _VERSION_RE.match(version.strip())
    if m is None:
        return None
    release = tuple(int(p) for p in m.group("release").split("."))
    pre = m.group("pre")
    if not pre:
        return (release, 1, ())
    # Numeric identifiers compare numerically and rank below alphanumerics.
    ids: list[object] = []
    for part in re.split(r"[.\-]", pre):
        if not part:
            continue
        ids.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return (release, 0, tuple(ids))


def _sort_key(version: str) -> tuple[tuple[int, ...], int, tuple[object, ...]]:
    parsed = parse_version(version)
    # Unparseable versions sort below everything rather than raising — a stray
    # directory shouldn't take the controller down.
    return parsed if parsed is not None else ((), 0, ())


def available_versions(chart_dir: str) -> list[str]:
    """Installable chart versions found on disk, oldest first."""
    try:
        entries = os.listdir(chart_dir)
    except OSError:
        return []
    found: list[str] = []
    for name in entries:
        if not name.startswith(_PREFIX):
            continue
        if any(name.endswith(sfx) for sfx in _NON_CHART_SUFFIXES):
            continue
        if not os.path.isdir(os.path.join(chart_dir, name)):
            continue  # skips the .images.txt manifests
        version = name[len(_PREFIX) :]
        if version and parse_version(version) is not None:
            found.append(version)
    return sorted(set(found), key=_sort_key)


def latest_version(chart_dir: str) -> str | None:
    versions = available_versions(chart_dir)
    return versions[-1] if versions else None


def is_upgrade(current: str, candidate: str) -> bool:
    """True when ``candidate`` is strictly newer than ``current``."""
    cur, cand = parse_version(current or ""), parse_version(candidate or "")
    if cur is None or cand is None:
        return False
    return cand > cur
