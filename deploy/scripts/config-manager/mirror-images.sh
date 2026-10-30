#!/usr/bin/env bash
#
# mirror-images.sh — mirror the NV-CM chart's images into daalu's Harbor.
#
# Engineer chapter 64 §64.2 step 2. The vendored NV-CM chart pulls every
# image as "<repository>:<tag>" with NO global.imageRegistry indirection,
# so to run it against Harbor we (a) push each image to
#   <HARBOR>/<HARBOR_PROJECT>/<basename>:<tag>
# and (b) the controller's render_values() repoints global.images.<key>.
# repository at that exact path. The basename + project here MUST match
# src/daalu_automation/config_manager_controller/values.py
# (_HARBOR_IMAGE_BASENAMES / HARBOR_PROJECT).
#
# Designed to download each image ONCE: the source blob lands in a local
# cache (skopeo: an OCI layout under CACHE_DIR; docker: the daemon's image
# store) and every later run skips images already cached AND already
# present in Harbor. Safe to re-run anytime — it converges, it doesn't
# re-pull.
#
#   Usage:
#     HARBOR=harbor.daalu.io NVIDIA_SRC=nvcr.io/nvidia ./mirror-images.sh
#
#   Required:
#     HARBOR         Harbor registry host (e.g. harbor.daalu.io). This is
#                    also what you set CONFIG_MANAGER_HARBOR_REGISTRY to.
#   Common:
#     NVIDIA_SRC     Real source for the NVIDIA service images, replacing
#                    the chart's "registry.example.com/nvidia" placeholder
#                    (e.g. nvcr.io/nvidia, or a registry you loaded the
#                    vendor tarballs into). Omit to SKIP the NVIDIA images
#                    and mirror only the public infra ones.
#     HARBOR_PROJECT Harbor project to push under (default: nv-config-manager).
#     CACHE_DIR      Local cache dir (default: ./.image-mirror-cache).
#     ENGINE         skopeo | docker (default: auto-detect, prefer skopeo).
#     DRY_RUN        1 = print actions, copy nothing.
#
#   Auth (do this once, outside the script):
#     skopeo login $HARBOR          (or: docker login $HARBOR)
#     skopeo login $NVIDIA_SRC_HOST (NGC: user '$oauthtoken', NGC API key)
#
set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────
HARBOR="${HARBOR:-}"
HARBOR_PROJECT="${HARBOR_PROJECT:-nv-config-manager}"
NVIDIA_SRC="${NVIDIA_SRC:-}"
CACHE_DIR="${CACHE_DIR:-./.image-mirror-cache}"
ENGINE="${ENGINE:-}"
DRY_RUN="${DRY_RUN:-0}"
# The placeholder the chart ships; rewritten to $NVIDIA_SRC when pulling.
NVIDIA_PLACEHOLDER="registry.example.com/nvidia"

if [[ -z "$HARBOR" ]]; then
  echo "ERROR: set HARBOR (e.g. HARBOR=harbor.daalu.io)" >&2
  exit 2
fi

# ── image table ─────────────────────────────────────────────────────────
# "<source-repo>|<tag>|<dest-basename>" — tags pinned to the chart's
# global.images block (deploy/charts/nv-config-manager-1.3.0/
# values.yaml). KEEP IN SYNC on a chart bump (and with values.py basenames).
#
# The six NVIDIA service images use the placeholder; $NVIDIA_SRC rewrites
# it at pull time. The rest are public infra images pinned by the chart.
IMAGES=(
  "${NVIDIA_PLACEHOLDER}/nv-config-manager|1.3.0|nv-config-manager"
  "${NVIDIA_PLACEHOLDER}/nv-config-manager-ui|1.3.0|nv-config-manager-ui"
  "${NVIDIA_PLACEHOLDER}/nv-config-manager-kea|1.3.0|nv-config-manager-kea"
  "${NVIDIA_PLACEHOLDER}/nv-config-manager-kea-admin|1.3.0|nv-config-manager-kea-admin"
  "${NVIDIA_PLACEHOLDER}/nv-config-manager-nautobot|1.3.0|nv-config-manager-nautobot"
  "${NVIDIA_PLACEHOLDER}/nv-config-manager-nats-ready|1.3.0|nv-config-manager-nats-ready"
  "hashicorp/http-echo|1.0|http-echo"
  "docker.io/alpine/kubectl|1.35.4|kubectl"
  "docker.io/library/busybox|1.36|busybox"
  "docker.io/library/redis|7-alpine|redis"
  "docker.io/library/nats|2.10-alpine|nats"
  "docker.io/natsio/nats-box|0.14.3|nats-box"
  "docker.io/temporalio/server|1.29|server"
  "docker.io/temporalio/admin-tools|1.29|admin-tools"
  "docker.io/temporalio/ui|v2.37.4|ui"
  # Optional — only pulled by the chart when UI / SPIFFE are enabled. Safe
  # to mirror eagerly so they're ready if a tenant turns those on.
  "quay.io/oauth2-proxy/oauth2-proxy|v7.6.0|oauth2-proxy"
  "ghcr.io/spiffe/spiffe-helper|0.8.0|spiffe-helper"
)

