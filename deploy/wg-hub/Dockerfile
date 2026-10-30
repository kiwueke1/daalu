FROM alpine:3.21

RUN apk add --no-cache \
      wireguard-tools \
      iptables \
      ip6tables \
      inotify-tools \
      bash \
      coreutils

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
