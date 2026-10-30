# Config-manager go-live operator scripts

Helper scripts for the **one-time platform setup** in engineer chapter
[`64-config-manager-go-live.md`](../../../docs/book-engineer/08-deployment-and-cicd/64-config-manager-go-live.md)
§64.2. They are non-destructive and idempotent — safe to re-run. None of
them are wired into CI or ArgoCD; an operator runs them by hand against
real infra (a live host cluster, Harbor, Cloudflare, Keycloak).

The in-repo code blockers from §64.6 are now closed (controller CLI +
image + manifests + ArgoCD app + prechecks); these scripts cover the live
external systems that the dev environment can't reach.

| Step (§64.2) | Script | What it does |
|---|---|---|
| 1. Tier-A singletons | `tier-a-check.sh` | Verifies Envoy Gateway / cert-manager / CNPG are installed (read-only). |
| 2. Mirror images | `mirror-images.sh` | Pulls each NV-CM image once into a local cache and pushes to Harbor. |
| 3. Wildcard DNS | `dns-wildcard.sh` | Creates/updates `*.cm.example.com` → gateway LB IP (Cloudflare). |
| 4. Keycloak clients | `setup-keycloak.sh` | Creates the hub service client + the NV-CM UI OIDC client. |

## Order & how they connect to the hub settings

```
tier-a-check.sh            # gate: don't proceed until this is clean
mirror-images.sh           # → then set CONFIG_MANAGER_HARBOR_REGISTRY=<HARBOR>
dns-wildcard.sh            # → certs for *.<slug>.cm.example.com can now issue
setup-keycloak.sh          # → set KEYCLOAK_ISSUER_URL / KEYCLOAK_TOKEN_AUDIENCE,
                           #   and paste keycloak_client_id/secret onto each
                           #   tenant Integration (§64.3 step 9)
```

All three hub settings live in the `daalu-automation-secrets` Secret and
are read by both `daalu-api` and the `config-manager-controller`:

- `CONFIG_MANAGER_HARBOR_REGISTRY` — must equal the `HARBOR` you mirrored
  to; `render_values()` repoints the chart's `global.images.*` here.
- `KEYCLOAK_ISSUER_URL`, `KEYCLOAK_TOKEN_AUDIENCE` — so the chart's `oidc`
  block trusts your realm and the hub's machine JWTs are accepted on the
  `svc-*` endpoints.

`CONFIG_MANAGER_CONTROLLER_URL` is already wired on the api Deployment
(in-cluster Service URL), so once the controller pod is up the wizard
route stops returning 503.

## Why the mirror matters (read before running)

The pinned NV-CM chart pulls every image as `<repository>:<tag>` and has
**no** `global.imageRegistry` knob. So the only way to run it off Harbor is
to (a) push each image to `<HARBOR>/nv-config-manager/<basename>:<tag>` and
(b) have the controller override each `global.images.<key>.repository` to
that path — which `render_values()` now does. `mirror-images.sh` and
`values.py` share the same basename/project convention, so they line up by
construction. Bumping the chart version means updating the image table in
both (each file says so at the top).

The NVIDIA service images ship as `registry.example.com/nvidia/...`
placeholders — pass `NVIDIA_SRC=<your real source>` (e.g. an NGC path or a
registry you loaded vendor tarballs into) to rewrite them at pull time.
Without it, the script mirrors only the public infra images and warns.

`mirror-images.sh` downloads each image **once**: with `skopeo` the source
lands in an OCI layout under `CACHE_DIR`; with `docker` the daemon's image
store is the cache. Re-runs skip anything already cached and already in
Harbor.
