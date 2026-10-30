# 3. Tech stack and the rationale behind each choice

The technologies in this codebase weren't picked at random.
Each one is a deliberate choice with at least one rejected
alternative. This chapter records those choices so future
contributors don't have to re-litigate them every six months.

If you think one of these decisions is wrong, send a patch
that revisits it. But please read this chapter first; most of
the obvious-sounding "why not X instead?" questions are
addressed below.

---

## Backend: Python + FastAPI

**What:** Python 3.12, FastAPI for HTTP, SQLAlchemy 2.0 for
ORM, Pydantic v2 for validation, Alembic for migrations.

**Why:**

- FastAPI gives us automatic OpenAPI docs and Pydantic
  validation for free. The OpenAPI is also what powers the
  `lib/api.ts` client generation.
- Python is what the broader infra/ops community knows.
  Adapter implementations for AWS / GCP / Kubernetes are all
  Python-first.
- SQLAlchemy 2.0's typed `Mapped[]` style plays well with
  Pydantic.

**Rejected:**

- **Go.** Tempting for raw performance and single-binary
  deploys, but the ecosystem for "talking to AWS SDK + ORM +
  LLM SDKs" is significantly less mature.
- **Rust.** Same plus a learning curve that would have
  slowed initial product velocity.
- **TypeScript on the backend.** Would have unified frontend
  and backend stacks, but the data-handling and ML library
  ecosystem in Python is multiple orders of magnitude
  larger.

---

## Frontend: Next.js + React + Tailwind

**What:** Next.js 14 App Router, React 18, TailwindCSS,
Radix UI primitives, React Query for data fetching.

**Why:**

- Next.js App Router gives us server components for the
  initial page load and a sane client-side hydration model.
- Tailwind keeps styling close to the component without
  inventing a new CSS-in-JS framework.
- Radix gives us accessible UI primitives without writing
  them ourselves.
- React Query handles cache, refetch, and loading state in
  one library; we don't have to invent that wheel.

**Rejected:**

- **SvelteKit / Solid.** Either would have been fine. We
  picked the stack where finding contributors is easiest.
- **Remix.** Promising but smaller ecosystem at the time of
  decision.
- **Pure SPA (no Next.js).** SSR matters for the initial
  load on a slow connection.

---

## Database: Postgres

**What:** PostgreSQL 16. Single instance in a StatefulSet.

**Why:**

- Boring, reliable, well-understood.
- JSONB columns let us flex schema where we want (user
  preferences, audit log payloads) without inventing a
  document store.
- k3s's local-path provisioner gives reliable PVC storage on
  a single node.

**Rejected:**

- **MySQL.** No technical reason against; we picked one.
- **CockroachDB / Yugabyte.** Tempting for distributed
  guarantees, but the operational overhead of running them
  at this stage of the company isn't worth the upside.
- **DynamoDB / Spanner.** Cloud-locks us; not portable for
  the self-hosted Enterprise story.

---

## Caching + broker: Redis

**What:** Redis 7. Single replica.

**Why:**

- Doubles as cache and Celery broker.
- Pub/sub for SSE notification fan-out.
- Cheap to operate.

**Rejected:**

- **RabbitMQ.** Better Celery broker in some ways but adds
  a second piece of infra.
- **Postgres LISTEN/NOTIFY.** Tempting (we already run
  Postgres) but the LISTEN/NOTIFY isn't fan-out friendly at
  the scale we expect.

---

## Background jobs: Celery

**What:** Celery 5, with Redis as both broker and result
backend.

**Why:**

- Mature, well-understood, integrates with FastAPI by being
  process-separate.
- Beat scheduler handles cron-style work.

**Rejected:**

- **arq / RQ.** Smaller libraries, comparable but with
  smaller ecosystems.
- **Temporal.** Strong technical fit (durable execution,
  retries, signals) but the operational overhead of
  running a Temporal cluster doesn't yet justify itself.
  We may revisit when state-machine workflows get more
  complex.
- **Native asyncio task queues.** Doable but would mean
  reinventing too much (retries, dead-letter, persistence).

---

## Kubernetes: k3s

**What:** A single-node k3s cluster (a dedicated server or VM
is enough for the operator cluster).

**Why:**

