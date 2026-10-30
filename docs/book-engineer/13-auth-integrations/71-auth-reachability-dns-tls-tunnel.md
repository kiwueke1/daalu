# 71. Reachability: DNS, TLS, firewall & the tunnel for auth

This chapter is the consolidated map of the **network and PKI substrate** the
cmtools tool-SSO path stands on. Everything in section 13 that touches a token,
a cookie, or a JWKS fetch assumes these four layers are already in place: a
wildcard DNS record, a wildcard Let's Encrypt cert, an open host firewall, and a
WireGuard tunnel terminated by a *restart-safe* `daalu-edge`. When one of them is
wrong the failure surfaces far from its cause — a "buffers forever" browser, a
`502` from the proxy, a `409` crash-loop on the edge, a Keycloak login that
hangs behind NAT. This chapter exists so you can tell those apart.

> Read the other section-13 chapters for the *auth logic* — this chapter is only
> the plumbing under them. For the request flow through the hub proxy see
> [68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md); for
> the per-user token exchange see
> [69. Per-user token-exchange](69-per-user-token-exchange.md) and
> [59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md). For TLS/DNS as a
> general operational topic (the `ops.example.com` substrate, cert-manager
> mechanics, the ClusterIssuer), see
> [53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md) —
> this chapter only adds the cmtools- and auth-specific deltas and does not
> re-document cert issuance.

It crosses four planes that are owned by four different systems — Cloudflare
(DNS), cert-manager + Let's Encrypt (TLS), the cloud/host firewall (L4
ingress), and WireGuard + `daalu-edge` (the private data path). Each is cited to
the script or manifest that owns it. Cross-references:
[24. The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md),
[25. Deploying daalu-edge](../05-cluster-federation/25-daalu-edge.md),
[53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md),
and [68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md) +
[21. The NV-CM network plane](../04-source-of-truth/21-nvcm-network-plane.md),
which define the `auth.example.com` / `*.cmtools.example.com` hostnames in the first
place.

---

## 71.1 The picture

There are two hostnames the browser must reach for tool-SSO to work, and they
are reached **two different ways**:

```
                 ┌──────────────────────────────────────────────────────────┐
  browser ──TLS──┤ auth.example.com        PROXIED (orange cloud)               │
                 │   Cloudflare edge ──► Keycloak (KC_PROXY=edge)            │
                 └──────────────────────────────────────────────────────────┘

                 ┌──────────────────────────────────────────────────────────┐
  browser ──TLS──┤ *.cmtools.example.com   DNS-only (grey cloud)                │
                 │   A → <ingress-ip> (hub traefik LB)                    │
                 │   traefik terminates the LE *.cmtools wildcard            │
                 │     │                                                     │
                 │     ▼ cmtools Ingress → daalu-api (CmtoolsHostRouter)    │
                 │     ▼ tool_proxy → WireGuard tunnel                       │
                 │           │                                               │
                 │           ▼ daalu-edge forward proxy (X-Daalu-Forward-Host)│
                 │           ▼ tenant Envoy gateway (HTTP-only) → tool UI    │
                 └──────────────────────────────────────────────────────────┘
```

The asymmetry is the single most important thing in this chapter and the most
common way to get it wrong:

| Host                  | Cloudflare mode     | Who terminates TLS         | Why                                                            |
| --------------------- | ------------------- | -------------------------- | ------------------------------------------------------------- |
| `auth.example.com`       | **PROXIED** (orange) | Cloudflare edge            | NAT-hairpin: the A record points at the operator's own public IP, unreachable behind that NAT without the CF edge in front |
| `*.cmtools.example.com`  | **DNS-only** (grey)  | hub **traefik** (LE wildcard) | cert-manager DNS-01 needs the origin reachable for ACME; a 2-level `*.cmtools` wildcard is **not** covered by Cloudflare's universal ACM cert |

Both are in your Cloudflare zone (`<zone-id>`).
Get the orange/grey cloud wrong on either and the auth path breaks in a way that
looks like an application bug.

---

## 71.2 Cloudflare DNS

