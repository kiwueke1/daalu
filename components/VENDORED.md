# Vendored: NV-CM bootstrap jobs (`components/`)

`components/nautobot/nv_config_manager_jobs/` is a **vendored copy** of
NVIDIA's upstream NV-CM Nautobot jobs, Apache-2.0 licensed (SPDX headers
retained on every file).

## Why this is in the image

The vendored `Deployer`
(`src/nv_config_manager_installer/`, see its `VENDORED.md`) has a
`setup-jobs-pvc` step that uploads these jobs into each tenant's
`nautobot-custom-jobs` PVC (mounted at `/opt/nautobot/jobs`). Nautobot's
`run-migrations` **init container** then runs
`python /opt/nautobot/jobs/nv_config_manager_jobs/migration_runner.py` from
that PVC. If the jobs are absent, `run-migrations` crashloops on a missing
`migration_runner.py` and Nautobot never reaches Ready — wedging the whole
tenant stack.

The Deployer locates these jobs at the **relative** path
`components/nautobot/nv_config_manager_jobs` (`deployer._BOOTSTRAP_JOBS_PATH`),
resolved against `find_project_root()`. In the controller image the project
root resolves to `/app` (it contains `deploy/`), so these files **must** live
at `/app/components/nautobot/nv_config_manager_jobs/`. The Dockerfile
`COPY components/ components/` puts them there.

`content.include_bootstrap_jobs` defaults to `True` in the upstream schema, and
`deployer_config.build_install_config` leaves it at the default — so every
tenant provision loads these.

## Upstream source

- Repo: `nvidia-tools/nv-config-manager` (NVIDIA, Apache-2.0)
- Path: `components/nautobot/nv_config_manager_jobs/`
- Commit: `7455da6ca9a9db0370dd3872cc16f0dbddf4f4b8` (tag `1.3.0`, 2026-07-06)
- Matches vendored chart: `deploy/charts/nv-config-manager-1.3.0`

## Re-vendoring

Copy the upstream `components/nautobot/nv_config_manager_jobs/` over this dir,
excluding `__pycache__`/`*.pyc`. Keep the chart, installer, and these jobs on
the **same** upstream tag so the migration runner matches the chart schema.
Do **not** hand-edit — Daalu-specific behaviour lives in
`daalu_automation.config_manager_controller.{deployer_runner,deployer_config}`.
