# 11. Page-by-page tour

Every page in the frontend, what it does, what it fetches,
and where the corresponding backend route lives.

This is a reference. Skip to the page you're working on.

The sidebar (`components/layout/sidebar.tsx`, Chapter 13) groups
these routes by task:

| Group | Entries |
|---|---|
| (top) | Home `/` |
| Respond | Alerts `/alerts`, Investigations `/investigations`, Workflows `/workflows` |
| Automate | Automations `/automations`, Agents `/agents`, Runbooks `/runbooks`, Autonomy policy `/autonomy` |
| Infrastructure | Managed infra `/managed-infra`, AI Factory `/ai-factory`, Operations `/operations`, Workspace `/workspace` |
| Insights | Dashboards `/dashboards`, Reports `/reports`, Maturity `/maturity`, Evals `/evals`, Audit log `/audit` |
| (bottom) | Usage & Pricing `/billing`, Settings `/settings`, Help & Feedback `/help` |

Routes that aren't entries are reached from a page: Devices and
Proposals are tabs in `/operations`; Integrations, clusters and
Network & servers are tabs in `/managed-infra`; `/network/topology`
opens from an environment card. The old standalone routes
(`/devices`, `/proposals`, `/clusters`, `/network`, `/integrations`)
redirect to those tabs.

---

## `/` — Home

**File:** `frontend/app/page.tsx`.

Server-side fetch on initial render:
- `GET /api/v1/briefing/today` → daily briefing.
- `GET /api/v1/notifications` → personal notifications.
- `GET /api/v1/integrations/health` → system health %.

Client components:
- `<StatusPill>` — polls `/api/v1/alerts/summary`.
- `<AssistantCard>` — inline chat input.
- `<NotificationsFeed>` — React Query for the notifications.
- `<QuickActions>` — static; just links.

---

## `/login` — Login

**File:** `frontend/app/login/page.tsx`.

Client component (no server-side fetch).

POSTs to `/api/v1/auth/login`. On success, the backend sets
the `daalu_session` cookie and redirects to home.

Form fields: email, password. Optional "Remember me" extends
JWT TTL.

---

## `/onboarding` — Onboarding wizard

**File:** `frontend/app/onboarding/page.tsx`.

A multi-step wizard for first-time tenants:
1. Tenant name.
2. Time zone.
3. First integration choice.
4. First teammate invite.

POSTs to `/api/v1/onboarding/step` per step. The state machine
on the backend lives in `api/routers/onboarding.py`.

---

## `/managed-infra` — Managed Infrastructure

**File:** `frontend/app/managed-infra/page.tsx`.

A `TabBar` with the active tab in `?tab=`:

- Clusters & observability (`?tab=clusters`)
- Network & servers (`?tab=network`) — NV-CM environment cards,
  their consoles, observability and the Topology link
- Onboarding (`?tab=onboarding`)
- Integrations (`?tab=integrations`, `IntegrationsView` from
  `components/integrations/integrations-view.tsx`)

`/clusters`, `/network` and `/integrations` redirect here.

---

## `/operations` — Operations

**File:** `frontend/app/operations/page.tsx`.

Tabs (`?tab=`), each a view in `components/operations/`:
- Overview (`OverviewView`)
- Devices (`DevicesView`)
- Drift (`DriftView`)
- Proposals (`ProposalsView`)
- Bulk import (`ImportView`)
- Routine runs (`RoutineRunsView`)

`/devices` and `/proposals` redirect to their tabs.

---

## `/operations/devices/[id]` — Device detail

**File:** `frontend/app/devices/[id]/page.tsx`.

Tabs:
- Identity
- Live config (with diff)
- History
- Run diagnostics

Backend: `/api/v1/sot/devices/<id>`.

---

## `/clusters` — Federated clusters

**File:** `frontend/app/clusters/page.tsx`.

Lists `cluster_tunnels` rows. Per-row: status, tunnel IP,
last handshake, edge version. **Add cluster** button mints a
bootstrap invite.

---

## `/clusters/[id]` — Cluster detail

**File:** `frontend/app/clusters/[id]/page.tsx`.

Tabs:
- Overview (node count, pod count)
- Nodes
- Recent rollouts
- Reconciliation
- Tunnel diagnostics
- Run kubectl

Backend: `/api/v1/clusters/<id>/*` — each tab to its own route.

---

## `/agents` — Agents

**File:** `frontend/app/agents/page.tsx`.

Lists shipped and custom agents with status, last run, p50
latency, success rate.

Click → `/agents/[id]` for the per-agent detail (run history,
configuration, logs, test now).

---

## `/alerts` — Alerts

**File:** `frontend/app/alerts/page.tsx`.

The big list view. Filters in the top bar (severity, status,
source, label search).

`GET /api/v1/alerts` with query params.

Each row links to `/alerts/[id]`.

---

## `/alerts/[id]` — Alert detail

**File:** `frontend/app/alerts/[id]/page.tsx`.

