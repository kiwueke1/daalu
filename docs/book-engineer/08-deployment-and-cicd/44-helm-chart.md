# 44. The Helm chart

`deploy/helm/daalu-automation/` is the operator-app chart.
This chapter is the structural overview; the per-template
details are scattered through earlier chapters (API in
Chapter 4, workers in Chapter 7, NetworkPolicies in Chapter
54).

---

## Chart.yaml

```yaml
apiVersion: v2
name: daalu-automation
description: The Daalu operator app
type: application
version: 0.1.0
appVersion: "0.1.0"
```

`appVersion` matches the Python package version. `version`
is the chart version, bumped when chart structure changes.

---

## Top-level values

`values.yaml`. Heavily commented; we'll show the high-level
sections:

```yaml
image:
  backend:
    repository: ghcr.io/kiwueke1/daalu-automation
    tag: latest               # overridden by install.sh
  frontend:
    repository: ghcr.io/kiwueke1/daalu-automation-frontend
    tag: latest
  pullPolicy: IfNotPresent
  pullSecret: ghcr            # k8s pull-secret name

api:
  replicas: 3
  runAlembicInitContainer: true
  resources:
    requests: { cpu: 200m, memory: 256Mi }
    limits:   { cpu: 1, memory: 1Gi }

worker:
  replicas: 3
  concurrency: 8
  resources: {...}

beat:
  replicas: 1
  resources: {...}

agents:
  replicas: 2
  resources: {...}

executor:
  replicas: 1
  concurrency: 4
  serviceAccountName: daalu-executor
  jwtScope: executor
  resources: {...}

frontend:
  replicas: 2
  resources: {...}

postgres:
  enabled: true
  storage:
    storageClassName: local-path
    size: 50Gi

redis:
  enabled: true
  storage: { ... }

wireguardHub:
  enabled: true
  basePrefix: 10.200.0.0/16
  perTenantPrefix: /24

ingress:
  enabled: true
  className: traefik
  host: ops.example.com
  tls:
    enabled: true
    secretName: ops-daalu-io-tls

networkPolicies:
  enabled: true

remediator:
  enabled: true
  serviceAccountName: daalu-remediator

existingSecret: daalu-automation-secrets
```

The chart is opinionated — it expects an existing Secret
named `daalu-automation-secrets` with all environment
variables. The Secret is created out-of-band by `install.sh`.

---

## Templates

`templates/` directory:

```
templates/
├── _helpers.tpl              # standard labels/selectors
├── api.yaml                  # Deployment + Service for daalu-api
├── workers.yaml              # Deployments for worker/beat/agents/executor
├── frontend.yaml             # Deployment + Service for daalu-frontend
├── postgres.yaml             # StatefulSet for daalu-postgres
├── redis.yaml                # Deployment for daalu-redis
├── wireguard-hub.yaml        # Deployment + Service for wg-hub
├── ingress.yaml              # Ingress (Traefik)
├── networkpolicy.yaml        # default-deny + per-component allows
├── remediator-rbac.yaml      # SA + ClusterRoleBindings for executor
└── pdb.yaml                  # PodDisruptionBudgets
```

Each template renders the resources for one logical
component. We avoid sprawl — one file per logical group
rather than one per resource.

---

## Helper templates

`_helpers.tpl` exposes:

- `daalu.labels` — the standard label set
  (`app.kubernetes.io/name`, `app.kubernetes.io/instance`,
  etc.).
- `daalu.podSecurityContext` — fsGroup + runAsNonRoot.
- `daalu.containerSecurityContext` — capabilities, RO root
  fs.
- `daalu.envFrom` — list of envFrom that includes the
  `existingSecret`.
- `daalu.commonEnv` — env vars common across all pods.
- `daalu.backendImage` — assembles the image reference.

These keep the per-component templates tight.

---

## Customer-side charts

Two additional charts ship from this repo:

- `deploy/helm/daalu-edge/` — federation chart (Chapter 25).
- `deploy/helm/daalu-vllm/` — vLLM chart (Chapter 30).

Both follow the same structure (Chart.yaml, values.yaml,
templates/). They're published to GHCR as OCI artifacts
that customers `helm install oci://...`.

---

## Linting

`helm lint deploy/helm/daalu-automation` runs in CI on every
chart change. We've found this catches a lot of yaml
syntax issues that would otherwise show up only at apply
time.

---

## What's next

Chapter 45 — `install.sh`, the manual rollout script that
sits between this chart and a deployed cluster.
