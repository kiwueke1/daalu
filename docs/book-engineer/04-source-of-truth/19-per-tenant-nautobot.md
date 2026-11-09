# 19. Per-tenant Nautobot (operator-cluster and customer-cluster)

> **Largely superseded by NV-CM (chapter 23).** For a tenant that enables
> config management, the **NV-CM stack bundles its own Nautobot** — so the
> `config-manager-controller` provisions Nautobot as part of the larger stack
> and **no separate `nautobot-controller` provision is needed**.
> `core/sot/nautobot.py::_load_credentials` prefers the NV-CM-bundled Nautobot
> and falls back to a standalone one. The `nautobot-controller` described below
> remains only as a **Nautobot-only fallback** (a tenant that wants the SoT
> without the rest of NV-CM, or a BYO arrangement). New installs should use
> NV-CM; this chapter stays accurate for the fallback path.

Replaces the pre-2026-05 shared-Nautobot-with-ObjectPermission-slice
model. Every tenant that opts into the hosted-Nautobot tier now gets
their own isolated Nautobot stack — own Postgres, own admin user, own
URL — deployed by the **nautobot-controller** service.

Why we pivoted: the shared-Nautobot model used a single
``ObjectPermission`` per tenant with a Django-ORM-style constraint
(``tenant__slug``) as the *only* gate between customer A and customer
B's data. Nautobot 2.x quietly removed the ``slug`` field from Tenant
— the constraint either silently mismatched (every read returns
empty) or silently no-op'd (cross-tenant data leak), depending on the
Django version. The integration test pinned the wrong string. A
single upstream rename had been one constraint typo away from a
silent data-isolation failure on every upgrade.

Per-tenant Nautobot eliminates this class of bug structurally: there
is nothing to constrain because there is nothing shared.

Customer-facing walkthrough: user-guide/30.
Engineer architecture cross-references: chapter 14 (reconciler),
chapter 15 (Nautobot adapter), chapter 17 (executor identity), chapter
18 (the prior shared-Nautobot operator runbook — now deprecated).

---

## Architecture

Four moving parts:

1. **daalu-api** — owns the customer-facing
   ``/api/v1/onboarding/nautobot/provision`` endpoint. Mints a
   ``purpose='nautobot-provision'`` service-token JWT and calls the
   controller's REST API.

2. **nautobot-controller** — its own Deployment in
   ``daalu-automation``. Two surfaces: a small REST API (``POST /tenants/{id}``,
   ``GET``, ``DELETE``) and a 30-s reconcile loop that materialises
   K8s state for every ``nautobot_tenants`` row.

3. **The `nautobot_tenants` table** — operator-side materialisation
   state, one row per tenant. Fields:
    - ``target_cluster_tunnel_id`` — NULL → operator cluster, NOT NULL
      → customer cluster (via WireGuard).
    - ``namespace`` — deterministic ``nautobot-<tenant-slug>``.
    - ``hostname`` — ``<slug>.sot.example.com`` (operator-cluster only;
      NULL in customer-cluster mode).
    - Encrypted bootstrap credentials (admin token, Postgres password,
      Nautobot secret key).
    - State machine: ``pending → provisioning → active`` (or
      ``error``); ``active → deleting → destroyed`` on teardown.

4. **Per-tenant Nautobot stack** — namespace + ConfigMap + Secret +
   Postgres StatefulSet + Redis Deployment + Nautobot web/worker/
   scheduler Deployments + Service + (operator-cluster only)
   Ingress. Mirrors the shape of the legacy shared-Nautobot stack
   at ``deploy/k8s/nautobot/``, just stamped once per tenant.

```
                ┌─────────────────────────┐
                │       daalu-api         │
                │ POST /onboarding/       │
                │   nautobot/provision    │
                └────────────┬────────────┘
                             │ JWT (purpose=nautobot-provision)
                             ▼
              ┌──────────────────────────────┐
              │     nautobot-controller      │
              │   reconciles nautobot_tenants│
              └──────────────┬───────────────┘
                             │
                ┌────────────┴────────────┐
                │                         │
       operator cluster              customer cluster
       (in-cluster kubeconfig)       (kubeconfig from cluster_tunnel)
                │                         │
   ┌────────────▼────────────┐  ┌─────────▼──────────────┐
   │ Namespace: nautobot-X   │  │ Namespace: nautobot-X  │
   │ + Postgres + Redis +    │  │ (same set, no Ingress  │
   │   Nautobot + Ingress    │  │  — reached via wg)     │
   └─────────────────────────┘  └────────────────────────┘
```

---

## Two deployment targets