### `*.cmtools.example.com` — DNS-only, A → the hub traefik LB

One wildcard A record covers every tenant and every tool. Tool hosts are **flat
single-label** names `<tool>-<slug>.cmtools.example.com` (the config-browser UI is
the bare `<slug>.cmtools.example.com`), produced by
`config_manager_controller/values.py:compute_urls`. Because they are all
one label under the same zone, **one** A record and **one** wildcard cert serve
all of them.

```
CF_API_TOKEN=<Zone:DNS:Edit on your zone> \
GATEWAY_IP=<ingress-ip> \
NAME='*.cmtools.example.com' \
  deploy/scripts/config-manager/dns-wildcard.sh
```

`deploy/scripts/config-manager/dns-wildcard.sh` is the source of truth: it is
idempotent (updates in place), targets your zone, and — critically —
keeps `PROXIED=false`:

```bash
PROXIED="${PROXIED:-false}"   # wildcard + cert-manager DNS-01 → keep DNS-only
```

DNS-only is **load-bearing**, not an accident:

1. **cert-manager must terminate, not Cloudflare.** The hub issues the
   `*.cmtools.example.com` Let's Encrypt wildcard itself (§71.3). If the record were
   PROXIED, Cloudflare would intercept `:443` and present *its* cert — and its
   universal ACM cert only covers `example.com` and one-level `*.example.com`, **not**
   the two-level `*.cmtools.example.com`. Browsers would get a cert error.
2. The script's GATEWAY_IP is `<ingress-ip>` — the hub's **traefik**
   LoadBalancer (e.g. single-node k3s), where the cmtools Ingress lives.
   Note the script's own header text still references the older
   `*.cm.example.com` per-tenant scheme; for cmtools you override `NAME` and
   `GATEWAY_IP` as above.

Verify:

```
dig +short '*.cmtools.example.com'      # → <ingress-ip>
```

### `auth.example.com` — PROXIED, and why DNS-only was the NAT-hairpin bug

Keycloak's external hostname **must be PROXIED (orange cloud)**. It was
originally DNS-only, pointed at the operator's own public IP
(`<public-ip>`). From a browser *inside the same NAT* that public IP is not
routable back to the host — the classic **NAT-hairpin** failure: packets leave
for the public IP and never loop back. Flipping the record to PROXIED puts the
Cloudflare edge in front, so the browser talks to Cloudflare and Cloudflare
reaches the origin — no hairpin.

This only works because **Keycloak is proxy-ready**: it runs with
`KC_PROXY=edge` and `KC_HOSTNAME=auth.example.com`, so it trusts the
`X-Forwarded-*` headers from the Cloudflare edge and emits issuer/redirect URLs
on the public hostname rather than its internal address. Without `KC_PROXY=edge`
the proxied requests would produce wrong-host redirects.

Note the token `iss` and the OIDC redirect URIs are all the **external**
`auth.example.com` — that is the host the browser must reach, and the host the
realm signs into tokens. (Inside the workload cluster the same name resolves to
a private `10.x` address and 404s; that is exactly why the tenant gateway uses
an **internal** JWKS URL — see §71.5 and
[68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md).)

> If Keycloak runs on the hub cluster instead, `auth.example.com` points at the
> hub traefik LB `<ingress-ip>`; keep it **proxied** for the same reasons.

### Cloudflare Browser Integrity Check vs machine OIDC clients (error 1010)

Being proxied cuts both ways: Cloudflare's **Browser Integrity Check** (BIC)
403-blocks requests whose `User-Agent` doesn't look like a browser, returning
**error 1010**. The OIDC endpoints under
`auth.example.com/protocol/openid-connect/` are hit by *machine* clients
constantly — a customer gateway's Envoy fetching JWKS, Nautobot's auth plugin
fetching JWKS, the hub's `httpx` token/exchange calls — and BIC blocked them
while every browser request sailed through. The observed symptom was
maddeningly indirect: token exchange worked, the tenant gateway's JWKS fetch
silently failed, and users dropped to the tool's own login (a second cause of the same
symptom is the online-`krt` idle expiry, [69 §2.1](69-per-user-token-exchange.md)).

