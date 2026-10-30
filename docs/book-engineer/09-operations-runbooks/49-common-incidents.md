# 49. Common incidents and their fixes

The runbook chapter. Organized by symptom. Each entry has a
quick diagnostic, the most-likely cause, and the fix.

If you find a new failure pattern, add it here.

---

## "daalu-api is returning 502"

### Diagnostic

```bash
kubectl -n daalu-automation get pods -l app=daalu-api
kubectl -n daalu-automation logs deploy/daalu-api -c api --tail=200
kubectl -n daalu-automation describe pod <crashing-pod>
```

### Common causes

- **All replicas crashlooping.** Look at logs.
- **Alembic init container failed.** The deployment has
  `runAlembicInitContainer: true`; if the migration errors
  the pod never reaches Ready. `kubectl logs <pod> -c
  alembic-upgrade` shows why.
- **Out of memory.** Check resource limits vs usage. Bump
  if needed.
- **Image pull failure.** Often `read:packages` scope on
  the GHCR token. See Chapter 52.

### Fix

Depends on cause. Typical:

```bash
# Roll back to the previous image
helm rollback daalu-automation
```

Then debug the offending change without time pressure.

---

## "Postgres connection pool exhausted"

### Diagnostic

```bash
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
```

### Common causes

- Long-running queries hogging connections.
- A worker stuck in a tight retry loop.
- Pool size mis-sized for replica count.

### Fix

Short-term: kill the offending session in pg_stat_activity.
Long-term: tune connection pool or kill the bad code path.

---

## "Watchdog stopped firing"

### Diagnostic

Check Alertmanager:

```bash
kubectl --kubeconfig "$KUBECONFIG_OPS" -n monitoring port-forward \
  svc/kube-prom-stack-kube-prome-alertmanager 9093:9093
# Open http://localhost:9093
```

Look at the silences page; is Watchdog silenced? Look at
the receivers; is the channel webhook still valid?

### Common causes

- Slack webhook URL revoked (rotate in Slack workspace).
- Alertmanager pod restarting and config didn't reload.
- Routing config typo.

### Fix

Fix the underlying cause; if config didn't reload, restart
Alertmanager:

```bash
kubectl --kubeconfig "$KUBECONFIG_OPS" -n monitoring rollout restart \
  sts/alertmanager-kube-prom-stack-kube-prome-alertmanager
```

---

## "TLS cert won't renew"

### Diagnostic

```bash
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation get certificate
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation describe certificate <name>
kubectl --kubeconfig "$KUBECONFIG_OPS" -n cert-manager logs deploy/cert-manager
```

### Common causes

- Cloudflare API token secret missing from challenge's
  namespace (see chapter 53).
