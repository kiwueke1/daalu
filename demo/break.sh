#!/usr/bin/env bash
# =============================================================================
#  Break the dummy app on purpose, so Daalu has something to discover and fix.
# -----------------------------------------------------------------------------
#  Each scenario pushes a bad rollout (a new ReplicaSet revision) that takes the
#  app DOWN. Because the Deployment uses strategy: Recreate, available replicas
#  drop to 0, the DummyAppDown alert fires (~1 min), and Daalu picks it up.
#
#  The fix in every case is to roll back the last change:
#     kubectl -n daalu-demo rollout undo deploy/dummy-app
#  …which is exactly what you'll approve inside Daalu.
#
#  Usage:
#     ./demo/break.sh                # default scenario: bad-image
#     ./demo/break.sh bad-image      # dummy-app: image tag that doesn't exist
#     ./demo/break.sh crashloop      # dummy-app: container crashes on startup
#     ./demo/break.sh errors         # metrics-app: error RATE climbs (metrics alert)
# =============================================================================
set -euo pipefail
# Find kubectl whether system-installed or fetched by up.sh into BINDIR.
export PATH="$PATH:${DAALU_DEMO_BINDIR:-$HOME/.daalu/bin}"
NS="daalu-demo"
GRN=$'\033[32m'; BLU=$'\033[36m'; YLW=$'\033[33m'; RST=$'\033[0m'
say(){ printf "%s\n" "${BLU}▶${RST} $*"; }
ok(){ printf "%s\n" "${GRN}✔${RST} $*"; }

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found"; exit 1; }
kubectl config use-context kind-daalu-demo >/dev/null 2>&1 || true

scenario="${1:-bad-image}"
case "$scenario" in
  bad-image)
    DEP="dummy-app"; ALERT="DummyAppDown (the app goes down)"
    say "Scenario: bad-image — pointing dummy-app at an image tag that doesn't exist"
    kubectl -n "$NS" set image "deploy/$DEP" dummy=nginx:this-tag-does-not-exist-9.9.9
    ;;
  crashloop)
    DEP="dummy-app"; ALERT="DummyAppDown (the app goes down)"
    say "Scenario: crashloop — overriding the container command so it exits immediately"
    kubectl -n "$NS" patch "deploy/$DEP" --type=json \
      -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo starting; sleep 2; echo crashing; exit 1"]}]'
    ;;
  errors)
    DEP="metrics-app"; ALERT="HighErrorRate (a metrics-based alert)"
    say "Scenario: errors — flipping metrics-app into error mode (its error rate climbs)"
    kubectl -n "$NS" set env "deploy/$DEP" ERROR_MODE=true
    ;;
  *)
    echo "unknown scenario: $scenario (use 'bad-image', 'crashloop', or 'errors')" >&2; exit 1 ;;
esac

ok "broken: $DEP. Alert to expect: $ALERT"
cat <<EOF

  What to expect:
    • ~1–2 min: the alert fires in Alertmanager.
    • ~2–3 min: it shows up in Daalu → Alerts.
    • Open the alert in Daalu and let the agent investigate — it will inspect the
      workload, find the bad rollout, and propose a rollback to approve.

  Watch it happen:
    ./demo/status.sh
    kubectl -n $NS get pods -w

  Fix it yourself (instead of via Daalu):
    kubectl -n $NS rollout undo deploy/$DEP
EOF
