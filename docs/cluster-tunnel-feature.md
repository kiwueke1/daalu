# Cluster tunnel (WireGuard VPN) — feature reference

This document covers, end to end, what happens when an operator
onboards a managed cluster through the **Clusters** page in the
Daalu Automation UI. The feature gives every onboarded customer
cluster a routable WireGuard address on a private `10.200.0.0/16`
mesh, so the operator's `daalu-api` pod can reach the customer's
Kubernetes API server (and any other in-cluster service) without the
customer exposing anything publicly.

The first half describes the **user-facing workflow** — what the
operator sees, clicks, and copies. The second half traces every
backend step that gets triggered, in order, with file/line pointers.

---

## 0 · Setup — one-time, by the operator

**Scope.** Everything in this section runs **once per operator
cluster**, not per tenant. After §0 is green, every new tenant
onboards via the wizard in §1 and the platform allocates per-tenant
tunnel IPs, keypairs, invite tokens, and peer entries automatically.
You only revisit §0 if you rebuild the operator cluster, rotate the
hub keypair (§0.3 *Rotation note*), or move the hub to a different
node.

Without these prerequisites the Clusters page returns a 503 with
``wireguard_public_endpoint is not configured`` the moment the operator
clicks **Create**, and even with the endpoint set the customer-side
``helm install`` will hang on its bootstrap callback. Walk through
this section once, end to end, before pointing any user at the
``/clusters`` or ``/onboarding/cluster`` workflows.

The setup is done **on the operator cluster** (point ``$KUBECONFIG`` at it).
There is nothing the customer has to do until §1.3.

### 0.1 Public DNS + UDP/51820 inbound

The customer-side ``daalu-edge`` container connects out to a stable
hostname over UDP. You have to pick that hostname and route it to a
node in the operator cluster.

1. **Pick a hostname.** Convention: ``wg.<your-domain>``. The same
   value goes into ``WIREGUARD_PUBLIC_ENDPOINT`` below and into the
   install snippet the wizard generates for the customer.

2. **Pick a node.** WireGuard runs as ``hostNetwork: true`` with
   ``hostPort: 51820/UDP``. You need a node that:
   - has a stable public IP (no rolling IP from a cloud autoscaler),
   - is allowed inbound UDP/51820 in any firewall/security-group in
     front of it.

   Confirm with ``kubectl get nodes -o wide``.

3. **DNS A-record.** Create an A-record ``wg.example.com →
   <that-node's-public-IP>`` in your DNS provider (e.g. Cloudflare zone
   ``<zone-id>``) — it is not pre-created.

   > **Must be DNS-only (gray cloud), not Proxied (orange cloud).**
   > Cloudflare's HTTP proxy does not forward UDP/WireGuard — if the
   > record is proxied, customer edges resolve ``wg.example.com`` to a
   > Cloudflare anycast IP (``104.21.x.x`` / ``172.67.x.x``), their
   > keepalive packets land on Cloudflare's edge and are silently
   > dropped. Symptom on the customer side: ``wg show wg0`` shows
   > ``transfer: X B sent, 0 B received``. Verify with
   > ``dig +short wg.example.com`` — it should return the hub node's
   > real public IP, not a ``104.21.x.x`` / ``172.67.x.x`` address.

4. **Firewall.** Open UDP/51820 inbound to that node. On a cloud provider that's
   a firewall / security-group rule attached to the node (allow
   ``UDP 51820`` inbound from any IPv4 / IPv6). On bare metal it's an
   iptables ``-A INPUT -p udp --dport 51820 -j ACCEPT`` on the host.
   Verify from any external host:

   ```bash
   nc -u -v -w3 wg.example.com 51820 </dev/null
   # "Connection to wg.example.com 51820 port [udp] succeeded!"
   ```

### 0.2 Pin the hub Deployment to that node

The chart accepts a ``wireguard.nodeSelector`` so the hub lands on the
node that owns the DNS pointer. If you skip this and the cluster has
more than one schedulable node, Kubernetes may put the hub on a node
where ``hostPort: 51820`` collides with something else or where the
DNS A-record doesn't point.

Label the node and set the selector:

```bash
# Choose a label the node either already has or you don't mind owning.
kubectl label node <node> daalu.io/wireguard-hub=true
```

Then in your Helm values override (see §0.4):

```yaml
wireguard:
  nodeSelector:
    daalu.io/wireguard-hub: "true"
```

### 0.3 Generate and seed the hub keypair

The hub Secret is annotated ``helm.sh/resource-policy: keep`` so Helm
never rewrites it after install. Seed it **once** at first enable;
afterward, the daalu-api coordinator owns its contents.

Two equivalent paths:

**Option A — seed at install time via Helm values** (recommended on first
install):

```bash
# Generate a keypair locally. `wg` is in wireguard-tools.
HUB_PRIV=$(wg genkey)
HUB_PUB=$(echo "$HUB_PRIV" | wg pubkey)
echo "hub pubkey (for the install snippet later if you ever inspect it): $HUB_PUB"
echo "hub privkey (paste into wireguard.hubPrivateKey): $HUB_PRIV"
```

Store both somewhere safe (1Password / Bitwarden — *not* the repo).
The privkey goes into the Helm values; the pubkey is what the API
re-derives every time it renders an install snippet, so you don't
have to track it separately.

**Option B — drop the Secret in by hand** (if the hub is already up but
empty, or to rotate later):

```bash
# wireguard.hubAddress defaults to 10.200.0.1, listenPort 51820.
HUB_PRIV=$(wg genkey)

kubectl -n daalu-automation create secret generic daalu-wireguard-peers \
  --from-literal=wg0.conf="$(cat <<EOF
[Interface]
PrivateKey = $HUB_PRIV
Address = 10.200.0.1/16
ListenPort = 51820
# Peers appended by the daalu-api coordinator at runtime.
EOF
)" --dry-run=client -o yaml | kubectl apply -f -
# After this, restart the hub pod so wg-quick picks the new key up.
kubectl -n daalu-automation rollout restart deploy/daalu-wireguard-hub
```

