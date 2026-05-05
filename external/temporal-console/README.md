# temporal-console

Friendly operator UI for [Temporal](https://temporal.io/). Built as a
reusable deployable — one binary, rebranded per deployment via
environment variables — so the same image can serve an Ajani-branded
console against one cluster and a differently-branded console against
another.

## What it does

- **Dashboard:** active workflow counts + recent completions/failures
  + schedule next-run times, with HTMX auto-polling.
- **Workflows:** list with filters (namespace, status, type, time
  window); detail view with pretty history timeline, payload inspector,
  pending activities, signals / terminate / cancel buttons, and a
  configurable deep-link back to the owning app.
- **Schedules:** list + detail; pause, resume, trigger, view next-run.
- **Start workflow:** dynamic form — pick namespace + workflow type,
  paste JSON input, submit.

## Configuration

All via environment variables:

```bash
# Temporal
TEMPORAL_HOST=temporal-frontend.temporal.svc.cluster.local:7233
TEMPORAL_NAMESPACES=default                    # comma-separated
NAMESPACE_LABELS=default:Ajani Workflows       # optional pretty names

# Branding
BRAND_NAME="Ajani Workflows"
BRAND_SUBTITLE="orchestration console"
BRAND_ACCENT=#7aa2ff                            # Tailwind accent color
DEEP_LINK_TEMPLATE=https://ajani.daalu.io/workflows/{workflow_id}

# Keycloak OIDC
OIDC_ISSUER=https://auth.daalu.io/realms/daalu
OIDC_CLIENT_ID=temporal-console
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URI=https://workflows.daalu.io/auth/callback

# App
SESSION_SECRET=$(openssl rand -hex 32)
LOG_LEVEL=INFO
```

## Running

### Local dev (against a port-forwarded Temporal)

```bash
kubectl -n temporal port-forward svc/temporal-frontend 7233:7233 &

pip install -e .
TEMPORAL_HOST=localhost:7233 \
TEMPORAL_NAMESPACES=default \
BRAND_NAME="Local" \
OIDC_ISSUER= \
SESSION_SECRET=devsecret \
uvicorn temporal_console.api.main:app --reload
```

When `OIDC_ISSUER` is empty, auth is disabled — for local dev only.

### In-cluster

```bash
helm install temporal-console ./chart \
  --set temporal.host=temporal-frontend.temporal.svc.cluster.local:7233 \
  --set temporal.namespaces=default \
  --set brand.name="Ajani Workflows" \
  --set deepLink.template="https://ajani.daalu.io/workflows/{workflow_id}" \
  --set oidc.issuer=https://auth.daalu.io/realms/daalu \
  --set oidc.clientId=temporal-console \
  --set-file oidc.clientSecret=./client_secret.txt \
  --set ingress.host=workflows.daalu.io
```

## Architecture

```
src/temporal_console/
├── api/
│   ├── main.py              # FastAPI app, routes, middleware
│   ├── templates/           # Jinja2 + HTMX + Tailwind
│   └── static/
├── auth.py                  # Authlib OIDC client
├── client.py                # Temporal client (cached, async)
├── config.py                # pydantic-settings
└── __init__.py
chart/                       # Helm chart
Dockerfile
```

Zero imports from any project-specific code. The only deployment-specific
thing is the config.