### Operator cluster (Phase 1, default)

`target_cluster_tunnel_id IS NULL` on the row. The controller uses
its in-cluster ServiceAccount + ClusterRole to stamp the per-tenant
Nautobot inside the hub cluster.

Resource usage per tenant: ~700 MiB memory (postgres + web + worker
+ scheduler + redis), ~200m CPU steady-state. A single mid-size node
(e.g. 16 vCPU / 32 GiB) can hold ~25–30 tenants comfortably before requiring a node-pool
expansion.

Public hostname is `<slug>.sot.example.com`. One **operator prereq**:
configure a `*.sot.example.com` Cloudflare CNAME pointing at
`<ingress-ip>` (the traefik LB) so cert-manager can solve HTTP-01
for new subdomains. Per-tenant certs are issued by `letsencrypt-prod`
the first time the Ingress goes up (~60 s).

### Customer cluster (Phase 2, requires federation)

`target_cluster_tunnel_id IS NOT NULL`. The customer has already
onboarded a cluster via the WireGuard federation flow (chapter 13).
The controller decrypts the kubeconfig from that cluster's
`Integration(provider="kubernetes")` row, then targets the customer's
K8s API. Manifests are identical except:

- No Ingress — daalu-api reaches the pod through the wg tunnel via
  in-cluster Service DNS (`http://nautobot.nautobot-<slug>.svc.cluster.local`).
- Looser NetworkPolicy on web ingress (any pod can reach :8080 — the
  wireguard interface is the perimeter, not the K8s network).

Why this matters for customers: their network inventory data is at
rest on **their** storage. Even if Daalu's hub is compromised, the
attacker can't extract it without first compromising the customer's
cluster.

The same controller code handles both modes — switching is a
per-row flag, not a separate deployment.

---

## Deploying the controller

Already wired:

- `deploy/k8s/nautobot-controller/` kustomize stack:
  - `rbac.yaml` — ServiceAccount + ClusterRole + ClusterRoleBinding.
    The role grants create/patch/delete on Namespaces (cluster
    scope) and the resource kinds inside per-tenant namespaces
    (deployments, statefulsets, services, ingresses, configmaps,
    secrets, networkpolicies, pvcs, pods).
  - `deployment.yaml` — single-replica Deployment + Service.
    `command: ["daalu", "nautobot-controller", "--port", "8082"]`.
- `deploy/argocd/applications/nautobot-controller.yaml` — Argo app
  that points at the kustomize path. Auto-sync + auto-prune.
- `deploy/k8s/api/deployment.yaml` — daalu-api gets
  `NAUTOBOT_CONTROLLER_URL=http://nautobot-controller.daalu-automation.svc.cluster.local`
  injected as a literal env var.
- `.github/workflows/cd.yaml` — CD now SHA-bumps
  `deploy/k8s/nautobot-controller/deployment.yaml` alongside api/workers/frontend.

Install on a fresh cluster:

```bash
kubectl apply -f deploy/argocd/applications/nautobot-controller.yaml
```

Argo creates the controller Deployment + RBAC. Verify:

```bash
kubectl get deploy -n daalu-automation nautobot-controller
# READY 1/1 within ~30 s.

kubectl logs -n daalu-automation deploy/nautobot-controller | head -3
# Expect: nautobot_controller.reconcile_loop_started
```

---

## Provisioning a tenant (operator-cluster path)

The customer-facing flow is unchanged — the SoT wizard's
"Use the hosted Nautobot" tile calls
`POST /api/v1/onboarding/nautobot/provision` as before. What's
different is the backend:

1. daalu-api: mints a 300 s `purpose='nautobot-provision'` JWT.
2. daalu-api: `POST nautobot-controller:80/tenants/{tenant_id}` with
   the JWT.
3. Controller: writes a `nautobot_tenants` row with
   `state='pending'` + freshly-generated credentials encrypted at
   rest.
4. Reconcile loop (≤ 30 s): applies manifests, transitions to
   `state='provisioning'`.
5. Reconcile loop (≤ next ~120 s): waits for the web Deployment's
   `readyReplicas ≥ 1`, transitions to `state='active'`.
6. daalu-api polls the controller every 5 s; on `active`, copies the
   returned URL + admin token into the customer's
   `Integration(provider='nautobot')` row (encrypted).
7. The wizard advances to the SSH-credentials step.

End-to-end timing: 60-180 s on a cold tenant (image pull + Django
migrations).

---

## Provisioning a tenant (customer-cluster path)

Phase 2 — currently available to operators willing to set the
`target_cluster_tunnel_id` field directly. The wizard doesn't surface
this yet; it's a deliberate gate so we ship Phase 1 first.