**Rotation note.** Replacing the hub privkey invalidates every customer's
hub-pubkey baked into their install snippet, which means every customer
edge has to be re-onboarded (delete the row in ``/clusters`` and
hand them a new snippet). Only rotate if compromised.

### 0.4 Enable the chart's WireGuard pieces

Edit ``deploy/helm/daalu-automation/values.yaml`` (or use a release-time
override) to switch the hub on:

```yaml
wireguard:
  enabled: true
  hubPrivateKey: "<paste the wg genkey output here, or omit if you used Option B above>"
  nodeSelector:
    daalu.io/wireguard-hub: "true"
  # listenPort / hubAddress / image only need to change from defaults
  # in unusual deployments — leave them alone otherwise.
```

What this flips on, all gated by ``wireguard.enabled``:

- the ``daalu-wireguard-hub`` Deployment (``deploy/helm/daalu-automation/templates/wireguard-hub.yaml``),
- the ``daalu-wireguard-peers`` Secret,
- the Role + RoleBinding granting the daalu-api ServiceAccount
  ``get/patch/update`` on that Secret.

If you also set ``wireguard.hubPrivateKey``, the chart's first apply
seeds ``wg0.conf`` into the Secret. Subsequent applies keep their
hands off (``helm.sh/resource-policy: keep``).

### 0.5 Set the daalu-api side: ``WIREGUARD_PUBLIC_ENDPOINT``

This is the env var the API reads to know what hostname to bake into
install snippets. **This is the variable the 503 error was pointing
at.** It lives in the operator's ``.env`` and gets bundled into the
``daalu-automation-secrets`` Secret by ``deploy/scripts/install.sh``.

```bash
# In .env (at the repo root):
WIREGUARD_PUBLIC_ENDPOINT=wg.example.com
# Public URL the customer-side edge POSTs its bootstrap callback to.
# Distinct from WIREGUARD_PUBLIC_ENDPOINT (which is UDP for WG traffic);
# this is the HTTPS ingress for daalu-api. Include scheme, no trailing
# slash. Onboarding returns 503 with a clear hint if unset.
OPERATOR_API_PUBLIC_URL=https://ops.example.com
# Optional overrides — defaults are fine for most installs:
# WIREGUARD_LISTEN_PORT=51820
# WIREGUARD_HUB_ADDRESS=10.200.0.1
# WIREGUARD_CONNECTED_WINDOW_S=180
# WIREGUARD_DEGRADED_WINDOW_S=600
```

Apply with ``./deploy/scripts/install.sh``. The script re-creates the
``daalu-automation-secrets`` Secret from ``.env``, but **does not**
restart the consumer pods — Helm only rolls pods when its rendered
manifests change, and an out-of-band Secret rewrite doesn't qualify.
After install.sh finishes, kick the four consumers manually so they
pick up the new env:

```bash
kubectl -n daalu-automation rollout restart \
  deploy/daalu-api deploy/daalu-agents deploy/daalu-beat deploy/daalu-worker
```

If you skip this, §0.6 step (1) will report ``wireguard_public_endpoint``
as an empty string even though the Secret on disk has the right value.

> The matching Pydantic setting is ``settings.wireguard_public_endpoint``
> in ``config.py`` — Pydantic settings lowercases the env-var name, so
> ``WIREGUARD_PUBLIC_ENDPOINT`` in ``.env`` materialises as
> ``settings.wireguard_public_endpoint`` at runtime.

### 0.6 Verify the operator side is fully ready

Run all five of these and confirm before pointing a user at the wizard:

```bash
export KUBECONFIG=<operator-cluster-kubeconfig>

# (1) The setting is actually in the pod's environment.
kubectl -n daalu-automation exec deploy/daalu-api -- \
  python -c "from daalu_automation.config import get_settings; print(get_settings().wireguard_public_endpoint)"
# expect: wg.example.com  (or whatever you set)

# (2) The hub pod is running and on the right node.
kubectl -n daalu-automation get pod -l app=daalu-wireguard-hub -o wide
# expect: STATUS=Running, NODE=<the one with the public IP>

# (3) UDP/51820 actually reaches the hub from outside.
nc -u -v -w3 wg.example.com 51820 </dev/null

# (4) Operator API confirms onboarding works (no real cluster, just
#     the precondition). This should succeed (201) — delete the row
#     after.
#
# $TOK is a tenant-admin JWT (the route uses current_admin, not
# current_superuser — any user with is_admin=True works). Mint it by
# logging in as your tenant-admin user:
ADMIN_EMAIL=admin@example.com   # whoever you created with `daalu create-admin`
read -rsp "Password for $ADMIN_EMAIL: " ADMIN_PASSWORD; echo
TOK=$(jq -n --arg e "$ADMIN_EMAIL" --arg p "$ADMIN_PASSWORD" \
        '{email:$e, password:$p}' \
      | curl -fsS -X POST https://ops.example.com/api/v1/auth/login \
          -H 'Content-Type: application/json' --data @- \
      | jq -r .token)
unset ADMIN_PASSWORD
echo "TOK length: ${#TOK}"   # expect ~200+ for a real JWT

curl -fsS -X POST -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"slug":"precheck","name":"precheck"}' \
  https://ops.example.com/api/v1/clusters | jq -r '.tunnel_ip,.install_snippet'

curl -fsS -X DELETE -H "Authorization: Bearer $TOK" \
  https://ops.example.com/api/v1/clusters/precheck

# (5) Beat task is scheduled (purely cosmetic check — confirms the
#     30s tunnel-health task is registered).
kubectl -n daalu-automation logs deploy/daalu-beat | grep -i wireguard | tail -3
```

If all five pass, the operator side is done. Users can now hit
``/clusters`` or ``/onboarding/cluster`` and the workflow will run.

