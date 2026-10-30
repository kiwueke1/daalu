#!/bin/bash
# daalu-logship — ship journald lines off a lab device as RFC5424 syslog/TCP.
#
# Why not rsyslogd? The CVX 5.3 container image's rsyslogd EPERMs opening ANY
# config file (even a fresh /tmp one, as root, in a privileged container) —
# see onboard-to-daalu.md. journald *does* run, so we tail it and frame each
# line as RFC5424 ourselves ("-" timestamp = NILVALUE; Loki stamps arrival
# time). Alloy's syslog listener on the clab bridge gateway ingests it with
# non-transparent (newline) framing.
#
# On TCP drop the write fails -> SIGPIPE kills the pipeline -> retry loop.
TARGET_HOST="${1:-172.20.20.1}"
TARGET_PORT="${2:-5514}"
H="$(hostname)"

# Cumulus keeps eth0 in the mgmt VRF: outbound from the default VRF is
# "Network is unreachable" even for the on-link mgmt subnet. Re-exec inside
# the VRF when one exists (inbound is unaffected — tcp_l3mdev_accept=1).
if [ -z "$DAALU_VRF_WRAPPED" ] && ip vrf show 2>/dev/null | grep -q "^mgmt"; then
  export DAALU_VRF_WRAPPED=1
  exec ip vrf exec mgmt "$0" "$@"
fi

while :; do
  journalctl -n0 -f -q -o short-iso --no-hostname 2>/dev/null |
    awk -v h="$H" '{ printf "<134>1 - %s journal - - - %s\n", h, $0; fflush() }' \
      > "/dev/tcp/${TARGET_HOST}/${TARGET_PORT}"
  sleep 5
done
