# 13. Shared components and the design system

`frontend/components/` houses the reusable UI building blocks.
We don't use a component library: the primitives are a handful
of files in `components/ui/` that we own, styled with Tailwind
over the theme tokens in `app/globals.css`. The rules every
screen follows (tabs vs accordions, how actions explain
themselves, states, motion, accessibility, layout) live in
`frontend/DESIGN.md`; this chapter is the map of where things are.

---

## Component categories

```
frontend/components/
├── ui/                  # primitives: Button, Dialog, TabBar, Accordion,
│                        #   ConfirmButton, LoadState, toast, useUnsavedGuard
├── layout/              # AppShell, Sidebar, Header, RightPanel, AssistantSheet
├── alerts/              # alert tile, alert chat + its right rail, investigation tabs
├── aiops/               # autonomy, investigations, maturity, runbook editor, verdicts
├── cards/               # Home tiles (summary, metrics, event feed, insights, …)
├── copilot/             # Markdown renderer + thinking dots for Assistant answers
├── managed-infra/       # Managed infra tabs (clusters, network & servers)
├── operations/          # Operations tabs (devices, drift, proposals, imports)
├── integrations/ gpu/ network/ onboarding/ proposals/ reports/
├── topology/ workflows/ brand/
├── command-bar.tsx      # the header's ask/search bar
├── greeting-hero.tsx, hero-briefing.tsx   # Home
```

The pattern: domain components compose primitive components.
Client helpers that aren't components live in `lib/`:
`errors.ts` (`errorText`), `timeout.ts` (`withTimeout`),
`use-automation-state.ts`, `theme.tsx`, `auth.tsx`, `api.ts`.

---

## Primitives (`components/ui/`)

| Component | File | Use for |
|---|---|---|
| `Button`, `buttonClass` | `button.tsx` | Every button. Variants `primary` / `secondary` / `danger` / `ghost` / `icon`; sizes `sm` / `md`. `pending` shows a spinner; `disabledReason` explains a disabled button (tooltip + screen reader). `variant="icon"` requires `aria-label` at the type level. |
| `Dialog` | `dialog.tsx` | Every overlay. `variant="modal"` (default), `"drawer"` (slides from the right) or `"sheet"` (rises from the bottom, for phones). Gives `role="dialog"`, a labelled title, Escape and backdrop close, a focus trap, focus return to the opener and scroll lock. It unmounts its children on close. |
| `TabBar`, `TabPanel`, `useUrlTab` | `tabs.tsx` | Page-level peer sections. `useUrlTab` keeps the active tab in `?tab=` so links and reloads land on it. A tab can carry a `badge` (accent = needs attention, muted = informational). Scrolls sideways on narrow screens. |
| `Accordion`, `AccordionItem` | `accordion.tsx` | Folded lists, one item open at a time. Folded rows show a one-line summary and a right-aligned `meta`. |
| `ConfirmButton` | `confirm-button.tsx` | One-click changes and removals. The first click asks in place: the `consequence` sentence plus a confirm/cancel pair, no modal. The question resets after 8 s. |
| `LoadState` | `load-state.tsx` | Loading, error, empty and "not set up yet" for a query. Never show "0" or "none" while loading. Errors render through `errorText()`. |
| `ToastProvider`, `useToast` | `toast.tsx` | Confirming what just happened (`toast.success("Runbook published", { action: { label: "View", … } })`) and announcing failures (`toast.error(errorText(err))`). `aria-live`, dismissible, paused on hover/focus, at most four on screen. |
| `useUnsavedGuard` | `use-unsaved-guard.ts` | Any editor or form: `useUnsavedGuard(draft !== saved)` makes the browser warn before reload or close. |

Two `lib/` helpers sit alongside them:

- `errorText(error)` (`lib/errors.ts`) is the only way an error
  reaches the screen. See Chapter 12.