Fix: a **zone Configuration Rule** in your Cloudflare zone that
disables Browser Integrity Check for requests matching
`auth.example.com/protocol/openid-connect/*`. This lives only in Cloudflare —
nothing in this repo provisions it — so a fresh zone (or an overzealous
security-settings pass) silently reintroduces the block. When JWKS fetches 403
with a Cloudflare HTML error page, check this rule before anything Keycloak-side.

---

## 71.3 TLS — the `*.cmtools.example.com` Let's Encrypt wildcard

The hub holds one wildcard cert and terminates TLS for every tool host at
traefik. Two manifests own it:

- `deploy/helm/daalu-automation/templates/cmtools-certificate.yaml` — a
  cert-manager `Certificate` named `cmtools-wildcard`, `commonName`/`dnsNames`
  `*.{{ baseDomain }}` (default `*.cmtools.example.com`), `issuerRef` the
  `letsencrypt-prod` **ClusterIssuer**, into secret `cmtools-wildcard-tls`.
  Wildcards **require DNS-01**, so the issuer is the Cloudflare DNS-01 solver.
- `deploy/helm/daalu-automation/templates/cmtools-ingress.yaml` — the `cmtools`
  Ingress (`*.cmtools.example.com` → `daalu-api:80`, ingressClass `traefik`)
  referencing that TLS secret. The template comment is explicit that there is
  **no** cert-manager annotation on the Ingress — an annotation would trigger an
  HTTP-01 challenge, which cannot satisfy a wildcard.

Both are gated on `cmtools.enabled`.

### The gotcha that costs you four hours: the Cloudflare token's namespace

cert-manager's Cloudflare DNS-01 solver reads the API-token secret from the
**challenge's namespace**, not from `--cluster-resource-namespace`. The
cmtools challenge runs in `daalu-automation`, so the `cloudflare-api-token`
secret must exist **in `daalu-automation`**, not only in `cert-manager`. If it
isn't, the `cmtools-wildcard` Certificate sits `Ready=False` forever with:

```
secrets "cloudflare-api-token" not found
```

`deploy/scripts/install.sh` handles this idempotently — it copies the secret
from `cert-manager` into the target namespace at install time:

```bash
kubectl -n cert-manager get secret cloudflare-api-token -o yaml \
  | grep -v -E '^\s+(namespace|resourceVersion|uid|creationTimestamp):' \
  | sed "s/namespace: cert-manager/namespace: ${NAMESPACE}/" \
  | kubectl apply -n "$NAMESPACE" -f -
```

(The `grep -v` scrub strips the source `namespace`/`resourceVersion`/`uid`/
`creationTimestamp` so the re-applied object is namespace-clean and not pinned to
a stale `resourceVersion`.)

This is the same DNS-01 pattern as `ops.example.com`; the cmtools-specific delta is
only the *namespace* of the copied secret. The general mechanics live in
[53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md).

Verify:

```
kubectl -n daalu-automation get certificate cmtools-wildcard    # READY=True
```

---

## 71.4 Cloud firewall — open 80/443 on the hub node

DNS-only means browsers connect **directly** to the hub origin
`<ingress-ip>:80/443` — there is no Cloudflare edge in front of cmtools to
hide behind. So the hub node's L4 firewall must accept the public.

If the cloud firewall on the hub server is restricted to
**Cloudflare IP ranges only** (correct for the CF-proxied `ops.example.com`, fatal
for DNS-only cmtools), the symptom is: `nautobot-default.cmtools.example.com` times
out from a laptop *and* from a workload-cluster pod, while `ops.example.com` works
— because `ops.example.com` is CF-proxied and cmtools is not.

Fix: open **80/443 to `0.0.0.0/0` (and `::/0`)** on the hub node's firewall
(preserving the existing SSH rule). This is an
**infra-level** change on the cloud-provider side — there is no
`loadBalancerSourceRanges` on the traefik Service to do it from the cluster, and
the block is the cloud firewall, not Kubernetes. It can be re-restricted to
Cloudflare ranges later only if cmtools is also moved behind the CF proxy (which
it is not, for the cert reason in §71.2).