### 0.7 What the wizard asks the user for

The wizard's UI is intentionally minimal — every other field is
generated server-side or derived from settings. The user enters:

| Field | Required? | What it is | Example |
|---|---|---|---|
| **Slug** | yes | DNS-style identifier (``[a-z0-9]([a-z0-9-]*[a-z0-9])?``, ≤64 chars). Used in the API path (``/clusters/<slug>``), the install snippet, and the hub Peer block's comment. Must be unique per tenant. | ``acme-prod`` |
| **Display name** | yes | Free-form human-readable name shown in the Clusters table. ≤255 chars. | ``ACME production cluster`` |

Everything else is automatic:

- **Tunnel IP** — allocated by the coordinator from ``10.200.0.0/16``
  (lowest free host, skipping ``.0.1`` which is the hub).
- **Operator-side keypair** — generated by the API; the public key
  goes into the row, the private key is Fernet-encrypted at rest.
- **Invite token** — 32-byte url-safe-base64, sha256-hashed at rest,
  shown once in the response.
- **Hub public key + endpoint** — pulled from the chart's settings;
  baked into the install snippet so the customer doesn't have to
  know it.

After clicking **Create** the user sees a single-screen result panel
with the install snippet, a copy-to-clipboard button, and a "the
invite token is one-shot" warning. They paste that snippet to the
customer over a secure channel, and the wizard polls the row every
5 seconds until the status flips to ``connected``.

### 0.8 Pre-flight checklist for the customer side

This is what the customer needs on their cluster before they can run
the snippet you give them. Forward this verbatim:

1. **kubectl access** to the cluster they want managed, with
   permission to create a namespace + Deployment + Secret + PVC.
2. **Helm 3.x** installed locally.
3. **Outbound UDP/51820** from any pod in that cluster to your hub
   hostname. Most clouds allow this by default; on locked-down
   networks it has to be whitelisted in the egress firewall.
4. A **PVC-capable StorageClass**. The default is fine on every cloud
   we've tested; if the customer cluster has no default
   StorageClass, pass ``--set persistence.storageClassName=<theirs>``
   when running the snippet, or ``--set persistence.enabled=false``
   to use an emptyDir (the WG keypair will then be regenerated on
   every pod restart, requiring a re-onboard — only use this for
   ephemeral testing).

### 0.9 Common setup failures and what they look like

