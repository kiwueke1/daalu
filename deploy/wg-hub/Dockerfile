FROM alpine:3.21

RUN apk add --no-cache \
      wireguard-tools \
      iptables \
      ip6tables \
      inotify-tools \
      bash \
      coreutils

# Ubuntu 26.04 hosts ship AppArmor profiles that attach by path to
# /usr/bin/wg-quick and /usr/bin/wg. Containers that run unconfined (kind
# nodes, some privileged runtimes) get those host profiles on exec, and they
# deny the Alpine coreutils wg-quick needs, so the hub crash-loops with
# "/usr/bin/readlink: Permission denied". Living in /usr/local/sbin (first
# on PATH) keeps the host profiles from attaching. Same fix as deploy/edge.
RUN mkdir -p /usr/local/sbin \
 && mv /usr/bin/wg-quick /usr/local/sbin/wg-quick \
 && mv /usr/bin/wg /usr/local/sbin/wg

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
