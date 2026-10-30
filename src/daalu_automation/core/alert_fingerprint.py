"""Stable identity for an alert — so re-fires collapse into one row.

Each :class:`Alert` carries a ``fingerprint`` string that identifies
"the same alert" across repeated fires. The infra agent's
``emit_alert`` looks up an existing open / acknowledged alert with the
same (tenant, fingerprint) and bumps its occurrence count instead of
inserting a duplicate row.

Two sources feed the fingerprint:

1. **Alertmanager-provided** — when the source event was a Prometheus
   alert, the upstream payload carries a stable ``fingerprint`` field
   (a 16-hex-char hash of the alert's full label set). We trust it
   verbatim — it's what Alertmanager itself groups by.

2. **Computed fallback** — for every other source (synthetic events,
   pagerduty, …) we derive a fingerprint from the module, the alert
   title (or ``alert_name`` label), and a small set
   of identifying labels: ``namespace``, ``deployment`` / ``service``,
   and the pod base-name (stripping the ReplicaSet hash suffix so
   churning pods of the same workload collapse together).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

# Pod names from a ReplicaSet end with `-<5-10 alphanum>-<5 alphanum>`.
# DaemonSets append `-<5 alphanum>`. Bare pods carry no suffix. Strip
# trailing tokens that look like the random suffix so e.g.
# "octavia-health-manager-default-787mv" → "octavia-health-manager".
_POD_HASH_RE = re.compile(r"^[a-z0-9]{4,10}$", re.IGNORECASE)


def _pod_base_name(pod: str) -> str:
    parts = pod.split("-")
    while len(parts) > 1 and _POD_HASH_RE.fullmatch(parts[-1]):
        parts.pop()
    return "-".join(parts)


def _coerce_labels(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Flatten label-like fields out of the metadata payload.

    Different sources stamp labels at different depths — Prometheus
    nests them under ``labels`` while ad-hoc emitters dump them at the
    top level. We accept both, with top-level values winning.
    """

    raw_labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
    nested = dict(raw_labels) if isinstance(raw_labels, Mapping) else {}

    flat: dict[str, str] = {}
    for k, v in nested.items():
        if isinstance(v, str):
            flat[k] = v
    for key in ("alert_name", "namespace", "deployment", "service", "pod"):
        v = metadata.get(key) if isinstance(metadata, Mapping) else None
        if isinstance(v, str) and v:
            flat[key] = v
    return flat


def compute_fingerprint(
    *,
    module: str,
    title: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Return a 16-hex-char fingerprint for this alert.

    Stable across re-fires of the same logical alert; differs across
    distinct targets (different namespace / deployment / pod-base /
    service / alertname). When the source payload carries an
    Alertmanager fingerprint we use it verbatim, otherwise we compute
    our own SHA1 from the identifying fields.
    """

    metadata = metadata or {}

    # 1. Trust Alertmanager's own fingerprint when present.
    am_fp = metadata.get("fingerprint")
    if isinstance(am_fp, str) and am_fp.strip():
        return am_fp.strip()[:64]

    labels = _coerce_labels(metadata)

    alert_name = labels.get("alert_name") or title
    namespace = labels.get("namespace", "")
    deployment = labels.get("deployment", "")
    service = labels.get("service", "")
    pod_base = _pod_base_name(labels.get("pod", "")) if labels.get("pod") else ""

    # The grouping key: order-stable so the hash is reproducible.
    parts = [
        module,
        alert_name.strip().lower(),
        namespace,
        deployment or service,
        pod_base,
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