---

## 71.5 The WireGuard tunnel and the daalu-edge forward proxy

Once a request lands on `daalu-api`, the proxy must reach a tool UI that lives
on a **private `10.x` ClusterIP** inside the tenant's workload cluster — there is
no public route to it. That last hop runs over the WireGuard mesh through the
tenant's `daalu-edge` forward proxy. See
[24. The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md) for the
mesh and [25. Deploying daalu-edge](../05-cluster-federation/25-daalu-edge.md)
for the edge bootstrap; this section covers only the two things the auth path
adds: the **host-preservation header** and the **persistence requirement**.

### The data path and `X-Daalu-Forward-Host`

`api/tool_proxy.py` dials the edge as an HTTP forward proxy
(`http://<tunnel_ip>:8888`, resolved from the integration's `cluster_tunnel_id`
via `core/cluster_proxy.get_proxy_url`). The browser-facing host stays **flat**
end-to-end — `<tool>-<slug>.cmtools.example.com` — and that flat host must survive
the hop so the tenant Envoy gateway can host-route it. But the proxy targets the
gateway by its in-cluster `*.svc.cluster.local` / `10.x` address (so the edge's
allowlist and kube-dns resolve it), which would normally reset the upstream
`Host`. To prevent that, the proxy passes the public flat host in
`X-Daalu-Forward-Host`, and the edge honors it.

`deploy/edge/proxy.py:http_handler` consumes the header and pins it as the
upstream `Host`:

```python
forward_host = request.headers.get("X-Daalu-Forward-Host")
if forward_host:
    fwd_headers["Host"] = forward_host
```

It is listed in the hop-by-hop drop set so it is never forwarded further
upstream. The edge listens **only on the `wg0` tunnel IP** — the per-peer
WireGuard cryptography *is* the auth boundary for this proxy (the same trust
model as the apiserver-proxy socat sidecar), and an `ALLOWED_HOSTS` regex
(default: `*.svc(.cluster.local)` + RFC1918 literals) blocks public dial-outs so
a compromised hub can't turn the edge into an exfiltration relay.

> The flat host is preserved **as-is** — there is no flat→nested rewrite. Earlier
> design notes that described the edge forwarding a *nested*
> `<tool>.<slug>.cmtools.example.com` are obsolete: the tenant gateway was
> re-provisioned to route the flat host directly, and the proxy forwards the
> flat `public_host` unchanged. End-to-end the host is flat. The tenant gateway
> validates the injected JWT against an **internal** keycloak Service URL (the
> external `auth.example.com` 404s inside the workload cluster) — see §71.2,
> [68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md) and
> [69. Per-user token-exchange](69-per-user-token-exchange.md) for the JWKS /
> token-exchange detail.

### Persistence is mandatory — the emptyDir restart that took the tunnel down

The edge's WireGuard identity is its keypair: the keypair is generated edge-side
and the **public key becomes the durable cluster identity** the hub peers with
(see [25. Deploying daalu-edge](../05-cluster-federation/25-daalu-edge.md)). The
state directory (`STATE_DIR=/var/lib/daalu-edge`, holding `wg0.priv` and the
`.bootstrapped` marker) **must be a persistent PVC** —
`persistence.enabled=true` — never an `emptyDir`.

The failure mode when it is an `emptyDir`: a pod restart wipes the state, the
edge generates a *new* WG private key (so the hub no longer recognizes it as a
peer), and the entrypoint re-POSTs the one-shot bootstrap callback
(`/clusters/<slug>/callback`) — which returns **`409` "invite token already
redeemed"** because the cluster was onboarded once already. The WireGuard
container then crash-loops on the 409 and the **whole tunnel goes down** — taking
the tool proxy *and* the co-resident apiserver-proxy / svcproxy sidecars with it.

Two fixes, both required, both shipped:

