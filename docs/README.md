# Daalu Automation — documentation

Start here:

1. **[architecture.md](architecture.md)** — what the platform is, why
   it's shaped this way, and how the pieces fit together.
2. **[system-walkthrough.md](system-walkthrough.md)** — low-level
   design with code references for every subsystem, plus end-to-end
   trace examples. Read this with the codebase open.
3. **[getting-started.md](getting-started.md)** — run the full stack
   locally in 10 minutes.
4. **[extending.md](extending.md)** — add a new domain module (Support,
   Finance, HR, …) without touching platform code.
5. **[Helm chart](book-engineer/08-deployment-and-cicd/44-helm-chart.md)**
   and **[install.sh](book-engineer/08-deployment-and-cicd/45-install-sh.md)**
   — **canonical** deploy path: Helm chart + GHCR + one-shot install
   script for your operator cluster.
6. **[runbooks/](runbooks/README.md)** and the
   **[operations runbooks](book-engineer/09-operations-runbooks/49-common-incidents.md)**
   — on-call quick reference.
7. **[ui-design-system.md](ui-design-system.md)** — tokens, motion,
   components.

Books and feature guides:

- **[book-engineer/](book-engineer/README.md)** — the engineering handbook.
- **[book-customer/](book-customer/README.md)** — the user handbook.
- **[cluster-tunnel-feature.md](cluster-tunnel-feature.md)** — WireGuard
  cluster federation (hub + daalu-edge).
- **[cloud-onboarding.md](cloud-onboarding.md)** and
  **[cloud_integrations/](cloud_integrations/)** — cloud provider
  integrations.
- **[sot-device-management/](sot-device-management/)** — Nautobot
  source-of-truth and device management.
- **[acronyms.md](acronyms.md)** — glossary of acronyms.
