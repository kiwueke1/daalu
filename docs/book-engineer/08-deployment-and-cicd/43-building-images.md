# 43. Building images

CI builds two images per commit: `daalu-backend` and
`daalu-frontend`. Both pushed to GHCR.

---

## Backend Dockerfile

`Dockerfile` at repo root, base `python:3.11-slim-bookworm`. Beyond the app
itself it installs pinned, arch-aware **helm** and **kubectl** binaries —
only the config-manager-controller entrypoint uses them (the vendored NV-CM
Deployer requires both on PATH), but they ride in the shared image.

The dependency install is the part with history:

```dockerfile
# Dependency layer first so the source-code change doesn't bust the cache.
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock
COPY src/ src/
# --no-deps: deps are already satisfied by the lock above
```

### The lockfile policy (PR #98 — post-incident)

Until 2026-06-15 the image installed straight from `pyproject.toml`'s open
`>=` floors with **no lockfile** — so any rebuild silently floated
transitive deps. That is exactly how a rebuild for an *unrelated* change
shipped fastapi `0.136.3 → 0.137.0`, whose `_IncludedRouter` wrapper (no
`.path`) crashed `prometheus-fastapi-instrumentator.get_route_name()` on
nearly every request: a **full-API 500 storm in prod** (see chapter 49's
runbook entry). The same rebuild floated kubernetes-client to 36.0.2 and
broke the kubectl console's `_raw_get` (chapter 78).

The rules now:

- **`requirements.lock`** is fully pinned, generated *inside the exact base
  image* (`uv pip compile pyproject.toml --extra dev --python-version 3.11`,
  the full command is in the lock's header). Do not hand-edit; regenerate
  after any `pyproject.toml` dep change.
- The Dockerfile installs `-r requirements.lock`, then the package itself
  with **`--no-deps`**. `pyproject.toml` stays the source of truth for
  *direct* deps; the lock is the build's truth for everything.
- `fastapi` additionally carries a `<0.137` cap in `pyproject.toml:18`
  (commit 5d45420, the emergency stop before the lock landed); un-capping is
  tracked in #97 and should be done by bumping *in the lock* with the
  instrumentator verified against the new router shape.

The build args `BUILD_SHA` and `BUILD_TIME` are passed by CI
and shown on the `/version` endpoint and `/help` page.

---

## Frontend Dockerfile

`frontend/Dockerfile`:

```dockerfile
FROM node:20-alpine AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --frozen-lockfile

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
RUN npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
USER 1000
EXPOSE 3000
CMD ["node", "server.js"]
```

Uses Next.js standalone output to skip shipping node_modules.

---

## CI workflow

`.github/workflows/build-images.yml`:

```yaml
on:
  push:
    paths:
      - "src/**"
      - "frontend/**"
      - "Dockerfile"
      - "frontend/Dockerfile"
      - "deploy/wg-hub/**"
      - "deploy/edge/**"
      - "deploy/helm/daalu-edge/**"
      - "pyproject.toml"
      - "alembic.ini"
      - "migrations/**"
      - ".github/workflows/build-images.yml"

concurrency:
  group: build-${{ github.ref }}
  cancel-in-progress: false

jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/kiwueke1/daalu-automation
          tags: |
            type=raw,value=${{ github.sha }}
            type=raw,value=latest,enable={{is_default_branch}}
      - id: buildmeta
        run: echo "build_time=$(date -u +%FT%TZ)" >> "$GITHUB_OUTPUT"
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          build-args: |
            BUILD_SHA=${{ github.sha }}
            BUILD_TIME=${{ steps.buildmeta.outputs.build_time }}
          cache-from: type=gha,scope=daalu-backend
          cache-to: type=gha,mode=max,scope=daalu-backend
  frontend:
    # similar
```

Image tags: `<sha>` (always) + `latest` (on main only). The
SHA tag is what `deploy/scripts/install.sh` pins to.

The path filter means docs-only or chart-only commits don't
trigger a rebuild. `install.sh` walks back to find the last
SHA that *would* have triggered.

---

## Concurrency policy

`group: build-${{ github.ref }}` with `cancel-in-progress:
false` — runs on the same branch don't step on each other,
but they queue. Important because a fast follow-up commit
must not overwrite a slower previous build's tag.

---

## Cache strategy

`cache-from`/`cache-to` use GitHub Actions cache scoped per
image. Typical second-build time: 30–60 seconds.

We've considered registry-cached builds (faster cross-runner)
but the GHA cache is good enough today.

---

## Image hardening

- Non-root user (`daalu`, UID 1000).
- Read-only root filesystem at runtime via securityContext.
- No package manager in runtime image.
- Pinned base image versions in Dockerfile.

`docker scout` runs on every push; HIGH+ findings are
flagged in the PR.

---

## What's next

Chapter 44 — the Helm chart that deploys this image to k8s.
