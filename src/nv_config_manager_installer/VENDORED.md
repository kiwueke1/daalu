# Vendored: nv_config_manager_installer

This package is a **vendored copy** of NVIDIA's upstream NV-CM installer
(`installer/src/nv_config_manager_installer/`), Apache-2.0 licensed (SPDX
headers retained on every file).

Daalu's `config_manager_controller` drives the upstream `Deployer` directly
(import-as-library) to install the NV-CM Helm chart. A bare `helm upgrade
--install` deadlocks: the chart's `secret-assembler` pre-install hook waits on
secrets (`redis-password`, `nautobot-token`, `device-creds`, `cluster-*-app`,
…) that are only minted later in the same release. The `Deployer` pre-creates
those secrets (and the namespace) *before* helm runs, breaking the deadlock.
See `docs/design/nv-config-manager-integration.md` and engineer chapter 64.

## What was changed vs upstream

- **`tui/` excluded** — the Textual TUI is interactive-only and pulls a
  `textual` dependency we don't want in a runtime container. Nothing on the
  `Deployer` code path imports it.
- **`air_sim/` excluded** (new in 1.3.0) — `air_sim/sim_manager.py` imports
  `air_sdk` (`nv-air-sdk`), which is not a Daalu dependency, and
  `air_sim/cli.py` imports `tui.air_sim.app` at module scope.
- **`cli.py` excluded** (new in 1.3.0) — it gained a *top-level*
  `from nv_config_manager_installer.air_sim.cli import air_sim` (1.3.0
  `cli.py:32`), so it is unimportable without the two packages above. Up to
  1.2.2-rc.23 the TUI import was lazy inside `init`, which is why keeping
  `cli.py` was safe then. Nothing on the `Deployer` path imports it — Daalu
  only imports `deployer` and `schema`.
- Everything else copied verbatim. Do **not** hand-edit these modules;
  Daalu-specific behaviour (shared GatewayClass, Harbor image overrides,
  skipping the NodePort gateway patch) lives in
  `daalu_automation.config_manager_controller.deployer_runner` /
  `deployer_config`, which subclass / configure the upstream `Deployer`
  rather than forking it.

## Upstream source

- Repo: `nvidia-tools/nv-config-manager` (NVIDIA, Apache-2.0)
- Path: `installer/src/nv_config_manager_installer/`
- Commit: `7455da6ca9a9db0370dd3872cc16f0dbddf4f4b8` (tag `1.3.0`, 2026-07-06)
- Matches vendored chart: `deploy/charts/nv-config-manager-1.3.0`

## Re-vendoring

Copy the upstream `*.py` modules over these, excluding `tui/`, `air_sim/`,
`cli.py`, `tests/`, `scripts/` (see "What was changed" for why the last two
are excluded — copying `cli.py` in will break the import chain). Re-run `tests/config_manager_controller/test_nvcm_vendor_import.py`
to confirm the `Deployer` import chain still loads against Daalu's pinned deps.