Sections:
- Header (severity, title, status, buttons)
- Auto-generated explanation
- Suggested actions
- Timeline
- Comments
- Labels
- Related

This page replaces the global right rail with its own
remediation chat rail (`components/alerts/alert-chat-rail.tsx`),
bound to this alert; the app shell skips `RightPanel` and the
phone Assistant button on `/alerts/<id>` (`ownsRightRail`).

---

## `/proposals` — Change proposals

**File:** `frontend/app/proposals/page.tsx`.

Same shape as Alerts — list view with filters, per-row
detail.

`GET /api/v1/proposals`.

---

## `/proposals/[id]` — Proposal detail

**File:** `frontend/app/proposals/[id]/page.tsx`.

Sections:
- Summary
- Diff (with side-by-side syntax highlighting for device
  configs)
- Plan (the tool calls)
- Approvals
- Comments
- Audit log

Approve and Deny are buttons that POST to the backend.

---

## `/automations` — Workflows

**File:** `frontend/app/automations/page.tsx`.

Lists workflows. Click → workflow detail with the visual
authoring canvas.

`GET /api/v1/workflows`.

---

## `/billing` — Billing and spend

**File:** `frontend/app/billing/page.tsx`.

Sections:
- Summary band
- Local GPU + LLM router
- Cloud spend
- Daalu subscription
- Spend breakdown

Backend: `/api/v1/billing/*`.

---

## `/reports` — Reports

**File:** `frontend/app/reports/page.tsx`.

List of saved queries and dashboards. Click to view or edit.

`GET /api/v1/reports`.

---

## `/settings` — Settings

**File:** `frontend/app/settings/page.tsx`.

Secondary nav inside the page:
- Profile
- Preferences
- API tokens
- Team (admin)
- Tenant (admin)
- Integrations (admin)
- Alert rules (admin)
- Notifications (admin)
- Briefings (admin)
- Reconciliation (admin)
- LLM router (admin)
- Audit log (admin)

Each sub-section is its own component within the page.

---

## `/help` — Help & Feedback

**File:** `frontend/app/help/page.tsx`.

Shows build info from `GET /version` (unauthenticated
endpoint), documentation links, the feedback form, support
contacts, the keyboard shortcuts cheat sheet, the changelog,
and the hardware compatibility matrix.

The feedback form POSTs to `/api/v1/feedback`.

---

## Pages added June–July 2026 (index)

Newer surfaces, each documented in its owning chapter — listed here so the
tour stays a complete map:

| Route | File | What / chapter |
|---|---|---|
| `/clusters/[slug]` | `frontend/app/clusters/[slug]/page.tsx` | Per-cluster console: overview, health rollup, read-only kubectl runner (ch. 78) |
| `/observability/[provider]` | `frontend/app/observability/[provider]/page.tsx` | Prometheus/Thanos/Loki detail consoles (ch. 78) |
| `/dashboards`, `/dashboards/[uid]` | `frontend/app/dashboards/` | Grafana-backed dashboards rendered natively + AI-create (ch. 40) |
| `/network/topology` | `frontend/app/network/topology/page.tsx` | Live fabric graph + node drawer + SSH terminal (ch. 79) |
| `/managed-infra` (tabbed) | `frontend/app/managed-infra/` | Now hosts the Integrations tab (`?tab=`-synced; `/integrations` redirects) and the NV-CM environment tiles + onboarding panel (ch. 23 §23.9, ch. 15) |
| `/signup`, `/verify-email` | `frontend/app/signup/`, `verify-email/` | Self-signup flow, flag-gated (ch. 5) |
| `/accept-invite/[token]` | `frontend/app/accept-invite/` | Public-route **prefix** (PR #88) — the authed shell must not wrap it; uses `useParams` (Next 14, not the Next-15 `use(params)` idiom — PR #90) |
| `/cli/activate` | `frontend/app/cli/activate/page.tsx` | CLI device-flow approval page (ch. 77) |
| `/ai-factory` (reworked) | `frontend/app/ai-factory/` | GPU list + per-card detail views + whole-floor test routes (ch. 80) |
| `/workspace` (+ terminal card) | `frontend/app/workspace/page.tsx` | "Use Daalu from your terminal" express-login card (ch. 77) |

One styling landmine worth recording here because it *will* recur
(PR #114): an opacity modifier on an arbitrary colour,
`bg-[color:var(--accent)]/10`, compiles to **no CSS at all**, because
Tailwind can only apply `/NN` to a colour it can mix. Buttons styled
that way render with no background until `:hover` applies the
unmodified class. The named tokens in `tailwind.config.ts` (`bg-accent`,
`bg-signal-critical`, `text-fg`, …) are declared through a `color-mix()`
helper, so `bg-accent/10` and `bg-accent-emerald/90` do work now; use
them (`frontend/DESIGN.md` §5).

---

## What's next

Chapter 12 — the API client (`lib/api.ts`).