| Symptom | Where it shows up | Cause | Fix |
|---|---|---|---|
| ``503: wireguard_public_endpoint is not configured`` on **Create** | The wizard, immediately | ``WIREGUARD_PUBLIC_ENDPOINT`` is empty in ``.env`` (or ``.env`` wasn't re-applied after editing) | §0.5 — add to ``.env``, re-run ``install.sh`` |
| Cluster row stuck in ``pending`` forever | Wizard's wait screen | Customer hasn't run the snippet yet | Confirm with the customer; resend the snippet if lost |
| Cluster row stuck in ``awaiting_handshake`` | Wizard's wait screen | UDP/51820 blocked, OR hub pubkey in snippet is stale (e.g. hub privkey was rotated between snippet generation and customer install) | ``kubectl -n <ns> logs deploy/daalu-edge`` on customer side. Most often: re-onboard so the snippet picks up the current hub pubkey |
| Cluster row stuck in ``awaiting_handshake``; on the customer side ``kubectl exec deploy/daalu-edge -c edge -- wg show wg0`` shows ``transfer: X B sent, 0 B received``; on the operator side ``kubectl -n daalu-automation exec deploy/daalu-wireguard-hub -- wg show wg0 dump`` lists the peer but column 5 (latest-handshake) is ``0`` | The WG hostname's Cloudflare record is **Proxied** (orange cloud). Packets go to a Cloudflare anycast IP (``104.21.x.x`` / ``172.67.x.x``) instead of the hub node, and Cloudflare drops them — it does not forward UDP. | Flip the record to **DNS-only** (gray cloud) in the Cloudflare dashboard. Verify with ``dig +short wg.example.com`` — should return the hub node's real public IP. **Then `kubectl rollout restart deploy/daalu-edge` on the customer cluster** — wg-quick resolves the endpoint hostname once at pod start, so a still-running pod will keep sending to the cached Cloudflare IP after the DNS flip. Confirm with ``wg show wg0`` that the peer's ``endpoint:`` line now shows the hub node's IP, not ``104.21.x.x``. Handshake usually lands within seconds and the next beat tick flips the row to ``connected``. See §0.1 step 3. |
| Hub pod stuck in ``CrashLoopBackOff`` | ``kubectl get pods`` | ``wireguard.hubPrivateKey`` was never seeded AND no manual Secret seed was done | §0.3 Option B — drop the Secret in by hand and restart |
| Snippet contains ``<hub-pubkey-unavailable>`` placeholder | Customer reports invalid snippet | daalu-api couldn't read the ``daalu-wireguard-peers`` Secret at render time — usually a missing RoleBinding (chart not at the WG-enabled version, or ``wireguard.enabled=false``) | Re-run ``helm upgrade`` with ``wireguard.enabled=true`` |
| Onboard succeeds but ``/clusters`` shows the row in ``error`` after ~10min | UI badge | Beat task observes no handshake — customer-side container can't reach the hub, OR is sending a different pubkey than the one on the row | Same as ``awaiting_handshake`` row — start with edge-pod logs |
| Hub pod (or any newly-published image) stuck in ``ImagePullBackOff`` with ``403 Forbidden`` from ``ghcr.io``, while older pods stay Running | ``kubectl describe pod`` events | The in-cluster ``ghcr`` pull secret was created from a GitHub token that lacks ``read:packages``/``write:packages``. Cached images keep working; fresh ones 403. Most often the operator's shell exports ``GITHUB_TOKEN=ghp_…`` (scope: ``repo`` only) which overrides ``gh``'s OAuth login | ``install.sh`` since 2026-05-17 strips ``GITHUB_TOKEN`` and pre-flights the scope before creating the secret. To unblock in-place without re-running: ``kubectl -n daalu-automation create secret docker-registry ghcr --docker-server=ghcr.io --docker-username=<gh-user> --docker-password="$(GITHUB_TOKEN= gh auth token)" --docker-email=<gh-user>@users.noreply.github.com --dry-run=client -o yaml \| kubectl apply -f -`` then ``kubectl -n daalu-automation delete pod <stuck-pod>`` |
| §0.6 step (1) prints ``wireguard_public_endpoint`` as empty string even after editing ``.env`` and re-running ``install.sh`` | The verify command in §0.6 | ``install.sh`` rewrites the Secret but does not restart consumer pods; the old pod is still reading the previous env from its in-memory snapshot | Run the ``kubectl rollout restart`` block at the bottom of §0.5 and re-verify |

---

## 1 · The end-user workflow

### 1.1 Where the page lives

`Clusters` is a top-level entry in the left sidebar (sidebar.tsx:25
adds the `<Boxes>` nav item). The page itself is
`frontend/app/clusters/page.tsx`. Visiting it shows a table of every
managed cluster the caller's tenant owns — slug, status badge,
tunnel IP, and time-since-last-handshake.

### 1.2 Onboarding a new cluster — the click path

1. The operator clicks **Onboard cluster** (top-right of the
   `/clusters` page).
2. A small inline form appears with two fields:
   - **Slug** — DNS-style identifier (lowercase letters, digits,
     hyphens; pattern `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`). This
     becomes the cluster's stable name across the API, the install
     snippet, and the customer-side Helm release. Example: `acme-prod`.
   - **Display name** — free-form, e.g. `ACME production cluster`.
3. Operator clicks **Create**. The form disables itself, the
   `POST /api/v1/clusters` request fires, and on success the form
   collapses and is replaced by an amber-bordered "Cluster created"
   panel containing:
   - The cluster's **tunnel IP** (e.g. `10.200.0.2`) and the
     operator's WireGuard public key.
   - The **install snippet** — a multi-line `helm install daalu-edge`
     command pre-baked with every value the customer needs.
   - A prominent **Copy** button.
   - A bold "the invite token is one-shot and is not shown again"
     warning.

The install snippet looks like this (the actual token / IP / pubkey
are real on a live row):

```bash
helm install daalu-edge oci://ghcr.io/kiwueke1/charts/daalu-edge \
  --set cluster.slug=acme-prod \
  --set cluster.tunnelIp=10.200.0.2 \
  --set hub.endpoint=wg.example.com:51820 \
  --set hub.publicKey=<hub-wireguard-public-key> \
  --set operator.apiUrl=https://ops.example.com \
  --set bootstrap.token=<one-shot-bootstrap-token>
```

The operator copies that and gives it to the customer (over an
existing secure channel — Slack DM, password manager, etc.). It is
never written to the DB in plaintext; only its sha256 is stored, on
the `cluster_tunnels.invite_token_hash` column.

### 1.3 What the customer does

The customer pastes the snippet into a shell with their
`KUBECONFIG` pointing at the cluster they want managed and runs it.
Helm installs the `daalu-edge` chart
(`deploy/helm/daalu-edge/`), which lays down:

- a `daalu-edge` Deployment (one pod with two containers — `edge`
  running kernel WireGuard inside the pod's netns with `NET_ADMIN` +
  `NET_RAW`, and `apiserver-proxy` running `socat` to expose the
  customer's kube-apiserver on the tunnel — see §2.10),
- a `daalu-edge-bootstrap` Secret (the invite token + the hub's
  WireGuard pubkey),
- a `daalu-edge-state` PVC (so the pod's WG keypair survives
  restarts).

The pod's entrypoint (`deploy/edge/entrypoint.sh`):

1. Generates a fresh X25519 keypair on first boot, persists it
   under `/var/lib/daalu-edge/`.
2. POSTs the bootstrap callback to the operator's API:
   `POST /api/v1/clusters/{slug}/callback` with the new pubkey and
   the invite token.
3. Writes `/etc/wireguard/wg0.conf` and brings `wg0` up.
4. Stays alive so the kernel keeps the tunnel open.

Alongside it, the `apiserver-proxy` sidecar listens on the pod's
wg0 address (`<tunnel_ip>:6443`) and forwards raw TCP to
`kubernetes.default.svc.cluster.local:443` — that's the path the
operator's agent uses to `kubectl` into the customer cluster via a
kubeconfig with `server: https://<tunnel_ip>:6443`. TLS terminates
at the apiserver, not at the proxy (see §2.10).

### 1.4 What the operator sees afterward

Back in the `/clusters` page, the table refetches every 30 seconds
(matching the beat-task cadence). Within a minute of the customer
running the snippet:

- The row's status badge flips: `pending` → `awaiting_handshake` →
  `connected`. Colour mapping is in
  `frontend/app/clusters/page.tsx:STATUS_BADGE`:
  - `connected` — emerald dot, fresh handshake within the last 180s
  - `degraded` — amber dot, handshake within 180–600s
  - `error` — red dot, no handshake for >600s
  - `awaiting_handshake` — amber dot, peer is on the hub but no
    handshake yet
  - `pending` — muted dot, no peer yet (customer hasn't installed)
- The **Last handshake** column shows a relative time (`30s ago`,
  `2m ago`).

To tear a cluster down, the operator clicks the trash icon and
confirms. That immediately removes the row and re-syncs the hub
Secret (the peer block disappears). Re-onboarding the same cluster
means a fresh `POST /clusters` and a fresh install snippet.

### 1.5 What is **not** in the UI (intentional)

- **Invite token re-display.** Once the create-response panel is
  dismissed, the token is gone — the DB only has its hash. If the
  customer loses the snippet, the operator deletes the row and
  re-onboards.
- **Per-cluster permissions / kubeconfig upload.** The tunnel just
  carries traffic. The kubeconfig that points at the now-reachable
  customer API server is a separate Integration row
  (`provider="kubernetes"`).
- **Manual peer config editing.** The hub Secret is owned by the
  coordinator. Editing it by hand will be overwritten on the next
  beat tick (see §2.5).

---

## 2 · The backend lifecycle

This section traces what happens when the operator clicks **Create**
and continues through the beat-task convergence loop.

### 2.1 Component map

| Concern | Location |
|---|---|
| HTTP routes | `src/daalu_automation/api/routers/clusters.py` |
| Auth-gate exception for the callback | `src/daalu_automation/api/main.py:AuthGateMiddleware` |
| WireGuard primitives (keygen, IP alloc, render) | `src/daalu_automation/core/wireguard.py` |
| At-rest encryption (Fernet) | `src/daalu_automation/core/crypto.py` |
| DB schema | `src/daalu_automation/models/cluster_tunnel.py`, `migrations/versions/0003_cluster_tunnels.py` |
| Tunnel-health beat task | `src/daalu_automation/workers/wireguard_health.py` |
| Hub Deployment + Secret + RBAC | `deploy/helm/daalu-automation/templates/wireguard-hub.yaml` |
| Hub container | `deploy/wg-hub/` |
| Edge container | `deploy/edge/` |
| Edge Helm chart | `deploy/helm/daalu-edge/` |

### 2.2 The `cluster_tunnels` row

One row per `(tenant, cluster)` pair. The model
(`models/cluster_tunnel.py`) holds:

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `tenant_id` | FK to `tenants.id`, cascade delete |
| `slug` | URL/identifier, unique per tenant |
| `name` | Display name |
| `status` | enum: `pending` / `awaiting_handshake` / `connected` / `degraded` / `error` |
| `operator_pubkey` | Base64 X25519 — the hub's view of "us-for-this-cluster" |
| `operator_privkey_encrypted` | Fernet ciphertext of the operator-side priv key |
| `customer_pubkey` | NULL until the bootstrap callback succeeds |
| `customer_endpoint` | Optional `host:port` if the customer has a stable public address |
| `tunnel_ip` | Postgres INET, unique. Allocated from `10.200.0.0/16` (skipping `.0.1` which is the hub) |
| `invite_token_hash` | sha256 of the one-shot token. Cleared on first successful callback |
| `last_handshake_at` | timestamptz, refreshed by the beat task |
| `last_error` | nullable free-text |
| `created_at` / `updated_at` | Standard timestamp mixin |

Two unique constraints back the operational invariants:
`uq_cluster_tunnels_tunnel_ip` (no two rows share an IP) and
`uq_cluster_tunnels_tenant_slug` (no two clusters share a slug per
tenant).

### 2.3 `POST /api/v1/clusters` — onboarding

Handler: `clusters.onboard_cluster` (api/routers/clusters.py). Requires
`current_admin` — a tenant-admin user. Step by step:

1. **Validate the slug.** Regex `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`,
   else 422.
2. **Verify `wireguard_public_endpoint` is set.** It's the hostname
   the customer's edge will dial; if it's empty in the operator's
   settings, the route returns 503 with an actionable message rather
   than silently producing an install snippet that points nowhere
   (see `clusters.py:_require_endpoint`).
3. **Generate the operator-side keypair.**
   `core.wireguard.generate_keypair()` produces a fresh X25519
   scalar via `cryptography.hazmat.primitives.asymmetric.x25519`,
   serialises both halves as raw bytes, and base64-encodes them
   to wg format. The keypair is unique per cluster row so revoking
   one customer doesn't affect the others.
4. **Encrypt the private key.** `encrypt_keypair()` calls
   `crypto.encrypt_secret` (Fernet, key derived from
   `settings.secret_key` via sha256). The ciphertext goes into
   `operator_privkey_encrypted`; the plaintext is discarded as soon
   as the row is built.
5. **Mint the invite token.** `generate_invite_token()` returns a
   `(plaintext, sha256)` pair. `secrets.token_urlsafe(32)` provides
   the entropy; only the hash is stored in
   `invite_token_hash`. The plaintext is included exactly once in
   the HTTP response and never persisted.
6. **Allocate a tunnel IP.** `allocate_tunnel_ip(db)` SELECTs every
   existing `tunnel_ip`, adds the reserved hub address
   (`10.200.0.1`) to the used-set, and returns the lowest free
   host in `10.200.0.0/16`. A `/16` gives ~65k peers — practically
   unbounded.
7. **Insert the row.** The first commit lands the row in `pending`.
   Two race-safe failure modes are translated to HTTP 4xx:
   - `uq_cluster_tunnels_tenant_slug` → 409 ("slug already exists")
   - `uq_cluster_tunnels_tunnel_ip` → 503 ("retry") — only
     reachable under concurrent onboards racing for the same IP
8. **Render the install snippet.** `render_edge_install_snippet`
   stitches together the public endpoint, hub pubkey, slug, tunnel
   IP, and plaintext token. The hub pubkey is read with
   `_load_hub_pubkey` — a best-effort, in-process read of the
   `daalu-wireguard-peers` Secret followed by an X25519
   private-to-public derivation. If the read fails (local dev, RBAC
   missing) the snippet renders with a `<hub-pubkey-unavailable>`
   placeholder rather than 500'ing.
9. **Return the augmented `ClusterCreateOut`** including
   `invite_token` (plaintext, one-shot) and `install_snippet`. The
   plaintext token never persists on the server beyond this
   response.

The row is **not** in the hub Secret yet — the customer hasn't
checked in. The hub-side Peer block only appears after the
callback (step 2.4) because rendering a Peer entry without a
`PublicKey` would crash wg-quick on the next reload.

### 2.4 `POST /api/v1/clusters/{slug}/callback` — bootstrap

Handler: `clusters.cluster_callback`. **No** `current_user`
dependency. The route is reached by the customer-side
`daalu-edge` pod, which has no user session and authenticates with
the invite token instead.

Two pieces of plumbing make this work safely:

- **AuthGateMiddleware bypass.** `api/main.py` builds a regex
  `^/api/v1/clusters/[^/]+/callback$` and lets POSTs that match
  through the cookie/Bearer gate. The route itself is the only
  authentication boundary.
- **Constant-time token compare.** The presented invite token is
  sha256'd and compared to `invite_token_hash` via
  `hmac.compare_digest`. A wrong token returns 401; a row whose
  hash is already `NULL` (already redeemed) returns 409 — by
  shape, not by content, so a leaked token can't be reused even
  if an attacker also has the plaintext.

Flow:

1. Look up the row by slug. 404 if absent.
2. If `invite_token_hash` is `NULL`, 409 ("already redeemed").
3. Constant-time hash check. 401 on mismatch.
4. Write `customer_pubkey`, optional `customer_endpoint`. Clear
   `invite_token_hash` (one-shot). Move status to
   `awaiting_handshake`. Commit.
5. Call `sync_hub_secret(db)` to push the updated peer set into
   the hub Secret. Any `HubSecretUnavailable` here is logged but
   **not** returned to the caller — the token has already been
   consumed, and the beat task will converge the Secret the next
   time it runs (§2.6).

### 2.5 Hub Secret patching — `sync_hub_secret`

The operator-side WireGuard hub is a single Deployment defined in
`templates/wireguard-hub.yaml`. It uses `hostNetwork: true` and
pins to a node so `hostPort 51820` lands on a stable public IP
(matches `settings.wireguard_public_endpoint`). The pod reads its
config from the `daalu-wireguard-peers` Secret, key `wg0.conf`.
That Secret carries `helm.sh/resource-policy: keep` so Helm
upgrades don't clobber what the coordinator wrote.

`core/wireguard.sync_hub_secret`:

1. **Selects the live peers** via `collect_live_peers(db)` — rows
   in `awaiting_handshake`, `connected`, or `degraded`. `pending`
   rows are intentionally skipped (no `customer_pubkey` yet).
2. **Reads the existing Secret** to recover the hub's existing
   `PrivateKey`. The hub keypair is **never** rotated by this
   call — only Peer blocks change. The hub priv key is seeded once
   at install (via `wireguard.hubPrivateKey` in values.yaml) or by
   a future `daalu init wg-hub` one-shot; rotating it would break
   every live tunnel until customers re-onboard.
3. **Renders the new `wg0.conf`** with `render_hub_config`. Each
   Peer block carries `# tenant=… cluster=…` comments for human
   debuggability and a `PersistentKeepalive = 25` so customers
   behind NAT get pinhole refresh from the hub side too. Peers
   without a `customer_pubkey` are silently skipped — emitting a
   `[Peer]` block with no `PublicKey` would crash wg-quick.
4. **Patches the Secret** via `CoreV1Api.patch_namespaced_secret`,
   writing `stringData.wg0.conf`. The kubelet refreshes
   secret-mounted volumes ~every 60s.
5. **Inside the hub container** (`deploy/wg-hub/entrypoint.sh`),
   an `inotifywait` loop watches `/etc/wireguard/wg0.conf`. When
   the file changes, it runs `wg syncconf wg0 <(wg-quick strip
   wg0)` — this is the surgical update path: established handshakes
   survive, only added/removed/modified peers are reconciled.

The kubernetes-client calls run via `asyncio.to_thread` so they
don't block the FastAPI event loop.

### 2.6 Tunnel-health beat task

`workers/wireguard_health.py` runs every 30 seconds (registered as
`wireguard.poll_tunnel_health` in `workers/celery_app.py`'s
`beat_schedule`). One pass:

1. Locate the hub pod by label selector `app=daalu-wireguard-hub`
   in the daalu-api's own namespace. If none is found
   (`wireguard.enabled=false` in values), the tick logs and
   returns — no DB writes.
2. `kubectl exec`-equivalent (`kubernetes.stream.stream`) into the
   pod and run `wg show wg0 dump`. `dump` is the script-friendly
   variant: one line per peer, tab-separated, with a numeric Unix
   epoch in column 5 for the latest handshake (`0` if none yet).
3. Parse the dump (`parse_wg_dump`). The first line is the
   interface itself and is skipped.
4. For every `ClusterTunnel` row with a non-NULL `customer_pubkey`,
   look up the matching peer entry in the dump and classify
   (`_classify`) the handshake age:
   - ≤ `wireguard_connected_window_s` (default 180s): `connected`
   - ≤ `wireguard_degraded_window_s` (default 600s): `degraded`
   - else: `error`
   - peer in dump but `ts == 0`, or peer not in dump:
     `awaiting_handshake`
5. Update `status` and `last_handshake_at` on every row whose
   bucket changed, then commit.
6. Call `sync_hub_secret` again. This is the **self-healing leg**:
   if the previous callback's sync attempt failed transiently
   (e.g. kubelet stale, transient API server hiccup), the beat
   tick re-pushes the desired Peer set. The patch body is the same
   regardless of whether anything drifted, so this is a cheap
   idempotent step.

### 2.7 At-rest crypto

`core/crypto.py` wraps Fernet:

- The Fernet key is sha256(`settings.secret_key`) base64-encoded.
  Multiple processes derive the same key without needing a
  separate `FERNET_KEY` env var.
- Rotating `secret_key` invalidates every ciphertext. This is the
  intended behaviour — re-encrypt before swapping the key. There
  is no fallback decryption with the old key, so a fresh
  `secret_key` cannot read existing rows. Operators who care about
  hot rotation must re-encrypt all `operator_privkey_encrypted`
  values first.

### 2.8 Mesh topology recap

```
                            10.200.0.0/16
                                  │
            ┌──────────────────── hub (10.200.0.1) ─────────┐
            │            UDP/51820 on a public hostname     │
            │                                               │
   wg syncconf on              [daalu-wireguard-peers]      │
   Secret reload                Secret (wg0.conf)           │
            │                                               │
            └── Peer A (10.200.0.2) ──── Peer B (10.200.0.3) … 
                  customer cluster A         customer cluster B
                  daalu-edge pod             daalu-edge pod
```

The hub itself is **never** the initiator. Each customer-side
edge dials out (`Endpoint = hub_endpoint:51820`,
`PersistentKeepalive = 25`); the hub waits. This makes the design
NAT-friendly out of the box — most customer clusters have outbound
internet but no inbound rule for arbitrary UDP.

### 2.9 Configuration surface

Settings in `config.py` (read at process start, lru_cached):

| Setting | Default | Meaning |
|---|---|---|
| `wireguard_public_endpoint` | `""` | Hostname the customer edges dial. Empty disables onboarding (returns 503 with hint). |
| `wireguard_listen_port` | `51820` | UDP port; baked into snippets and the hub config. |
| `wireguard_hub_address` | `10.200.0.1` | Hub's address on the mesh. |
| `wireguard_connected_window_s` | `180` | Beat-task threshold for "fresh handshake". |
| `wireguard_degraded_window_s` | `600` | Beat-task threshold for "stale but recent". |

Helm `values.yaml` keys (`deploy/helm/daalu-automation/values.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `wireguard.enabled` | `false` | Master switch — disables hub deployment + RBAC. |
| `wireguard.image` | `ghcr.io/kiwueke1/daalu-wg-hub:latest` | Hub container image. |
| `wireguard.listenPort` | `51820` | Mirrors `wireguard_listen_port`. |
| `wireguard.hubAddress` | `10.200.0.1` | Mirrors `wireguard_hub_address`. |
| `wireguard.hubPrivateKey` | `""` | Seed the hub's keypair at install time. Subsequent re-installs do not overwrite the Secret (resource-policy: keep). |
| `wireguard.nodeSelector` | `{}` | Pin the hub Deployment so hostPort lands on a node with the public DNS pointer. |

### 2.10 Apiserver-proxy sidecar on the edge pod

The WireGuard tunnel routes packets to the edge pod's wg0 address,
but the customer's kube-apiserver doesn't listen there — it sits on
the in-cluster ``kubernetes.default.svc.cluster.local:443`` Service.
Without bridging the two, the operator's agent could see the IP route
but get ``Connection refused`` on every `kubectl`-style call.

The `apiserver-proxy` container in the daalu-edge chart (introduced
in chart version `0.3.0`, see
`deploy/helm/daalu-edge/templates/deployment.yaml`) is the bridge.
It runs a stock `alpine/socat` image with a single command:

```
socat -d TCP-LISTEN:6443,fork,reuseaddr TCP:kubernetes.default.svc.cluster.local:443
```

- **Network namespace.** Same as the wireguard container, so it
  binds in the namespace that owns wg0. The listener on
  `0.0.0.0:6443` accepts both packets arriving from wg0 (the
  operator side) and any pod on the customer cluster that resolves
  to this pod's IP. The customer is welcome to add a NetworkPolicy
  if they want to restrict the latter.
- **TLS passthrough.** socat speaks raw TCP — it does not terminate
  TLS, decrypt, or re-encrypt anything. The TLS session is between
  the operator's `kubectl` and the customer's kube-apiserver, with
  the proxy just shuffling bytes. The kubeconfig stored in the
  tenant's `kubernetes` Integration row must either use
  `insecure-skip-tls-verify: true` (the current default, because the
  apiserver cert won't include the tunnel IP in its SAN list), or
  ship a CA + cert minted with the tunnel IP as an additional SAN.
  For k3s this is `--tls-san 10.200.0.X` in the server config; for
  kubeadm it's `apiServer.certSANs`.
- **Upstream.** Defaults to the cluster's `Service/kubernetes`
  ClusterIP via Service DNS, so it works on any conformant cluster
  without knowing the API server's real address. Override
  `apiserverProxy.upstream` in values if the cluster has a
  non-standard layout.
- **Failure mode if disabled.** Setting `apiserverProxy.enabled=false`
  in the edge chart turns this off; the tunnel still carries
  WireGuard traffic, but the operator's agent loses all kubectl
  capability and the chat will fall back to suggestions-only mode.

Configuration surface (in `deploy/helm/daalu-edge/values.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `apiserverProxy.enabled` | `true` | Toggles the sidecar. |
| `apiserverProxy.image` | `alpine/socat:1.8.0.0` | Proxy image. |
| `apiserverProxy.listenPort` | `6443` | Port on wg0 the operator dials. Bake into the kubeconfig's `server:`. |
| `apiserverProxy.upstream` | `kubernetes.default.svc.cluster.local:443` | Where to forward to. |

---

## 3 · Failure modes and the recovery path for each

| Symptom | Likely cause | Fix |
|---|---|---|
| `POST /clusters` returns 503 with "wireguard_public_endpoint is not configured" | Setting unset on the daalu-api pod | Set `WIREGUARD_PUBLIC_ENDPOINT` in the operator Secret and roll the pod |
| `POST /clusters` returns 409 "slug already exists" | Tenant has a row with that slug (current or soft-deleted) | Pick a different slug, or delete the old row first |
| Customer install fails: callback returns 409 "already redeemed" | They installed once already (the marker file in `STATE_DIR` is missing but the row was redeemed) | Delete the row on the operator side, re-onboard, paste fresh snippet |
| Edge pod in `CrashLoopBackOff` with `curl: (22) ... 422` on the bootstrap callback, but a manual replay of the same JSON body from elsewhere (e.g. operator's laptop) against `/clusters/{slug}/callback` returns 200 | A pydantic validation error in the pod's request body that does not reproduce when curl is run by hand (suspected: `jq` empty-string handling of `customer_endpoint` when `OUR_PUBLIC_ENDPOINT` is unset on certain `jq` builds; not fully root-caused). The successful manual replay consumes the one-shot invite token, so further pod retries get 409 and the pod keeps crashing | Plant the `.bootstrapped` marker by hand and let the pod skip the (already-completed) callback on next boot: ① `kubectl scale deploy/daalu-edge --replicas=0` and wait for the pod to delete; ② apply a debug pod that mounts the `daalu-edge-state` PVC and runs `touch /state/.bootstrapped`; ③ delete the debug pod; ④ `kubectl scale deploy/daalu-edge --replicas=1`. Edge logs should then show `[edge] already bootstrapped; skipping callback` and `[edge] interface wg0 up`. Alternative if you'd rather start clean: delete the row on the operator side and re-onboard with a fresh snippet (loses the existing edge keypair) |
| Tunnel handshake is healthy (edge `wg show wg0` shows `latest handshake: Now`, hub `wg show wg0 dump` shows non-zero column 5) but the operator row stays in `awaiting_handshake` indefinitely — UI never flips. Beat-task logs show `wireguard.health.no_cluster error='not running in-cluster (no SA mount)'` every 30s | The `daalu-worker` Deployment was rendered with `automountServiceAccountToken: false`. The chart's API deployment correctly toggles this on when `remediator.enabled=true` (see `templates/api.yaml`), but the workers chart was missing the same conditional, so the celery worker has no SA token mounted and bails out of `poll_tunnel_health` before reading the hub dump | Chart fix in `templates/workers.yaml`: wrap the worker `spec:` with the same `if .Values.remediator.enabled` / `serviceAccountName: ... / automountServiceAccountToken: true` block used in `api.yaml`. To unblock a live cluster without re-rolling the chart: `kubectl -n daalu-automation patch deploy daalu-worker --type=strategic -p '{"spec":{"template":{"spec":{"serviceAccountName":"daalu-remediator","automountServiceAccountToken":true}}}}'` then `kubectl rollout status deploy/daalu-worker` |
| Beat task gets past SA mount but logs `wireguard.health.exec_failed ... pods "daalu-wireguard-hub-…" is forbidden: User "system:serviceaccount:daalu-automation:daalu-remediator" cannot get resource "pods/exec"` | The `daalu-wireguard-coordinator` Role only granted `secrets` for `sync_hub_secret`; `pods/exec` was missing. Also requires both `get` AND `create` verbs: `kubectl exec` uses POST (`create`) but the python `kubernetes-client.connect_get_namespaced_pod_exec` uses GET (`get`) — the API server upgrades the GET to a WebSocket. Chart fix landed in `templates/wireguard-hub.yaml` | To unblock a live cluster: `kubectl -n daalu-automation patch role daalu-wireguard-coordinator --type=json -p='[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["pods/exec"],"verbs":["get","create"]}}]'`. Verify with `kubectl -n daalu-automation auth can-i get pods --subresource=exec --as=system:serviceaccount:daalu-automation:daalu-remediator` returning `yes` |
| `wireguard.poll_tunnel_health` succeeds the first time after a worker pod start, then every subsequent tick raises `RuntimeError: ... got Future ... attached to a different loop`. The row stays at whatever status the first tick wrote; `last_handshake_at` stops advancing | SQLAlchemy's async engine pins its connection pool to the first event loop that touches it. The task's `asyncio.run(_poll_once())` creates a fresh loop every tick, so the shared `engine`'s pool still references the previous loop's futures, and the second call fails the moment it touches the DB | Code fix in `src/daalu_automation/workers/wireguard_health.py`: wrap `_poll_once()` in an inner coroutine and call `await engine.dispose()` in a `finally` clause so each tick gets a fresh pool. Requires rebuilding the worker image and rolling the deployment. To work around in a live cluster without a rebuild: run the task once-per-need from the `daalu-api` pod (`kubectl -n daalu-automation exec deploy/daalu-api -c api -- python -c "import asyncio; from daalu_automation.workers.wireguard_health import _poll_once; print(asyncio.run(_poll_once()))"`) — each exec spawns a fresh python process so the loop-reuse issue doesn't bite. The row stays at `connected` between manual polls as long as no failed beat tick overwrites it |
| Row stuck in `awaiting_handshake` | UDP/51820 blocked between customer and hub, or wrong hub.publicKey in the snippet | Have the customer check pod logs: `kubectl -n <ns> logs deploy/daalu-edge`; firewall the UDP, or re-onboard if the snippet was old |
| Row flips between `connected` and `degraded` | Peer behind aggressive NAT eating the keepalive | Bump `PersistentKeepalive` on the customer side (already 25s by default) |
| Hub pod stuck in CrashLoopBackOff | `wireguard.hubPrivateKey` never set on first install AND no `daalu init wg-hub` run | Either re-install with `wireguard.hubPrivateKey=<base64>`, or kubectl edit the `daalu-wireguard-peers` Secret to seed `wg0.conf` manually |
| `helm install daalu-edge` fails on `cluster.slug is required` | Forgot one of the `--set` flags | Re-paste the full snippet — the chart's helper enforces all five required values |

---

## 4 · Test coverage

`tests/test_clusters.py` covers:

- crypto Fernet round-trip
- X25519 keypair shape (32-byte raw, distinct between calls)
- invite-token hash stability and url-safety
- `render_hub_config` skipping peers without a pubkey (the
  "would-crash-wg-quick" guard)
- `_classify` bucketing for every status edge
- `parse_wg_dump` skipping the interface line
- `POST /clusters` happy path, including snippet shape
- 503 when `wireguard_public_endpoint` is unset
- 409 on duplicate slug
- cross-tenant isolation (A's cluster invisible to B)
- one-shot token (second redemption → 409)
- bad token (401)
- callback bypass of AuthGateMiddleware (no auth header → reaches
  route, not middleware 401)
- IP allocator skips the hub IP and avoids collisions

14 tests, no skips. Run with `pytest tests/test_clusters.py -v`.