- `useAutomationState()` (`lib/use-automation-state.ts`) returns
  `{ paused, tripped }` from the autonomy policy (kill switch and
  breakers whose cooldown hasn't ended). The header pill and the
  sidebar's Autonomy badge both read it, so they can't disagree.

---

## Confirmations

Native `confirm()` and `window.prompt()` are not used anywhere.
A change or removal confirms in place instead:

- **Most actions:** `ConfirmButton`, with `label` (the first
  click), `confirmLabel` (the second) and `consequence` (one
  sentence on what will happen). Example: Resolve on an alert,
  Resume automation on the Autonomy page.
- **Something that needs more than a sentence** (a warning to
  acknowledge, extra input): a `Dialog` whose footer has the
  primary action last. Example: the stressful-diagnostic
  acknowledgement in `app/ai-factory/components/diagnostics-panel.tsx`
  (`AckModal`).
- **Irreversible teardown:** type-the-name confirmation. Example:
  `DeleteEnvironmentDialog` in
  `components/managed-infra/network-view.tsx`.

Deletes are permanent (no model has a `deleted_at`), so they
confirm first; the toast's `action` slot is ready for an Undo
once soft delete exists.

---

## Layout components

The app shell (`components/layout/app-shell.tsx`, Chapter 10)
places these.

### `<Sidebar>`

`components/layout/sidebar.tsx`. The left navigation, grouped by
what you came to do. Home sits on top, then four labelled groups,
then a quieter bottom block:

```tsx
const HOME: Item = { href: "/", label: "Home", icon: Home };

const GROUPS = [
  { label: "Respond",        items: [Alerts, Investigations, Workflows] },
  { label: "Automate",       items: [Automations, Agents, Runbooks, "Autonomy policy"] },
  { label: "Infrastructure", items: ["Managed infra", "AI Factory", Operations, Workspace] },
  { label: "Insights",       items: [Dashboards, Reports, Maturity, Evals, "Audit log"] },
];

const BOTTOM = ["Usage & Pricing" (/billing), Settings, "Help & Feedback"];
```

(Abridged; the file has the `href`/`icon` for each.) There is no
"AI Ops" section any more: its pages joined these groups. Several
older routes are tabs elsewhere and are deliberately not entries:
Devices and Proposals are tabs in `/operations`; Integrations,
Clusters & observability, Network & servers and Onboarding are
tabs in `/managed-infra`. Their old routes still resolve.
Topology is not an entry either; it belongs to one NV-CM
environment and is reached from Managed infra → Network & servers
→ (environment) (Chapter 79). A new page joins an existing group;
the list must fit 1440×900 without scrolling.

**"Needs you" badges.** `useNavBadges()` in the same file puts a
badge on an entry only when a person can act on something:

| Entry | Badge | Source |
|---|---|---|
| Alerts | "N critical" (else "N warnings") | `api.alerts.list({ status: "open" })`, key `["alerts", "open-summary"]` (shared with the header and Assistant sheet) |
| Investigations | "N to grade" (muted) | `api.aiops.investigations.list({ days: 30, needs_verdict: true })` |
| Workflows | "N to approve" | `api.workflows.runs()` filtered to `status === "waiting_for_approval"`, key `["workflow-runs", "right-panel"]` (shared with the rail) |
| Autonomy policy | "Paused" or "N tripped" | `useAutomationState()` |

Each refetches every 60 s. Nothing shows until its query has
answered, and a query the user can't read (403) just means no
badge. Urgent badges use `motion-attention` (pops in, rings
twice, stops).

The active entry is matched with `usePathname()` (prefix match,
except `/`). From `md` up the sidebar is `sticky` at full
viewport height, so Settings stays reachable on a long page; the
nav list scrolls on short screens while the bottom block stays
pinned. Below `md` it is a drawer with a backdrop, closed by
Escape, the backdrop, the close button or navigating; a closed
drawer is `invisible` so its links leave the tab order.

### `<Header>`

`components/layout/header.tsx`. Sticky, 64px. Left to right: the
phone-only menu button (opens the sidebar drawer), an alert
status computed from open alerts ("2 critical alerts", "No open
alerts", never a static "all nominal"), the **Automation paused**
/ **N breakers tripped** pill when `useAutomationState()` says so
(links to `/autonomy`), the `CommandBar`, the theme toggle, an
alerts bell with a severity dot, and the account menu (name,
email, Sign out).

### `<RightPanel>` (the right rail)

`components/layout/right-panel.tsx`. Shown from `xl` up. Four
sections, each a pill in a slim 76px strip on the right edge:
**Assistant** (AI Assistant chat), **Alerts** (Notifications),
**Sessions** (Active sessions) and **Health** (System health).
Every section is collapsed by default, so the main column gets
the width; clicking a pill opens that section in a 340px column
beside the strip, and several can be open at once (they render
in rail order, not click order). The open set is saved in
`localStorage["daalu-right-rail-open"]` per browser; first visit
is fully collapsed, and the server render is always collapsed so
the main column never flashes narrow.

While Notifications is collapsed, its pill shows a severity dot
for open alerts (`OpenAlertDot`), so a firing critical is still
visible. The Assistant section is `CopilotBody`, a question/answer
list over `api.copilot.ask()` rendered with
`components/copilot/markdown.tsx`; it is not streamed.

Alert detail pages (`/alerts/<id>`) don't get this rail; they
render `components/alerts/alert-chat-rail.tsx`, a remediation chat
bound to that alert.

### `<AssistantSheet>`

`components/layout/assistant-sheet.tsx`. Below `xl`, where the
rail doesn't fit, a floating **Assistant** button (bottom right,
with the open-alert count) opens a `Dialog variant="sheet"` with
three tabs: Ask, Notifications and System health. It reuses the
rail's `CopilotBody`, `NotificationsBody` and `SystemHealthBody`.
The conversation is held in the sheet component, above the
`Dialog`, because `Dialog` unmounts its children on close; so it
survives closing the sheet and changing page, but not a reload.
The button is hidden on `/alerts/<id>` and `/workspace`
(`hidesAssistantButton` in the app shell).

---

## Tables

We don't use a table library. Tables are plain `<table>` markup
styled with Tailwind. Two rules from `DESIGN.md`:

- A table sits in a `relative overflow-x-auto` wrapper (never
  `overflow-hidden`), or becomes stacked cards below `md`, so
  action columns stay reachable on a phone. The wrapper must be
  `relative`, or an `sr-only` header escapes it and widens the
  page.
- A clickable row puts a real `<Link>` or `<button>` in the row;
  never `onClick` on a `<tr>` alone.

Filters that should survive a reload live in URL search params.

---

## Charts

**Recharts** for time series and bars: the Grafana-backed
dashboards (`app/dashboards/[uid]`), report dashboards, billing,
AI Factory and the maturity trend (`components/aiops/maturity-parts.tsx`).
The topology graph (`components/topology/FabricGraph.tsx`) is
hand-drawn SVG.

---

## Forms

Forms are controlled inputs with `useState`; there is no form
library. Every input has a visible label above it, never a
placeholder alone, and phone inputs are at least 16px so iOS
doesn't zoom on focus. The submit button says what happens
("Save new version", not "Submit") and uses `disabledReason` when
it can't work yet. Editors call `useUnsavedGuard(dirty)`.

---

## Color and theme

Semantic colour tokens are CSS variables in `app/globals.css`:
`--accent`, `--accent-glow`, `--text`, `--text-muted`,
`--bg-base`, `--bg-card`, `--bg-elevated`, `--line`, `--critical`,
`--warning`, `--info`. `:root` holds the dark values and
`html[data-theme="light"]` overrides them, so the theme is the
`data-theme` attribute on `<html>`. `tailwind.config.ts` maps
them to classes (`text-fg`, `bg-accent`, `bg-signal-critical`, …)
through `color-mix()`, so opacity modifiers like `bg-accent/10`
work. `bg-[color:var(--x)]/10` still compiles to nothing; use the
named token.

Attention is `--accent`, operator/manual is `--warning`, failure
is `--critical`. Never hard-code a hex value in a component.

The theme follows the OS until the user picks one with the header
toggle (saved as `localStorage["daalu-theme"]`), and it is applied
by an inline script in `app/layout.tsx` before first paint
(Chapter 10).

---

## Accessibility

The target is WCAG 2.2 AA. What's built in:

- Text tokens clear 4.5:1 on every surface in both themes; the
  sidebar badges use an opaque background so their contrast
  doesn't depend on the wallpaper behind them.
- A global `:focus-visible` ring (`app/globals.css`) and a "Skip
  to content" link in the app shell.
- `Dialog` handles role, label, Escape, focus trap and focus
  return; `Button variant="icon"` requires an `aria-label`.
- Results of async actions are announced: `useToast()` is
  `aria-live`, `LoadState` errors are `role="alert"`, and the
  header's automation pill is `role="status"`.
- Status is never colour alone: every badge and dot has a word.
- Tap targets are at least 24×24px (sidebar rows are 44px tall on
  a phone).

---

## Motion

Motion does one of three jobs or none: confirm an action
(`motion-press` on every `Button`, toasts, `motion-success`),
direct attention (`motion-attention` on "needs you" badges and the
automation pill, `motion-update`), or soften a transition
(`motion-enter` on route changes and loaded content,
`motion-dialog` / `motion-drawer` / `motion-sheet`). Durations
come from `--dur-fast`, `--dur` and `--dur-slow`, all under
400ms. A `prefers-reduced-motion: reduce` block at the bottom of
`globals.css` turns all of it off. Nothing loops for decoration.

---

## Checking a UI change

`npm run test:visual` runs the Playwright screenshot tests in
`e2e/visual.spec.ts` against fixtures in `e2e/fixtures` (key
screens, the grouped sidebar and the phone Assistant sheet); it
runs in CI. Update the baselines in `e2e/__screenshots__/` in the
same commit as an intended change. `scripts/ui-shots.mjs` takes
read-only screenshots against real data.

---

## Icons

We use **lucide-react** for icons. They're tree-shaken so the
bundle only includes the ones we import.

---

## What's next

Part IV starts the SoT deep dive. Chapter 14 — the
reconciler architecture.
