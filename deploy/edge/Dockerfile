FROM alpine:3.21

RUN apk add --no-cache \
      wireguard-tools \
      iptables \
      ip6tables \
      curl \
      jq \
      bash \
      coreutils \
      python3 \
      py3-aiohttp

# Ubuntu 26.04 hosts ship AppArmor profiles that attach by path to
# /usr/bin/wg-quick and /usr/bin/wg. Containers that run unconfined (kind
# nodes, some privileged runtimes) get those host profiles on exec, and they
# deny the Alpine coreutils wg-quick needs, so the edge exits 126. Living in
# /usr/local/sbin (first on PATH) keeps the host profiles from attaching.
RUN mkdir -p /usr/local/sbin \
 && mv /usr/bin/wg-quick /usr/local/sbin/wg-quick \
 && mv /usr/bin/wg /usr/local/sbin/wg

COPY entrypoint.sh /entrypoint.sh
COPY proxy.py /proxy.py
RUN chmod +x /entrypoint.sh /proxy.py

# Default entrypoint is the WireGuard tunnel manager. The proxy sidecar
# in the Helm chart overrides this with `python3 /proxy.py`.
ENTRYPOINT ["/entrypoint.sh"]
