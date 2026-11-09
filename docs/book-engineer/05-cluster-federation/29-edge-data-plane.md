# 29. The edge data plane

Customer-facing chapter: user guide §44 (the "Private (full)"
column).

This chapter is for engineers who'll change the hub-side forwarder,
the edge `--mode=edge` server, or any tenant-scoped router. The
contract you must preserve: **for Private tenants, no tenant-scoped
row touches the hub Postgres**.

---

## The thirty-second tour

Two new actors in the platform:

1. **EdgeForwardMiddleware** on the Daalu hub. Sits *after* the
   auth gate. For each request, decides whether it's a
   tenant-scoped path *for a Private tenant*; if yes, proxies the
   request to the tenant's `edge_data_url` (over the WireGuard
   tunnel) and returns the response.

2. **daalu-edge-data**, a new pod in the `daalu-edge` Helm chart.
   Runs the same Daalu backend image with `daalu server --mode=edge`.
   Auth is service-token-only via `EdgeAuthMiddleware`. DB target
   is a local Postgres on the customer's cluster.

The Daalu hub never reads the rows; it only carries the encrypted
HTTP bytes from the browser to the tunnel and back.

---

## Request flow (Private tenant, tenant-scoped path)

```
   Browser
     │ GET /api/v1/alerts
     ▼
   daalu-api (hub)
     │
     ├── AuthGateMiddleware  ─── cookie/PAT verified
     │
     ├── EdgeForwardMiddleware
     │     │
     │     ├── is_tenant_scoped(path)?  yes (/alerts is in TENANT_SCOPED_PREFIXES)
     │     │
     │     ├── tenant.is_private?       yes
     │     │
     │     ├── mint service token (purpose=edge-rpc, tid, sub, exp=60s)
     │     │
     │     └── httpx.AsyncClient → POST <edge_data_url>/api/v1/alerts
     │                                                   │
     │                                                   ▼ via WireGuard tunnel
     │                                          daalu-edge (customer cluster)
     │                                                   │ TCP forward
     │                                                   ▼
     │                                          daalu-edge-data pod
     │                                            │
     │                                            ├── EdgeAuthMiddleware
     │                                            │     │  verify JWT
     │                                            │     │  enforce tid == DAALU_EDGE_TENANT_ID
     │                                            │
     │                                            ├── alerts router runs
     │                                            │
     │                                            └── customer Postgres → result
     │
     └── stream response back to browser (no buffering, no logging)
```

The decision happens *once*, in `EdgeForwardMiddleware.dispatch`.
If the path isn't tenant-scoped OR the tenant isn't Private, the
middleware calls `await call_next(request)` and the normal hub
router chain runs against the hub DB. No code in routers /
handlers cares about which mode they're running in.

---

## Where the code lives

| File | Owns |
|---|---|
| `src/daalu_automation/core/edge_forward.py` | The forwarder middleware + tenant-scoped path matcher. |
| `src/daalu_automation/api/edge_auth.py` | `EdgeAuthMiddleware` for `--mode=edge`. Enforces tenant boundary. |
| `src/daalu_automation/api/main.py` | `create_app(mode=...)` factory that swaps middlewares. |
| `src/daalu_automation/cli.py` | `daalu server --mode=edge` flag (sets `DAALU_MODE` env). |
| `src/daalu_automation/models/tenant.py` | `edge_data_url`, `edge_data_token_hash` columns. |
| `migrations/versions/0010_edge_data_plane.py` | DB migration for those columns. |
| `deploy/helm/daalu-edge/templates/data-plane.yaml` | Edge Deployment + Service + Secret + migration Job + NetworkPolicies. |
| `deploy/helm/daalu-edge/templates/postgres.yaml` | Bundled single-instance Postgres (opt-in). |

Tests: `tests/test_edge_forward.py` (41 cases) + the existing
`tests/test_llm_router_tiers.py`.

---

## Adding a new tenant-scoped router

The forwarder's allow-list is `TENANT_SCOPED_PREFIXES` in
`core/edge_forward.py`. **If you add a router that touches
tenant-scoped tables, add its prefix to that tuple.** A new router
left off the list silently writes to the hub DB even for Private
tenants. The unit tests cover the prefix list but not new routers
— sometimes the test you really want is a CI grep that asserts
every router prefix appears in either `TENANT_SCOPED_PREFIXES` or
`PLATFORM_SCOPED_PREFIXES`. Open issue.

