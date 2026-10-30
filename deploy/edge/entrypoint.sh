#!/bin/bash
# daalu-edge: customer-side WireGuard endpoint for the operator's mesh.
#
# Required env (provided by the daalu-edge Helm chart):
#   OPERATOR_API_URL    e.g. https://api.daalu.io
#   CLUSTER_SLUG        DNS-style identifier the operator used at onboard time
#   TUNNEL_IP           the /32 the operator allocated on 10.200.0.0/16
#   HUB_ENDPOINT        host:port the hub listens on (UDP)
#   HUB_PUBLIC_KEY      hub's WG pubkey
#   BOOTSTRAP_TOKEN     one-shot invite token; presented to /callback once
#
# Optional:
#   OUR_PUBLIC_ENDPOINT host:port the hub can reach us on, if we have one
#   STATE_DIR           defaults to /var/lib/daalu-edge
#   KUBE_TOKEN_FILE     path to a long-lived ServiceAccount token. When set,
#                       it is reported in the bootstrap callback and the hub
#                       assembles this cluster's kubeconfig from it — so no
#                       one has to paste one, and a rebuilt cluster replaces
#                       its own stale credential. Unset = skip (the operator
#                       registers a kubeconfig manually).
#
# State is persisted under STATE_DIR so a pod restart keeps the same
# keypair (otherwise the hub would see a brand-new pubkey and the
# operator's row would go stuck in awaiting_handshake forever). When
# STATE_DIR is empty we generate a new keypair and run the one-shot
# bootstrap; on subsequent boots we just bring wg0 back up.
set -euo pipefail

: "${OPERATOR_API_URL:?OPERATOR_API_URL is required}"
: "${CLUSTER_SLUG:?CLUSTER_SLUG is required}"
: "${TUNNEL_IP:?TUNNEL_IP is required}"
: "${HUB_ENDPOINT:?HUB_ENDPOINT is required}"
: "${HUB_PUBLIC_KEY:?HUB_PUBLIC_KEY is required}"

STATE_DIR=${STATE_DIR:-/var/lib/daalu-edge}
PRIVKEY_FILE="$STATE_DIR/wg0.priv"
PUBKEY_FILE="$STATE_DIR/wg0.pub"
BOOTSTRAPPED_MARK="$STATE_DIR/.bootstrapped"
IFACE=wg0
CONFIG=/etc/wireguard/${IFACE}.conf

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# 1. Keypair. Generated once and pinned to STATE_DIR so it survives
#    pod restarts. wg-quick will refuse to write into /etc/wireguard if
#    perms are wrong, hence the explicit chmod.
if [[ ! -s "$PRIVKEY_FILE" ]]; then
  echo "[edge] generating new WireGuard keypair"
  wg genkey | tee "$PRIVKEY_FILE" | wg pubkey > "$PUBKEY_FILE"
  chmod 600 "$PRIVKEY_FILE"
fi

OUR_PRIVKEY=$(cat "$PRIVKEY_FILE")
OUR_PUBKEY=$(cat "$PUBKEY_FILE")
echo "[edge] our pubkey: $OUR_PUBKEY"

