# Vendored third-party charts

We **do not** copy or fork NVIDIA Config Manager's source. Only its **Helm chart** is vendored
here (pinned by version), its **images** are mirrored to daalu's Harbor, and our integration code
talks to its **REST API** (see `core/configmgr/`). See
`docs/design/nv-config-manager-integration.md` (§"how will we implement it") for the rationale.

## `nv-config-manager-<version>/`

A pinned, unmodified copy of the upstream chart from `nv-config-manager/deploy/helm` at the
tagged version. The `config_manager_controller` renders values and runs
`helm upgrade --install` of this directory into the target cluster over the WireGuard tunnel.

**To bump the version:**
1. `cp -r <upstream>/deploy/helm/. deploy/charts/nv-config-manager-<newver>/`
2. Re-capture images: `grep -oE 'repository: [^ ]+' .../values.yaml | awk '{print $2}' | sort -u
   > deploy/charts/nv-config-manager-<newver>.images.txt`
3. Mirror images into Harbor (see below) and set the controller's pinned `chart_version`.
4. Roll out per tenant by bumping `config_manager_tenants.chart_version`; reconcile applies it.

## `nv-config-manager-<version>.images.txt`

The upstream image repositories the chart references (repositories only — the tags live in the
chart's `global.images` block). **This chart has no `global.imageRegistry` knob**: each image is
pulled as `<repository>:<tag>` directly, so mirroring to Harbor means pushing to
`harbor.daalu.io/nv-config-manager/<basename>:<tag>` *and* having the controller override each
`global.images.<key>.repository` to that path — which `config_manager_controller/values.py`'s
`render_values()` does when `harbor_registry` is set. Upstream NVIDIA images use
`registry.example.com/nvidia/...` placeholders; supply the real source at mirror time.

Use the idempotent, download-once mirror script (it pins the tags and shares the
basename/project convention with `values.py`):

```
HARBOR=harbor.daalu.io NVIDIA_SRC=<real-nvidia-source> \
  deploy/scripts/config-manager/mirror-images.sh
```

See `deploy/scripts/config-manager/README.md` for the full go-live sequence.

## `nv-config-manager-<version>-api-specs/`

The upstream OpenAPI specs, kept alongside the chart so `core/configmgr/` clients can be
regenerated/verified against the exact pinned version.