- Single-node simplicity at our current scale; sub-100 ms
  intra-cluster latency.
- k3s is fast to bootstrap and self-contained.

**Rejected:**

- **EKS / GKE / AKS.** Not required; the stack runs on any
  conformant cluster if you prefer a managed one.
- **Talos.** Excellent OS for clusters but we don't have the
  scale to justify its overhead.
- **No Kubernetes at all** (just systemd + Docker). We
  considered it; the federation pattern depends on
  Kubernetes-native primitives.

---

## LLM serving: vLLM

**What:** vLLM serving open-weight models (Llama 3.1, Qwen 2.5)
in customer clusters.

**Why:**

- High throughput, mature batching, OpenAI-compatible API.
- Open models avoid licensing entanglements.
- The customer pays the GPU cost once; per-call inference is
  free.

**Rejected:**

- **NVIDIA NIM / NGC.** Required entitlements, vendor
  lock-in, and pulled out of the architecture as part of
  the FOSS pivot.
- **Self-rolled inference server.** Would have been
  reinventing vLLM badly.
- **Cloud-only inference.** Cost.

---

## CI/CD: GitHub Actions

**What:** GitHub Actions builds images, runs tests, publishes
to GHCR. ArgoCD then... in theory deploys, but in practice
is misconfigured for the main daalu-automation Application
and the real deploys run via `deploy/scripts/install.sh`.

**Why:**

- The repo is on GitHub. Actions is the default.
- GHCR is built-in image registry; no separate Docker Hub
  needed.

**Rejected:**

- **GitLab CI.** Would have meant moving the repo.
- **Jenkins.** Self-host, more operational overhead.
- **CircleCI / Drone.** No specific reason against; just
  picked one.

---

## Observability: kube-prometheus-stack, Loki, Grafana

**What:** Prometheus + Alertmanager + Grafana via
kube-prometheus-stack; Loki + Promtail for logs.

**Why:**

- Standard k8s-observability stack; no custom tooling
  needed.
- Operates inside the same cluster as the app, scrapes via
  ServiceMonitors.

**Rejected:**

- **Datadog / New Relic / Honeycomb.** Cost and vendor
  lock-in; we operate at a scale where in-cluster Prom is
  ~$0.
- **OpenTelemetry-first.** Will move there eventually; not
  the priority right now.

---

## Auth: JWT + PATs + cookie sessions

**What:** Short-lived JWT in an HttpOnly cookie for browser
sessions, opaque PATs (`dpat_...`) for scripts.

**Why:**

- HttpOnly cookies are XSS-resistant; the JWT itself never
  hits client JS.
- PATs are sha256-hashed at rest; the cleartext is shown
  once.
- The shape mirrors GitHub's, which is a familiar pattern
  for the audience.

**Rejected:**

- **Auth0 / Clerk.** Cost and a hard third-party
  dependency on the critical login path.
- **OAuth-only (no passwords).** Will add SSO later;
  passwords are needed for the small-team trial use case.
- **JWE encrypted JWT.** Overkill — the JWT is already in
  an HttpOnly cookie.

---

## State management on the frontend: React Query + URL

**What:** Server state via React Query. UI state in React
useState. Filters / queries serialized into URL.

**Why:**

- Avoids Redux/Zustand/Recoil ceremony.
- URL-as-state means everything is bookmarkable and
  shareable.

**Rejected:**

- **Redux + RTK Query.** More boilerplate than we want.
- **Apollo.** Tied to GraphQL; we serve REST.
- **Zustand.** Fine choice but adds a dependency for
  little gain over plain React.

---

## Containerization: Docker (multi-stage)

**What:** Standard Dockerfile with multi-stage builds; uv
for Python dependency resolution.

**Why:**

- Multi-stage cuts image size; the runtime image is small.
- uv is meaningfully faster than pip/poetry for dependency
  resolution, especially in CI.

**Rejected:**

- **Distroless base.** We were on Alpine; the move was
  considered. Glibc-vs-musl complications with some
  numpy-derived libs kept us on debian-slim.
- **Single-binary build via Nuitka.** Tempting but mature
  Python single-binary tooling doesn't exist at the
  level of robustness we need.

---

## What's next

Part II starts the deep dive into the backend. Chapter 4 —
the FastAPI application.