To provision into a customer cluster:

1. Verify the customer's cluster is onboarded:

    ```sql
    SELECT id, status FROM cluster_tunnels
    WHERE tenant_id = '<daalu-tenant-uuid>' AND status = 'connected';
    ```

2. Confirm the matching `Integration(provider='kubernetes')` row has
   a non-empty kubeconfig:

    ```sql
    SELECT (config->>'kubeconfig') IS NOT NULL AS has_kubeconfig
    FROM integrations
    WHERE tenant_id = '<daalu-tenant-uuid>' AND provider = 'kubernetes';
    ```

3. Call the controller directly (or update an existing row):

    ```bash
    TOKEN=$(daalu service-token --purpose nautobot-provision \
              --tenant-id <daalu-tenant-uuid>)

    curl -sf -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' \
      -d '{"target_cluster_tunnel_id": "<cluster-tunnel-uuid>"}' \
      http://nautobot-controller.daalu-automation.svc.cluster.local/tenants/<daalu-tenant-uuid>
    ```

4. Watch the controller pick it up — same lifecycle as Phase 1, but
   the manifests get applied through the wg tunnel.

When the row goes `active`, the controller-returned URL points at
the customer's in-cluster Service through the tunnel
(`http://nautobot.nautobot-<slug>.svc.cluster.local`), not a public
hostname. The daalu reconciler/executor reach it via the same wg
tunnel.

---

## RBAC blast radius

The controller has *cluster-wide* permission to create namespaces +
the resource kinds inside per-tenant namespaces. This is unusual
for a per-tenant service and worth being explicit about:

- **It cannot mutate cluster RBAC.** No `clusterroles`,
  `rolebindings`, `serviceaccounts` outside its own.
- **It cannot read secrets outside namespaces it owns.** The
  ClusterRole grants `secrets`, but in practice it only touches
  `nautobot-<slug>` namespaces. Operationally this is a soft
  boundary — a buggy controller could in theory list every secret in
  the cluster. The mitigation is the reconciler's hard-coded
  namespace pattern (`namespace_for_slug`) plus tests; long-term we
  could narrow this to a `RoleBinding` template applied per-tenant
  by an admission webhook.
- **It does have permission to delete namespaces.** Combined with
  the `prune: true` flag on the Argo app, a malicious or buggy
  reconciler could cascade-delete every per-tenant Nautobot.
  Mitigation: only the controller's pod (with its specific SA) has
  these rights — no human admin runs `kubectl delete ns` as part
  of normal operations.

In **customer-cluster mode**, the kubeconfig the controller uses is
whatever scope the customer granted during cluster onboarding —
usually cluster-admin within the customer's federation namespace.
That's the customer's choice, not the operator's.

---

## Verification checklist

After deploy + the first provision attempt:

### V1. Controller pod healthy

```bash
kubectl get pods -n daalu-automation \
  -l app=nautobot-controller
# READY 1/1, status Running
```

### V2. daalu-api can reach the controller

```bash
kubectl exec -n daalu-automation deploy/daalu-api -- \
  curl -sf -o /dev/null -w "%{http_code}\n" \
  http://nautobot-controller.daalu-automation.svc.cluster.local/healthz
# expect: 200
```

### V3. A test tenant provisions end-to-end

Click **Provision now** in the wizard for an eligible tenant.
Within ~180 s:

```bash
kubectl get all -n nautobot-<slug>
# expect: nautobot-* pods all 1/1, nautobot-postgres-0 1/1, nautobot-redis-* 1/1

kubectl get certificate -n nautobot-<slug>
# expect: <slug>-sot-daalu-io-tls READY=True (after cert-manager solves HTTP-01)

curl -sf https://<slug>.sot.example.com/api/status/ | head -c 100
# expect: a JSON status blob with the per-tenant Nautobot's status

kubectl exec -n daalu-automation deploy/daalu-api -c api -- python -c "
import asyncio
from sqlalchemy import select
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import NautobotTenant
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(NautobotTenant))).scalars().all()
        for r in rows:
            print(r.tenant_id, r.state.value, r.namespace, r.hostname)
asyncio.run(main())
"
# expect: one row per provisioned tenant, state=active
```

### V4. The customer's Integration row got the URL + token

```bash
kubectl exec -n daalu-automation deploy/daalu-api -c api -- python -c "
import asyncio
from sqlalchemy import select
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Integration).where(Integration.provider=='nautobot'))).scalars().all()
        for r in rows:
            cfg = r.config or {}
            print(r.tenant_id, cfg.get('url'), bool(cfg.get('token_ciphertext')))
asyncio.run(main())
"
# expect: each provisioned tenant has url=https://<slug>.sot.example.com and token=True
```