# ── engine detection ──────────────────────────────────────────────────────
if [[ -z "$ENGINE" ]]; then
  if command -v skopeo >/dev/null 2>&1; then ENGINE=skopeo
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else echo "ERROR: need 'skopeo' or 'docker' on PATH" >&2; exit 2; fi
fi
echo "engine=$ENGINE  harbor=$HARBOR/$HARBOR_PROJECT  cache=$CACHE_DIR  dry_run=$DRY_RUN"
[[ -z "$NVIDIA_SRC" ]] && echo "NOTE: NVIDIA_SRC unset — NVIDIA service images will be SKIPPED."
mkdir -p "$CACHE_DIR"

run() { if [[ "$DRY_RUN" == "1" ]]; then echo "  + $*"; else echo "  + $*"; "$@"; fi; }

# Does a remote image already exist in Harbor? (skip work if so)
dest_exists() {
  local dest="$1"
  if [[ "$ENGINE" == "skopeo" ]]; then
    skopeo inspect "docker://$dest" >/dev/null 2>&1
  else
    docker manifest inspect "$dest" >/dev/null 2>&1
  fi
}

mirror_one() {
  local src="$1" tag="$2" base="$3"
  local dest="$HARBOR/$HARBOR_PROJECT/$base:$tag"

  # Rewrite the NVIDIA placeholder to the real source, or skip if unset.
  if [[ "$src" == "$NVIDIA_PLACEHOLDER/"* ]]; then
    if [[ -z "$NVIDIA_SRC" ]]; then
      echo "SKIP  $src:$tag  (NVIDIA_SRC unset)"; return 0
    fi
    src="${src/$NVIDIA_PLACEHOLDER/$NVIDIA_SRC}"
  fi

  echo "== $src:$tag  ->  $dest"
  if dest_exists "$dest"; then
    echo "  already in Harbor — skip"; return 0
  fi

  if [[ "$ENGINE" == "skopeo" ]]; then
    # OCI-layout cache → download once. --all copies every arch/digest.
    local oci="$CACHE_DIR/$base"
    if [[ -f "$oci/index.json" ]] && skopeo inspect "oci:$oci:$tag" >/dev/null 2>&1; then
      echo "  cache hit ($oci:$tag) — no pull"
    else
      run skopeo copy --all "docker://$src:$tag" "oci:$oci:$tag"
    fi
    run skopeo copy --all "oci:$oci:$tag" "docker://$dest"
  else
    # docker's local store IS the persistent cache.
    if docker image inspect "$src:$tag" >/dev/null 2>&1; then
      echo "  cache hit (local docker store) — no pull"
    else
      run docker pull "$src:$tag"
    fi
    run docker tag "$src:$tag" "$dest"
    run docker push "$dest"
  fi
}

fail=0
for entry in "${IMAGES[@]}"; do
  IFS='|' read -r src tag base <<<"$entry"
  if ! mirror_one "$src" "$tag" "$base"; then
    echo "  !! FAILED: $src:$tag" >&2; fail=1
  fi
done

echo
if [[ "$fail" == "0" ]]; then
  echo "DONE. Now set CONFIG_MANAGER_HARBOR_REGISTRY=$HARBOR on the hub"
  echo "(daalu-automation-secrets) so render_values() repoints the chart here."
else
  echo "COMPLETED WITH FAILURES — see !! lines above." >&2; exit 1
fi