The complementary `PLATFORM_SCOPED_PREFIXES` tuple is there as
documentation, not enforcement — the forwarder reads only the
tenant list. We list platform prefixes explicitly so the next
person who adds a router doesn't get "everything I add silently
forwards" by surprise.

---

## Tenant boundary on the edge

The edge has a *single* tenant identity, set at install time:

```
DAALU_EDGE_TENANT_ID = "00000000-0000-0000-0000-000000000010"
```

`EdgeAuthMiddleware.dispatch` enforces this on every request:

```python
if claims.tenant_id != expected_tenant_id():
    return 403
```

This means a hub-side bug that forwards tenant A's request to
tenant B's edge fails closed — the edge refuses because the JWT
names a tenant it isn't provisioned for. The only way to get an
edge to serve a different tenant is to redeploy it with a
different env. That's the contract.

---

## Why we pass through the hub at all (vs. browser → edge direct)

Option B from the design doc § 3.2 ("direct redirect") would have
the browser talk directly to the edge URL on its own DNS name.
That removes even the brief pass-through transit. We didn't ship
it Phase 1 because:

* Customers don't always own a public-DNS name pointing at their
  cluster.
* TLS termination on the edge needs a working cert path
  (LetsEncrypt over the tunnel? a customer-supplied cert?) — not
  hard, but onboarding friction.
* The pass-through is byte-streaming — no buffering, no logging.
  In practice the hub is a load balancer for these requests.

Direct redirect is on the Phase 2 roadmap for buyers who insist
on zero hub transit.

---

## Bundled Postgres vs bring-your-own

The chart includes a single-instance Postgres StatefulSet (off by
default; enable with `postgres.enabled=true`). It's there for the
"60-second onboarding" experience and for proof-of-concept
deployments. We deliberately do *not* recommend it for production:

* No backups configured.
* No replicas.
* No automated failover.
* No connection pooling beyond what the edge-data pod does
  on its own.

Production users set `postgres.enabled=false` and point at a
managed Postgres via `tenantData.dbUrl`. Reference deployments
(CNPG, RDS Terraform) ship as Phase 2 follow-ups.

---

## Failure modes the operator will hit

* **Edge pod CrashLoopBackOff after first install** — almost
  always because `tenantData.serviceToken` or `tenantData.tenantId`
  is empty. The chart fails-loud with a `required` error from
  Helm, but a `helm upgrade --reuse-values` after a partial set
  can lose those values.
* **502 from the hub** on every Private-tenant API call → edge
  unreachable. Check the tunnel (`wg show` on both ends), then
  check the edge-data pod's logs (`daalu server --mode=edge`).
* **401 from the edge** → service-token mismatch. The token's
  signing key must match the hub's `SERVICE_TOKEN_SECRET_KEY`.
* **Migration Job stuck `Pending`** → the customer's cluster
  doesn't have a default StorageClass and the Postgres PVC can't
  bind. Set `postgres.storageClassName` explicitly.

The `daalu-edge-data` pod logs at INFO carry the JWT claims for
every request (tenant + user + path); no bodies. Useful debugging
without spilling user data.

---

## Known gaps for production-grade

* [ ] CI check: every router prefix is in exactly one of the
  forwarder's `_PREFIXES` tuples.
* [ ] Streaming endpoints (briefing progress) — Phase 1 buffers
  the full response; needs Transfer-Encoding chunked passthrough.
* [ ] WebSocket forwarding (today only the IDE proxy uses WS;
  that path already short-circuits through a dedicated handler).
* [ ] Hub-side data-out CLI (`daalu migrate-tenant-out`) for
  Standard → Private upgrades carrying existing data.
* [ ] Schema split (hub-only vs tenant-only migrations) — today
  the edge runs all migrations against its local Postgres; the
  hub-only tables sit empty there. Harmless but untidy.
* [ ] Pen-test the tenant boundary on the forward path.
* [ ] SOC 2 control mapping for the new data flow.
* [ ] Backup story for the bundled Postgres (Velero docs).
* [ ] Distributed-trace correlation IDs propagated through the
  forward.

---

## See also

* Chapter 39 (user guide) — buyer-facing tier story
* Chapter 28 — Daalu Private (edge-agents tier; this chapter is
  the next step beyond it)
* Chapter 57 (engineer book) — tenant isolation
* Chapter 34 (user guide) — installing daalu-edge