---

## Troubleshooting

### "no nautobot tenant" 404 from the controller

Caller asked for a `nautobot_tenants` row that doesn't exist. Either
the upsert wasn't called yet (the wizard hits POST first), or it was
called for a different tenant than the one in the bearer token.

### `nautobot-<slug>` namespace stuck in pods CrashLoopBackOff

```bash
kubectl logs -n nautobot-<slug> \
  deploy/nautobot -c migrate | tail -50
```

Usually a Postgres-not-ready race on first boot. The web pod's
`migrate` init container retries; if Postgres takes >5 min to come up
something is wrong with the PVC (`kubectl describe pvc` in the same
namespace).

### Cert-manager Certificate stuck `READY=False`

The HTTP-01 challenge needs DNS to resolve `<slug>.sot.example.com` to
the traefik LB. Verify the wildcard CNAME is set in Cloudflare:

```bash
dig <slug>.sot.example.com +short
# expect: an A record (the LB IP)
```

If empty, set a wildcard CNAME `*.sot.example.com` → your apex domain (or
directly to `<ingress-ip>`) in Cloudflare.

### Customer-cluster provision fails with "no kubeconfig"

The cluster_tunnel row's sibling `Integration(provider='kubernetes')`
either doesn't exist or has an empty `kubeconfig` field. The customer
needs to finish the cluster onboarding wizard before deploying
Nautobot to their cluster.

### Reconcile loop logs "apply_one.patch_failed_immutable"

A field on an existing resource is immutable (e.g., a PVC's storage
size) and a patch can't change it. The controller logs and continues
— the running stack stays on the previous values. If you genuinely
need to resize, the destroy-and-recreate path is the only option
(and the customer's data goes with it).

### Re-provisioning rotates credentials, losing the Nautobot admin

By design: re-clicking Provision is a *re-apply*, not a credential
rotation. The bootstrap credentials are generated once at row-create
time and re-used on every reconcile. If you need to rotate the admin
token, delete the row (controller destroys the stack) and provision
fresh — the customer's Integration row will get the new URL+token
automatically.

---

## Phase 3 — what's intentionally not yet built

- **Backup / restore.** The PVCs are not snapshotted. A node failure
  loses the tenant's Postgres data. Mitigation pattern: a CronJob per
  tenant that `pg_dump`s to S3 / Rook-Ceph. Track in a follow-up.
- **Per-tenant resource caps in the wizard.** The TenantParams
  dataclass takes profile-shaped CPU/memory inputs, but the wizard
  doesn't let customers pick a profile yet. Default is the
  conservative "small" set.
- **CloudNativePG (or another Postgres operator).** Today every
  tenant runs an in-namespace Postgres StatefulSet pod. CloudNativePG
  would let us use one Postgres cluster with per-tenant DBs — lower
  pod count, higher operational complexity (a new operator to run).
  Swap-in surface is contained to ``manifests.build_postgres``.
- **DNS automation.** The wildcard CNAME is operator-set; this is
  fine for v1. A future tier would create per-tenant subdomains via
  the Cloudflare API as part of provisioning.
- **Wizard surface for customer-cluster mode.** Today the
  `target_cluster_tunnel_id` flag is operator-set via direct
  controller API. The wizard could detect "this tenant has a
  connected cluster_tunnel" and offer a "deploy in my cluster"
  toggle — straightforward UI addition once we have a customer
  asking for it.

---

## Why we kept the old shared-Nautobot path code around

`provision_tenant()` (the slug-based shared-Nautobot path) stays in
``core/sot/nautobot_provisioning.py``. The route prefers
`provision_via_controller()` when `nautobot_controller_url` is set,
and falls back to the shared path otherwise. This means:

- Deployments that haven't installed the controller yet (older
  installs, dev environments) keep working.
- Tenants already on a shared Nautobot (e.g. the operator's own
  `default` tenant) don't have to be migrated immediately.

When you migrate the last shared-Nautobot tenant, delete the legacy
function + the `managed_nautobot_*` settings. This chapter's
path is then the only one.

---

## See also

- Chapter 14 — SoT reconciler (the consumer side of these Nautobot
  instances).
- Chapter 13 — Cluster federation (the wg tunnel Option B rides on).
- ``src/daalu_automation/nautobot_controller/manifests.py`` — the
  exact K8s spec each tenant gets; this is the source-of-truth for
  what's actually deployed, not the docs.
