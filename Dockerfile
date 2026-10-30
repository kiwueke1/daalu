FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# helm — only the config-manager-controller entrypoint uses it (it runs
# `helm upgrade --install` of the vendored NV-CM chart). Pinned and
# arch-aware so the same image works on amd64 and arm64 builders. Every
# other entrypoint (api, workers, the other controllers) ignores it.
ARG HELM_VERSION=v3.16.4
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" \
       -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp \
    && mv "/tmp/linux-${arch}/helm" /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf /tmp/helm.tgz "/tmp/linux-${arch}" \
    && helm version --short

# kubectl — the vendored NV-CM Deployer (config-manager-controller) requires
# both helm AND kubectl on PATH: its prereqs step checks for kubectl, and it
# shells out to it for CRD apply/wait. Pinned and arch-aware to match helm.
ARG KUBECTL_VERSION=v1.31.4
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
       -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

WORKDIR /app

# Dependency layer first so the source-code change doesn't bust the cache.
# Install from the fully-pinned lock (requirements.lock) — NOT the open
# `>=` ranges in pyproject — so a rebuild can never silently float a
# transitive dep (see the 2026-06-15 fastapi 0.137 _IncludedRouter
# outage). Regenerate the lock with the command documented in its header.
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock

COPY src/ src/
# --no-deps: deps are already satisfied by the lock above; this only
# installs the daalu-automation package itself (editable) without letting
# pip re-resolve and upgrade anything.
RUN pip install --no-cache-dir --no-deps -e .

COPY alembic.ini ./
COPY migrations/ migrations/

# Vendored, pinned NV-CM Helm charts. The config-manager-controller's
# `config_manager_chart_dir` setting defaults to `deploy/charts`, resolved
# against this WORKDIR (/app). Only the controller reads these; harmless
# weight for the other entrypoints.
COPY deploy/charts/ deploy/charts/

# Vendored NV-CM bootstrap jobs (Nautobot migration runner + bootstrap data).
# The Deployer's `setup-jobs-pvc` step loads these into each tenant's
# `nautobot-custom-jobs` PVC; Nautobot's `run-migrations` init container then
# runs `nv_config_manager_jobs/migration_runner.py` from it. The Deployer finds
# them via `find_project_root()` (which resolves to /app because deploy/ exists)
# + the relative `components/nautobot/nv_config_manager_jobs` path — so they
# MUST live at /app/components/…. Without this, run-migrations crashloops on a
# missing migration_runner.py and Nautobot never reaches Ready.
COPY components/ components/

RUN useradd -m -u 1000 daalu && chown -R daalu:daalu /app
USER daalu

# Build identity — surfaced by GET /version and shown on the /help page.
# CI passes --build-arg BUILD_SHA=$GITHUB_SHA --build-arg BUILD_TIME=$(date -u +%FT%TZ).
ARG BUILD_SHA=unknown
ARG BUILD_TIME=unknown
ENV BUILD_SHA=${BUILD_SHA}
ENV BUILD_TIME=${BUILD_TIME}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["daalu", "server", "--host", "0.0.0.0", "--port", "8000"]
