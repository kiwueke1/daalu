#!/bin/bash
# WireGuard hub for daalu-automation. Brings up wg0 from the secret-
# mounted config, then keeps the live interface reconciled with that
# config as the daalu-api coordinator patches the daalu-wireguard-peers
# Secret (one Peer block per onboarded cluster).
#
# Reload reliability — the important part:
#   kubelet updates a secret-mounted volume by writing a fresh
#   `..2026_..._<ts>` directory and atomically swapping the `..data`
#   symlink to point at it. It does NOT modify wg0.conf in place; the
#   file is a symlink (wg0.conf -> ..data/wg0.conf) whose *target* is
#   replaced. An `inotifywait` on the file path resolves that symlink to
#   a concrete inode at watch-setup time, so on the first `..data` swap
#   the watched inode is deleted, its watch is dropped, and every later
#   update is silently missed. That is exactly why onboarded peers used
#   to sit on "awaiting handshake" until the hub was restarted by hand.
#
# We fix it two ways, belt-and-suspenders:
#   (a) watch the mount DIRECTORY (a stable inode) for the `..data`
#       swap, for near-instant reaction to a new peer; and
#   (b) run an unconditional periodic `wg syncconf` as a
#       guaranteed-convergence backstop in case an event is ever missed.
# `wg syncconf` is idempotent and only diffs peers — it never tears down
# a live handshake — so re-running it on every tick is cheap and safe.
set -euo pipefail

CONFIG_DIR=/etc/wireguard
CONFIG="$CONFIG_DIR/wg0.conf"
IFACE=wg0
# How often the fallback reconcile runs. Fast enough that onboarding
# never waits long even if the inotify path misbehaves.
RECONCILE_INTERVAL="${WG_RECONCILE_INTERVAL:-15}"
# Docker Compose (file-backed hub): there is no pod for the API to exec
# `wg show` in, so the hub writes the dump to this path every tick and the
# health task reads it. Unset under Kubernetes (the mount is read-only).
DUMP_PATH="${WG_DUMP_PATH:-}"

write_dump() {
  [[ -n "$DUMP_PATH" ]] || return 0
  wg show "$IFACE" dump > "$DUMP_PATH.tmp" 2>/dev/null \
    && mv -f "$DUMP_PATH.tmp" "$DUMP_PATH" || true
}

if [[ ! -s "$CONFIG" ]]; then
  echo "[wg-hub] $CONFIG missing or empty — waiting for coordinator to seed it"
  while [[ ! -s "$CONFIG" ]]; do
    sleep 5
  done
fi

# `wg-quick up` reads PostUp/PostDown from the config and brings the
# interface up. If the coordinator rotates the hub key the file changes
# wholesale; a syncconf can't swap the interface's own private key, so
# that (rare) case still relies on a pod restart.
# A crash (no TERM, so no wg-quick down) leaves wg0 behind in a network
# namespace that outlives the container (the host under hostNetwork, the
# shared netns under Compose). Clear it so wg-quick up doesn't fail.
ip link del "$IFACE" 2>/dev/null || true
wg-quick up "$IFACE"
echo "[wg-hub] interface $IFACE up"
write_dump

cleanup() {
  echo "[wg-hub] shutdown signal — bringing $IFACE down"
  wg-quick down "$IFACE" || true
  exit 0
}
trap cleanup TERM INT

# Reconcile the live interface with the on-disk config. `strip` may fail
# during the brief window where kubelet is mid-swap on the volume; treat
# that as transient and let the next tick/event pick it up.
reconcile() {
  local stripped
  if ! stripped=$(wg-quick strip "$IFACE" 2>/dev/null); then
    echo "[wg-hub] reconcile: config unreadable (mid-swap?) — will retry"
    return 0
  fi
  if wg syncconf "$IFACE" <(printf '%s\n' "$stripped"); then
    echo "[wg-hub] reconciled live $IFACE with $CONFIG"
  else
    echo "[wg-hub] WARNING: syncconf failed — leaving running peers intact"
  fi
  write_dump
}


# (a) Event-driven: watch the directory so we catch the `..data` symlink
# swap (create/moved_to) that a file-path watch misses. The directory
# inode is stable, so -m survives every swap.
(
  inotifywait -m -q -e create -e moved_to -e move_self -e delete_self \
    --format '%e %f' "$CONFIG_DIR" |
  while read -r ev file; do
    case "$file" in
      ..data|..data_tmp|wg0.conf)
        echo "[wg-hub] config event: $ev $file"
        reconcile
        ;;
    esac
  done
) &
WATCH_PID=$!

# (b) Guaranteed-convergence fallback: periodic reconcile, idempotent.
(
  while sleep "$RECONCILE_INTERVAL"; do
    reconcile
  done
) &
POLL_PID=$!

# If either loop dies, exit so the pod restarts rather than silently
# ceasing to reconcile (which is the failure we're eliminating).
wait -n "$WATCH_PID" "$POLL_PID"
echo "[wg-hub] a reconcile loop exited unexpectedly — shutting down for restart"
cleanup