# 2. Bootstrap callback. The operator's API row carries a sha256 of the
#    invite token; once we redeem it the hash is cleared, so further
#    bootstrap attempts will 409. We mark a local file so we don't even
#    try a second time on restart (which would just spam a warn log).
if [[ ! -f "$BOOTSTRAPPED_MARK" ]]; then
  if [[ -z "${BOOTSTRAP_TOKEN:-}" ]]; then
    echo "[edge] FATAL: first boot but BOOTSTRAP_TOKEN is unset; the operator must re-issue this cluster row"
    exit 1
  fi
  # NB: do NOT write this as `($e | select(length>0) | {customer_endpoint:$e} // {})`.
  # When $e is empty the `select` produces zero values, the rest of the
  # pipe also produces zero values, the `+` propagates the emptiness,
  # and the WHOLE expression evaluates to nothing — body becomes "" and
  # the API rejects the empty POST with 422 (missing required fields).
  # Use an `if/then/else` instead so the alternative branch always
  # yields exactly one value.
  # Self-register this cluster's API credential alongside the pubkey, so the
  # operator never hand-pastes a kubeconfig (and a rebuilt cluster replaces
  # its own stale one instead of 401ing at the next provision). The token
  # controller fills this Secret asynchronously, so it can legitimately be
  # missing for a beat on a cold install — wait briefly rather than racing.
  KUBE_TOKEN=""
  if [[ -n "${KUBE_TOKEN_FILE:-}" ]]; then
    for _ in $(seq 1 15); do
      [[ -s "$KUBE_TOKEN_FILE" ]] && { KUBE_TOKEN=$(cat "$KUBE_TOKEN_FILE"); break; }
      sleep 2
    done
    if [[ -n "$KUBE_TOKEN" ]]; then
      echo "[edge] reporting cluster API credential to the hub (self-registration)"
    else
      # Not fatal: the tunnel is the point of this callback. Without the token
      # the operator just falls back to pasting a kubeconfig by hand.
      echo "[edge] WARN: no SA token at $KUBE_TOKEN_FILE; skipping kubeconfig self-registration"
    fi
  fi
  body=$(jq -nc \
    --arg t "$BOOTSTRAP_TOKEN" \
    --arg p "$OUR_PUBKEY" \
    --arg e "${OUR_PUBLIC_ENDPOINT:-}" \
    --arg k "$KUBE_TOKEN" \
    '{invite_token:$t, customer_pubkey:$p}
     + (if ($e|length)>0 then {customer_endpoint:$e} else {} end)
     + (if ($k|length)>0 then {kube_token:$k} else {} end)')
  echo "[edge] POST ${OPERATOR_API_URL}/api/v1/clusters/${CLUSTER_SLUG}/callback"
  # Capture body + status separately so a non-2xx response prints the
  # API's JSON error detail instead of a bare `curl: (22)`. The api's
  # 401/409 reasons (invalid token / token already redeemed) are
  # actionable; previously they showed as opaque curl exit codes.
  http=$(curl -sS --retry 5 --retry-delay 3 --retry-connrefused \
    -o /tmp/edge-resp.json -w '%{http_code}' \
    -X POST \
    -H 'content-type: application/json' \
    -d "$body" \
    "${OPERATOR_API_URL}/api/v1/clusters/${CLUSTER_SLUG}/callback")
  if [[ "$http" == 2* ]]; then
    resp=$(cat /tmp/edge-resp.json)
    echo "[edge] bootstrap OK: $(echo "$resp" | jq -r '.status')"
    touch "$BOOTSTRAPPED_MARK"
  elif [[ "$http" == 409 ]]; then
    # 409 = the invite token was already redeemed by an earlier boot. This is
    # reached when STATE_DIR was wiped (a fresh PVC, or — historically — an
    # emptyDir pod restart) so the local .bootstrapped marker is gone but the
    # operator-side token is spent. Do NOT crashloop: the keypair printed above
    # is valid; bring wg0 up and let the operator reconcile the hub Peer to our
    # pubkey. With a persistent STATE_DIR this branch is taken at most once,
    # after which the marker persists and the callback is skipped entirely.
    echo "[edge] bootstrap callback 409 (invite token already redeemed); treating as already-bootstrapped and continuing"
    echo "[edge] ACTION REQUIRED: ensure the hub WireGuard Peer lists our pubkey: $OUR_PUBKEY"
    touch "$BOOTSTRAPPED_MARK"
  else
    echo "[edge] FATAL: bootstrap callback returned HTTP $http"
    echo "[edge] response body:"
    cat /tmp/edge-resp.json; echo
    echo "[edge] hint: re-onboard this cluster slug on the operator UI to mint a fresh invite token, then `helm uninstall daalu-edge` + delete the state PVC + reinstall with the new snippet."
    exit 1
  fi
else
  echo "[edge] already bootstrapped; skipping callback"
fi

# 3. Bring up wg0. PersistentKeepalive=25 keeps the NAT pinhole open
#    from our side so the hub can reach us even when we sit behind CGNAT.
cat > "$CONFIG" <<EOF
[Interface]
PrivateKey = ${OUR_PRIVKEY}
Address = ${TUNNEL_IP}/32

[Peer]
PublicKey = ${HUB_PUBLIC_KEY}
Endpoint = ${HUB_ENDPOINT}
AllowedIPs = 10.200.0.0/16
PersistentKeepalive = 25
EOF
chmod 600 "$CONFIG"

wg-quick up "$IFACE"
echo "[edge] interface $IFACE up; tunnel IP=${TUNNEL_IP}"

cleanup() {
  echo "[edge] shutdown signal — bringing $IFACE down"
  wg-quick down "$IFACE" || true
  exit 0
}
trap cleanup TERM INT

# Stay running so the pod doesn't crashloop. We don't need to actively
# do anything — WireGuard runs in-kernel; we just need to keep PID 1
# alive and forward signals.
while true; do
  sleep 3600 &
  wait $!
done