- DNS records pointing to wrong IP.
- ACME rate limit hit (Let's Encrypt has weekly limits).

### Fix

```bash
# Re-apply the Cloudflare secret to the right namespace
kubectl -n cert-manager get secret cloudflare-api-token -o yaml \
  | sed "s/namespace: cert-manager/namespace: daalu-automation/" \
  | kubectl apply -n daalu-automation -f -

# Trigger a renewal
kubectl -n daalu-automation delete challenge --all
```

---

## "Full-API 500 storm right after an image rebuild"

### Diagnostic

```bash
kubectl -n daalu-automation logs deploy/daalu-api -c api --tail=100
# AttributeError deep inside prometheus-fastapi-instrumentator / middleware
# on nearly EVERY request, not one route
pip show fastapi   # inside the pod: which version actually shipped?
```

### Cause

The image was rebuilt for an unrelated change and a **transitive dependency
floated**: fastapi `0.136.3 → 0.137.0` wraps included sub-routers in
`_IncludedRouter` objects that lack `.path`, crashing
`prometheus-fastapi-instrumentator.get_route_name()` on nearly every
request. The deps were floor-pinned (`>=`) with **no lockfile**, so any
rebuild could ship a different dependency set than the last known-good
image. The same rebuild floated kubernetes-client to 36.0.2 and broke the
kubectl console (chapter 78).

### Fix / policy

Immediate: cap or pin the offending package and rebuild
(`fastapi>=0.111.0,<0.137` at `pyproject.toml:18`, commit 5d45420). Durable
(PR #98): the image now installs from the fully-pinned
**`requirements.lock`** (+ `--no-deps` for the package itself) — see
chapter 43 for the regeneration procedure. **Policy: never reopen the
Dockerfile to unpinned installs.** Any "works locally, 500s in prod after
rebuild" report → diff `pip freeze` in the pod against `requirements.lock`
first.

---

## "New cluster stuck on 'awaiting handshake'"

### Diagnostic

```bash
# hub side: is the peer on the live interface?
kubectl -n daalu-automation exec deploy/daalu-wireguard-hub -- wg show wg0
# vs what the Secret says:
kubectl -n daalu-automation get secret daalu-wireguard-peers \
  -o jsonpath='{.data.wg0\.conf}' | base64 -d | grep -A2 PublicKey
```

If the Secret has the peer but `wg show` doesn't, the hub missed the reload.

### Causes

1. **Pre-#106 hub image**: the entrypoint watched the `wg0.conf` *file
   path*, whose inode kubelet replaces on the first `..data` symlink swap —
   after that every peer add was silently missed. Fixed by PR #106
   (directory watch + periodic `wg syncconf` every 15 s); make sure the hub
   runs a current image (`imagePullPolicy: Always` is set for this reason).
2. **The peer never reached the Secret**: the `sync_hub_secret` reconciler
   still 401s (lowercase-`bearer` bug) — on (re-)onboarding, patch the
   `daalu-wireguard-peers` Secret by hand (render via `sync_hub_secret(db)`
   from a shell) and let the hub converge. **No rollout restart needed**
   on a current image.

Escape hatch while diagnosing: `kubectl exec` into the hub pod and run
`wg syncconf wg0 <(wg-quick strip wg0)` — idempotent, never drops live
handshakes. Full detail in chapter 24.

---

## "Machine OIDC calls 403 with a Cloudflare error page (error 1010)"

### Diagnostic

A JWKS fetch or token call to `auth.example.com` returns 403 with **Cloudflare
HTML** ("error 1010") — while the same URL works from a browser. Typical
victims: a tenant gateway's Envoy JWKS fetch, Nautobot's auth plugin, the
hub's `httpx` token-exchange. User-visible symptom: NV-CM tools silently
show their own login page (the gateway can't fetch signing keys, and the
Nautobot plugin swallows the error at `log.debug`).

### Cause

`auth.example.com` is Cloudflare-proxied, and **Browser Integrity Check**
blocks non-browser `User-Agent`s.

### Fix

Restore the **zone Configuration Rule** on `daalu.io` disabling Browser
Integrity Check for `auth.example.com/protocol/openid-connect/*`. It lives
only in Cloudflare — nothing in this repo provisions it, and a zone
security-settings sweep can silently remove it. See
[71 §71.2](../13-auth-integrations/71-auth-reachability-dns-tls-tunnel.md).

---

## "The browser says 'Failed to fetch' but the operation actually succeeded"

### Diagnostic

A long-running provision (GPU vLLM deploy, NV-CM install/upgrade) errors in
the UI at ~100 s, yet the pods appear and the deploy completes server-side.

### Cause

The edge proxy in front of the API cuts idle connections at **~100 s**. Any
route that holds the request open longer than that surfaces success as a
network error.

### Fix / policy

The long-running routes are all async now — `POST .../gpu/provision`,
`POST .../config-manager/provision`, `.../upgrade`, environment create all
return **202** and the UI polls a progress endpoint (PRs #107/#112/#114).
**Policy for new endpoints: nothing user-facing may block longer than the
~100 s ceiling — return 202 + a poll/progress route.** If you see this
symptom, some new route regressed the rule.

---

## "Cluster federation tunnel down"

### Diagnostic

`Managed Infra → Clusters → <cluster>` shows the diagnostic
blob. From the customer side:

```bash
kubectl -n daalu-edge get pods
kubectl -n daalu-edge logs deploy/daalu-edge
```

### Common causes

- Egress firewall blocking UDP/51820.
- Bootstrap token expired (>1h).
- daalu-edge pod evicted or crashing.

### Fix

Per cause. Most often: regenerate the invite, retry the
helm install.

---

## "vLLM is OOM'ing"

### Diagnostic

```bash
kubectl -n daalu logs deploy/daalu-vllm
kubectl -n daalu describe pod -l app=llama-classifier
nvidia-smi   # on the node
```

### Common causes

- Model too large for VRAM.
- Max context too high (KV cache scales with context).
- Memory leak from a vLLM version (rare but happens).

### Fix

- Pick a smaller model or quantization (INT4 vs FP16).
- Lower `maxModelLen`.
- Pin vLLM to a known-good version.

---

## "ArgoCD says Synced but the cluster is wrong"

### Diagnostic

Check the Application's `spec.source` and live state:

```bash
kubectl --kubeconfig "$KUBECONFIG_OPS" -n argocd get application daalu-automation -o yaml
```

### Common causes

- The Application is configured wrong (e.g., the
  `daalu-automation` one — see Chapter 46).
- ArgoCD's git cache is stale.

### Fix

For stale cache:

```bash
kubectl --kubeconfig "$KUBECONFIG_OPS" -n argocd patch application <name> \
  --type merge -p '{"operation":{"sync":{}}}'
```

For misconfiguration: fix the Application spec.

---

## "Image won't pull (ImagePullBackOff)"

### Diagnostic

```bash
kubectl -n <ns> describe pod <pod>
# Look at the Events section
```

### Common causes

- GHCR pull secret missing read:packages scope.
- Image tag doesn't exist on GHCR.
- Wrong namespace for the pull secret.

### Fix

See Chapter 52 in detail.

---

## "A migration is wedged"

### Diagnostic

```bash
kubectl -n daalu-automation logs -l app=daalu-api -c alembic-upgrade
```

### Common causes

- Migration has a bug (raise during upgrade).
- Migration is hung on a long-running operation (e.g.,
  CREATE INDEX on a huge table).

### Fix

If it's a bug: the init container fails, the API pod won't
start. Disable the init container temporarily, fix the
migration, redeploy:

```bash
helm upgrade daalu-automation deploy/helm/daalu-automation \
  --set api.runAlembicInitContainer=false ...
```

Then run the corrected migration manually.

If it's a slow migration: leave it running. Don't kill it
mid-way (especially DDL — partial state is worse than long
wait).

---

## "The Assistant is hallucinating heavily"

### Diagnostic

Click "Show details" on an Assistant reply. Look at:

- Which model answered.
- What tools were called.
- The tool outputs.

### Common causes

- LLM router routing to local when it should have spilled
  to cloud.
- Tool output empty/wrong; model fills in.
- New prompt regression in a recent model update.

### Fix

- Per-tenant override to force cloud for the affected SKU.
- Investigate the tool call.
- For provider regressions: pin to a previous model version.

---

## What's next

Chapter 50 — production migration practices.