1. **Persistent PVC** for `STATE_DIR` so the WG key and bootstrap marker survive
   a restart (`persistence.enabled=true` on the `daalu-edge` chart;
   `local-path`/Ceph PVC, not `emptyDir`).
2. **Restart-safe bootstrap** — the edge `entrypoint.sh` treats a bootstrap
   `409` (token already redeemed) as "already bootstrapped" and brings `wg0`
   **up** instead of crash-looping. The edge image must also carry the
   `deploy/edge/proxy.py` that honors `X-Daalu-Forward-Host`; an older image
   without it resets the Host to the svc name and the gateway 404s.

After any deliberate key change (e.g. re-onboarding), the hub side must still
**re-peer manually** — there is a known reconciler bug where `sync_hub_secret`
401s (lowercase-`bearer`). The path is: update `ClusterTunnel.customer_pubkey`
in the DB → run `sync_hub_secret(db)` → patch the `daalu-wireguard-peers`
secret. **Rolling the hub Deployment is no longer required** (PR #106): the
hub's entrypoint (`deploy/wg-hub/entrypoint.sh`) now watches the secret-mount
*directory* for kubelet's atomic `..data` symlink swap **and** runs a periodic
`wg syncconf` backstop (every 15 s, `WG_RECONCILE_INTERVAL`), so a patched
Secret converges within kubelet's sync period + one tick. The old behaviour —
an `inotifywait` on the *file path* whose watched inode was replaced by the
first `..data` swap, silently missing every later peer add ("awaiting
handshake" forever) — is the failure mode this fixed. One exception remains: a
hub *key rotation* rewrites the `[Interface]` section, which `syncconf` cannot
apply — that rare case still needs a pod restart. This is documented in
[24. The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md) and the
edge chapter; flagged here because an auth outage that bottoms out in a dead
tunnel is indistinguishable from an auth bug until you check the edge pod.

---

## 71.6 Putting the four layers in failure-diagnosis order

When tool-SSO is broken, walk the layers outside-in — each has a distinct
signature:

| Symptom | Layer | Check |
| --- | --- | --- |
| Browser cert error on `*.cmtools.example.com` | DNS / TLS | Is the wildcard A record **DNS-only**? Did Cloudflare intercept `:443`? Is `cmtools-wildcard` `Ready=True`? |
| `cmtools-wildcard` stuck `Ready=False`, `secrets "cloudflare-api-token" not found` | TLS | Token secret missing in the **`daalu-automation`** namespace (§71.3) |
| Connection **times out** from a browser/pod (no TLS at all) | Firewall | Cloud firewall still Cloudflare-IPs-only; open 80/443 to `0.0.0.0/0` (§71.4) |
| Keycloak login hangs / wrong-host redirect | DNS | `auth.example.com` DNS-only behind NAT (hairpin) — flip to **PROXIED**; confirm `KC_PROXY=edge` (§71.2) |
| Machine JWKS/token calls 403 with a Cloudflare HTML page (error **1010**); browsers fine | DNS / CF | Browser Integrity Check blocking non-browser UAs — restore the zone Configuration Rule for `auth.example.com/protocol/openid-connect/*` (§71.2) |
| Proxy returns a clear **`502`** | Tunnel / config | `gateway_internal_url` unset, or the WG tunnel/edge down |
| Edge pod **`409` crash-loop**, tunnel down | Tunnel | `emptyDir` state wiped on restart — needs PVC + restart-safe bootstrap (§71.5) |
| Gateway **`404`** on a reached request | Tunnel | Old edge image without `X-Daalu-Forward-Host`, so upstream `Host` was reset to the svc name (§71.5) |

The auth *logic* — cookie scope, the session JWT's `krt`/`kit` claims, the
Keycloak Standard Token Exchange, the gateway `SecurityPolicy`'s
`interactiveRedirect=false` gate — lives in
[68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md),
[69. Per-user token-exchange](69-per-user-token-exchange.md), and
[59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md). If you've
confirmed all four layers above are healthy and a tool still won't load, the
problem is up there, not here.
