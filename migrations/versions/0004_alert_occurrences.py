"""Dedupe alerts by fingerprint + record per-fire occurrence history.

Revision ID: 0004_alert_occurrences
Revises: 0003_cluster_tunnels
Create Date: 2026-05-20

Adds three columns to ``alerts`` (``fingerprint``, ``occurrence_count``,
``last_seen_at``) and a new ``alert_occurrences`` table that records
every time the underlying signal fired. Going forward,
``emit_alert`` upserts on ``(tenant_id, fingerprint)`` for open /
acknowledged alerts instead of inserting a duplicate row.

This migration also backfills existing data:

1. Computes a fingerprint for every existing alert using the same
   logic as ``core.alert_fingerprint.compute_fingerprint``.
2. Inserts one occurrence per existing alert at its ``created_at`` (so
   no fire-history is lost when we collapse).
3. For each ``(tenant_id, fingerprint)`` group whose status is
   open/acknowledged, keeps the oldest alert as canonical, re-points
   the other alerts' occurrences + chat / actions to the canonical
   row, then deletes the redundant alert rows. Resolved alerts are
   left untouched — once closed, they stay closed.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_alert_occurrences"
down_revision = "0003_cluster_tunnels"
branch_labels = None
depends_on = None


# Mirror of ``core.alert_fingerprint`` — inlined so the migration is
# self-contained and survives if that module is ever moved or renamed.
_POD_HASH_RE = re.compile(r"^[a-z0-9]{4,10}$", re.IGNORECASE)


def _pod_base_name(pod: str) -> str:
    parts = pod.split("-")
    while len(parts) > 1 and _POD_HASH_RE.fullmatch(parts[-1]):
        parts.pop()
    return "-".join(parts)


def _coerce_labels(metadata: dict[str, Any]) -> dict[str, str]:
    nested = metadata.get("labels") if isinstance(metadata, dict) else None
    flat: dict[str, str] = {}
    if isinstance(nested, dict):
        for k, v in nested.items():
            if isinstance(v, str):
                flat[k] = v
    for key in ("alert_name", "namespace", "deployment", "service", "pod"):
        v = metadata.get(key) if isinstance(metadata, dict) else None
        if isinstance(v, str) and v:
            flat[key] = v
    return flat


def _compute_fingerprint(module: str, title: str, metadata: dict[str, Any]) -> str:
    metadata = metadata or {}
    am_fp = metadata.get("fingerprint")
    if isinstance(am_fp, str) and am_fp.strip():
        return am_fp.strip()[:64]
    labels = _coerce_labels(metadata)
    alert_name = labels.get("alert_name") or title
    pod = labels.get("pod", "")
    parts = [
        module,
        (alert_name or "").strip().lower(),
        labels.get("namespace", ""),
        labels.get("deployment") or labels.get("service") or "",
        _pod_base_name(pod) if pod else "",
    ]
    return hashlib.sha1(
        "|".join(parts).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]


def upgrade() -> None:
    # ── Schema: new columns on alerts ──────────────────────────────────
    op.add_column(
        "alerts",
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_alerts_fingerprint", "alerts", ["fingerprint"])
    op.add_column(
        "alerts",
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "alerts",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_alerts_last_seen_at", "alerts", ["last_seen_at"])

    # ── Schema: alert_occurrences table ────────────────────────────────
    op.create_table(
        "alert_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_alert_occurrences_alert_id",
        "alert_occurrences",
        ["alert_id"],
    )
    op.create_index(
        "ix_alert_occurrences_tenant_id",
        "alert_occurrences",
        ["tenant_id"],
    )
    op.create_index(
        "ix_alert_occurrences_occurred_at",
        "alert_occurrences",
        ["occurred_at"],
    )

    # ── Backfill ───────────────────────────────────────────────────────
    import json as _json

    bind = op.get_bind()

    rows = bind.execute(
        sa.text(
            """
            SELECT id, tenant_id, module, title, status, metadata_json,
                   created_at
              FROM alerts
            """
        )
    ).mappings().all()

    # Step 1: compute fingerprints + seed one occurrence per existing row.
    fingerprints: dict[str, str] = {}
    for r in rows:
        fp = _compute_fingerprint(
            r["module"] or "", r["title"] or "", r["metadata_json"] or {}
        )
        fingerprints[str(r["id"])] = fp
        bind.execute(
            sa.text(
                """
                UPDATE alerts
                   SET fingerprint = :fp,
                       occurrence_count = 1,
                       last_seen_at = COALESCE(last_seen_at, created_at)
                 WHERE id = :id
                """
            ),
            {"fp": fp, "id": r["id"]},
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO alert_occurrences
                    (id, tenant_id, alert_id, occurred_at,
                     metadata_json, created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :tenant_id, :alert_id, :occurred_at,
                     CAST(:metadata AS JSON), :occurred_at, :occurred_at)
                """
            ),
            {
                "tenant_id": r["tenant_id"],
                "alert_id": r["id"],
                "occurred_at": r["created_at"],
                "metadata": _json.dumps(r["metadata_json"] or {}),
            },
        )

    # Step 2: collapse open / acknowledged duplicates.
    #
    # Group alerts by (tenant_id, fingerprint, status-bucket) where the
    # status-bucket lumps open + acknowledged together. Keep the oldest
    # row as canonical; for each other row in the group, re-point its
    # alert_occurrences (already inserted above), alert_chat_messages,
    # and alert_actions to the canonical id, then delete it.
    open_rows = [
        r for r in rows if r["status"] in ("open", "acknowledged")
    ]
    groups: dict[tuple[Any, str], list[Any]] = {}
    for r in open_rows:
        fp = fingerprints[str(r["id"])]
        groups.setdefault((r["tenant_id"], fp), []).append(r)

    for (_tenant, _fp), members in groups.items():
        if len(members) <= 1:
            continue
        members.sort(key=lambda r: r["created_at"])
        canonical = members[0]
        canonical_id = canonical["id"]
        for redundant in members[1:]:
            rid = redundant["id"]
            bind.execute(
                sa.text(
                    "UPDATE alert_occurrences SET alert_id = :cid WHERE alert_id = :rid"
                ),
                {"cid": canonical_id, "rid": rid},
            )
            bind.execute(
                sa.text(
                    "UPDATE alert_chat_messages SET alert_id = :cid WHERE alert_id = :rid"
                ),
                {"cid": canonical_id, "rid": rid},
            )
            bind.execute(
                sa.text(
                    "UPDATE alert_actions SET alert_id = :cid WHERE alert_id = :rid"
                ),
                {"cid": canonical_id, "rid": rid},
            )
            bind.execute(
                sa.text("DELETE FROM alerts WHERE id = :rid"),
                {"rid": rid},
            )

        # Recompute the canonical alert's counters from the (now
        # consolidated) occurrence rows.
        bind.execute(
            sa.text(
                """
                UPDATE alerts
                   SET occurrence_count = sub.cnt,
                       last_seen_at = sub.last_seen
                  FROM (
                    SELECT COUNT(*) AS cnt, MAX(occurred_at) AS last_seen
                      FROM alert_occurrences
                     WHERE alert_id = :cid
                  ) sub
                 WHERE alerts.id = :cid
                """
            ),
            {"cid": canonical_id},
        )


def downgrade() -> None:
    op.drop_table("alert_occurrences")
    op.drop_index("ix_alerts_last_seen_at", table_name="alerts")
    op.drop_column("alerts", "last_seen_at")
    op.drop_column("alerts", "occurrence_count")
    op.drop_index("ix_alerts_fingerprint", table_name="alerts")
    op.drop_column("alerts", "fingerprint")
