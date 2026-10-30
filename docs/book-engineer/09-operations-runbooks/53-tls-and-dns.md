# 53. Cert-manager, TLS, and DNS

Daalu's operator app is fronted by Traefik on k3s, with TLS
certificates provisioned by cert-manager via DNS-01
challenges against Cloudflare.

This chapter is the practitioner's notes.

---

## The pieces

- **Cloudflare** — DNS for your domain (zone ID
  `<zone-id>`). Records are not pre-created.
- **cert-manager** — runs in `cert-manager` namespace.
  Talks to Let's Encrypt to issue certs.
- **Traefik** — k3s's bundled ingress controller. Serves
  TLS on 443, redirects 80 → 443.
- **DNS-01 solver** — uses Cloudflare API.

---

## Issuer config

`ClusterIssuer` defined in
`deploy/k8s/letsencrypt-issuer.yaml`:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - dns01:
          cloudflare:
            apiTokenSecretRef:
              name: cloudflare-api-token
              key: api-token
```

The Cloudflare token has DNS:edit scope for the daalu.io
zone.

---

## The Cloudflare token placement gotcha

cert-manager v1.16 reads the API-token secret from the
**challenge's namespace**, not from cert-manager's. So the
secret has to exist in every namespace that creates a
Certificate.

Symptom when it is missing: TLS challenges sit in pending
forever with "secrets cloudflare-api-token not found."

`install.sh` copies the token from cert-manager to the
daalu-automation namespace on every run:

```bash
kubectl -n cert-manager get secret cloudflare-api-token -o yaml \
  | sed "s/namespace: cert-manager/namespace: $NAMESPACE/" \
  | kubectl apply -n "$NAMESPACE" -f -
```

Idempotent. If you bootstrap a new namespace, copy the secret
there too.

---

## DNS records

Records under `daalu.io` (Cloudflare DNS):

- `ops.example.com` — A record pointing at the cluster's
  external IP `<ingress-ip>`.
- `grafana.example.com` — same.
- `argocd.example.com` — same.
- `hub.example.com` — same (for federation tunnels).
- `status.daalu.io` — CNAME to status-page provider.

DNS is **not** pre-created for new hostnames. When we add a
new ingress, we create the A record manually.

---

## Ingress

Our chart's Ingress (Traefik):

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: daalu-ingress
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls: "true"
spec:
  ingressClassName: traefik
  tls:
    - hosts: [ops.example.com]
      secretName: ops-daalu-io-tls
  rules:
    - host: ops.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: daalu-frontend
                port: { number: 80 }
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: daalu-api
                port: { number: 80 }
          - path: /docs
            pathType: Prefix
            backend:
              service:
                name: daalu-api
                port: { number: 80 }
          - path: /openapi.json
            pathType: Exact
            backend:
              service:
                name: daalu-api
                port: { number: 80 }
```

`cert-manager.io/cluster-issuer: letsencrypt-prod` triggers
cert-manager to provision a cert for the hostnames in
`spec.tls`.

---

## First-issuance timing

The first time you deploy with a new hostname:

1. Apply the Ingress.
2. cert-manager creates a Certificate resource.
3. Certificate triggers a CertificateRequest.
4. CertificateRequest triggers a Challenge.
5. Challenge calls Cloudflare API to set the `_acme-challenge`
   TXT record.
6. Waits for DNS propagation (~30–60 s).
7. Let's Encrypt validates.
8. Cert issued and stored in the named Secret.
9. Traefik picks up the Secret and serves TLS.

Total: ~90 seconds from apply to live HTTPS.

For renewals it's the same but happens 30 days before
expiry.

---

## Diagnostic commands

```bash
# Cert status
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation get certificate
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation describe certificate ops-daalu-io-tls

# Recent challenges
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation get challenge
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation describe challenge

# cert-manager controller logs
kubectl --kubeconfig "$KUBECONFIG_OPS" -n cert-manager logs deploy/cert-manager

# Inspect the cert from outside
echo | openssl s_client -showcerts -servername ops.example.com -connect ops.example.com:443 2>/dev/null | openssl x509 -inform pem -noout -text | head -30
```

---

## Rate limits

Let's Encrypt has weekly issuance limits:

- 50 certificates per registered domain per week.
- 5 duplicate certificates per week.
- 5 failed validations per account per hostname per hour.

For development/test, use `letsencrypt-staging` (a separate
ClusterIssuer pointing at the staging endpoint). The staging
issuer has higher limits but issues untrusted certs.

---

## What's next

Chapter 54 — debugging NetworkPolicies.
