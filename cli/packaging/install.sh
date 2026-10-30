#!/bin/sh
# Daalu CLI installer.
#
#   curl -fsSL https://get.daalu.io/install.sh | sh
#
# Detects your OS/arch, downloads the matching single-file `daalu` binary from
# the latest GitHub Release, and installs it to ~/.local/bin (or /usr/local/bin
# if writable). Re-run any time to upgrade.
set -eu

REPO="${DAALU_CLI_REPO:-daalu-io/cli}"
RELEASE_BASE="https://github.com/${REPO}/releases/latest/download"
BIN_NAME="daalu"

say()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ── detect platform ──────────────────────────────────────────────────────────
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$os" in
  linux)  os="linux" ;;
  darwin) os="macos" ;;
  *) err "unsupported OS '$os' — try: pipx install daalu-cli" ;;
esac
case "$arch" in
  x86_64|amd64) arch="x64" ;;
  arm64|aarch64) arch="arm64" ;;
  *) err "unsupported arch '$arch' — try: pipx install daalu-cli" ;;
esac

asset="${BIN_NAME}-${os}-${arch}.tar.gz"
url="${RELEASE_BASE}/${asset}"

# ── pick an install dir ──────────────────────────────────────────────────────
if [ -w /usr/local/bin ] 2>/dev/null; then
  dest="/usr/local/bin"
else
  dest="${HOME}/.local/bin"
fi
mkdir -p "$dest"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

say "Downloading ${asset}…"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$url" -o "${tmp}/${asset}" || err "download failed: $url"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "${tmp}/${asset}" "$url" || err "download failed: $url"
else
  err "need curl or wget"
fi

tar -xzf "${tmp}/${asset}" -C "$tmp"
chmod +x "${tmp}/${BIN_NAME}"
mv "${tmp}/${BIN_NAME}" "${dest}/${BIN_NAME}"

say ""
say "✓ Installed ${BIN_NAME} to ${dest}/${BIN_NAME}"
case ":${PATH}:" in
  *":${dest}:"*) : ;;
  *) say "  Add ${dest} to your PATH:  export PATH=\"${dest}:\$PATH\"" ;;
esac
say ""
say "Next:  daalu login"
