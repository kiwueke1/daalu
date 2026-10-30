# daalu-automation Helm chart

Production deployment artifact for the AI Operations Command Center.
The chart provisions everything the platform needs (API, workers,
beat, agents, frontend, Postgres, Redis, networkpolicies, ingress) in
one namespace.

## Install

The chart **does not** create the application secret — keep credentials
out of git. Use the bootstrap script:

```bash
export KUBECONFIG=$KUBECONFIG
./deploy/scripts/install.sh
```

That script:

1. Creates the `daalu-automation` namespace.
2. Generates the GHCR pull secret (`ghcr`) from your `gh` token.
3. Generates the `daalu-automation-secrets` secret from `./.env`.
4. Runs `helm upgrade --install daalu-automation
   deploy/helm/daalu-automation -n daalu-automation`.

To install manually (without the helper):

```bash
kubectl create ns daalu-automation
kubectl -n daalu-automation create secret docker-registry ghcr \
  --docker-server=ghcr.io --docker-username=$GITHUB_USER \
  --docker-password=$GHCR_PAT --docker-email=$GITHUB_USER@users.noreply.github.com
kubectl -n daalu-automation create secret generic daalu-automation-secrets \
  --from-env-file=.env
helm upgrade --install daalu-automation deploy/helm/daalu-automation \
  -n daalu-automation \
  --set image.backend.tag=$(git rev-parse HEAD) \
  --set image.frontend.tag=$(git rev-parse HEAD)
```

## Values

The full list is in `values.yaml`. The ones you actually tune per
environment:

| Key | Default | Notes |
|---|---|---|
| `ingress.host` | `daalu.example.com` | Must have a DNS record pointed at the cluster ingress IP |
| `ingress.className` | `traefik` | k3s default IngressClass |
| `ingress.clusterIssuer` | `letsencrypt-prod` | Provisioned with cert-manager |
| `image.backend.tag` | `latest` | Override with the git SHA in CI |
| `image.frontend.tag` | `latest` | Override with the git SHA in CI |
| `api.replicas` / `worker.replicas` / `agents.replicas` | 3 / 4 / 2 | Horizontal scale |
| `beat.replicas` | 1 | MUST stay 1 |
| `postgres.enabled` / `redis.enabled` | true / true | Set false to point at external instances |

## Upgrade

```bash
helm upgrade daalu-automation deploy/helm/daalu-automation \
  -n daalu-automation --reuse-values \
  --set image.backend.tag=$(git rev-parse HEAD) \
  --set image.frontend.tag=$(git rev-parse HEAD)
```

The API deployment runs `alembic upgrade head` in an init container at
startup, so schema migrations roll out automatically with the image.

## Uninstall

```bash
helm uninstall daalu-automation -n daalu-automation
kubectl delete pvc -n daalu-automation daalu-postgres-data  # WIPES THE DB
```
