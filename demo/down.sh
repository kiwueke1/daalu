#!/usr/bin/env bash
# =============================================================================
#  Tear down the hosted Daalu demo lab.
# -----------------------------------------------------------------------------
#  Deletes the kind cluster and detaches it from the hosted Daalu at
#  ${DAALU_API}: removes the prometheus/loki/kubernetes integration rows and the
#  cluster (tunnel) row. Detaching the cluster also frees its tunnel IP and lets
#  a fresh ./demo/up.sh mint a new one-shot invite token.
#
#  Required env (for the detach step): DAALU_TOKEN  (the same dpat_ PAT).
#  Optional env: DAALU_API (default https://daalu.example.com), CLUSTER_SLUG.
#
#  Usage:  DAALU_TOKEN=dpat_... ./demo/down.sh
# =============================================================================
set -euo pipefail
export PATH="$PATH:${DAALU_DEMO_BINDIR:-$HOME/.daalu/bin}"

DAALU_API="${DAALU_API:-https://daalu.example.com}"; DAALU_API="${DAALU_API%/}"
CLUSTER_SLUG="${CLUSTER_SLUG:-daalu-demo}"
CLUSTER="daalu-demo"

GRN=$'\033[32m'; BLU=$'\033[36m'; YLW=$'\033[33m'; RST=$'\033[0m'
say(){ printf "%s\n" "${BLU}▶${RST} $*"; }
ok(){ printf "%s\n" "${GRN}✔${RST} $*"; }
warn(){ printf "%s\n" "${YLW}!${RST} $*"; }

# ── Detach from the hub (best-effort; needs DAALU_TOKEN) ──────────────────────
if [ -n "${DAALU_TOKEN:-}" ]; then
  say "Detaching from ${DAALU_API} (integrations + cluster row)"
  for prov in prometheus loki kubernetes; do
    curl -fsS -X DELETE "${DAALU_API}/api/v1/integrations/config/${prov}" \
      -H "Authorization: Bearer ${DAALU_TOKEN}" >/dev/null 2>&1 \
      && ok "  removed '${prov}' integration" || warn "  '${prov}' integration not removed (already gone?)"
  done
  curl -fsS -X DELETE "${DAALU_API}/api/v1/clusters/${CLUSTER_SLUG}" \
    -H "Authorization: Bearer ${DAALU_TOKEN}" >/dev/null 2>&1 \
    && ok "  removed cluster '${CLUSTER_SLUG}'" || warn "  cluster '${CLUSTER_SLUG}' not removed (remove it in the UI if it lingers)"
else
  warn "DAALU_TOKEN not set — skipping hub detach. Remove the cluster + integrations in the UI, or re-run with DAALU_TOKEN set."
fi

# ── Delete the local kind cluster (takes the daalu-edge pod with it) ──────────
if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  say "Deleting kind cluster '${CLUSTER}'"
  kind delete cluster --name "$CLUSTER"
  ok "cluster deleted"
else
  warn "no kind cluster named '${CLUSTER}' found — nothing to delete"
fi

cat <<EOF

${GRN}✔ Demo lab torn down.${RST}
  The cluster + its integrations are detached from ${DAALU_API}.
  Re-run ./demo/up.sh to bring it all back (a fresh tunnel + token).
EOF
