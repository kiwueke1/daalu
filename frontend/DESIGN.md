# Daalu UI design rules

The rules every screen follows. Started 2026-09-28 from the alert-page redesign; the
screen-by-screen audit will extend it. When a rule and a screen disagree, fix the screen
or change the rule here — don't leave both.

## 1. One thing on screen at a time

- **Peer sections → tabs.** A page with several sections the user reads one at a time
  (Root cause / Background / Workflow / Evidence) uses `TabBar` from
  `components/ui/tabs.tsx`. The active tab lives in `?tab=` (`useUrlTab`) so links and
  reloads land on it.
- **Lists of detail → accordion, one open at a time.** Steps, evidence groups, past
  actions: `Accordion` from `components/ui/accordion.tsx`. Folded rows show a one-line
  summary and a right-aligned meta (count, status).
- **Supporting data → a Details tab or a folded `<details>`**, never stacked under the
  main content (occurrence history, raw payloads, metadata).
- Never two panels that each expand to full width on the same screen.

## 2. Say what matters in the folded state

- A folded row must be enough on its own: the sentence, not a label. "Roll back
  chat-llm to revision 3, which set GPU_MEM_UTIL=0.10", not "Step 1".
- Something that needs the user shows from anywhere: a badge on its tab
  ("1 to approve"), in the accent colour. Informational counts use the muted badge.

## 3. Actions explain themselves

- Every action that changes something says **why** and **what will happen**, in one
  plain sentence, before any button. The agent supplies it as `reason`.
- The button says the verb and the consequence: "Approve & run", not "OK".
- Commands and raw payloads are one click away ("Show exact command"), not on the
  card.
- Say who acts: **System** (Daalu runs it on approval) vs **You** (the operator does it).

## 4. Tokens, not colours

- Colours come from CSS variables in `app/globals.css`: `--accent`, `--accent-glow`,
  `--text`, `--text-muted`, `--bg-base`, `--bg-card`, `--bg-elevated`, `--line`,
  `--critical`, `--warning`, `--info`. Both themes define all of them; never hard-code a
  hex value in a component.
- Attention = `--accent`, operator/manual = `--warning`, failure = `--critical`.

## 4b. States

- Never render an empty state or a zero metric while the query is still loading or
  has failed: wrap it in `LoadState` (or check `isLoading`/`isError` first).
- An empty state says where the data comes from or what to do next, with a link.
- Status copy is computed from data ("2 critical alerts"), never a static "All systems
  nominal".
- No developer text in the product: no mock/adapter internals, feature-flag names,
  raw backend reason strings, private-repo links, or stale model names.

## 5. Layout

- The app shell's `<main>` has no height of its own. A page that needs an inner scroll
  area sizes itself to the viewport (`h-[calc(100dvh-4rem)]`, 4rem = header), not
  `h-full`.
- Headers and action rows `flex-wrap`, so nothing overflows at 390px.
- Tables sit in `overflow-x-auto` wrappers (never `overflow-hidden`), or become stacked
  cards below `md`, so action columns stay reachable on a phone.
- Colours with opacity use the named tokens (`bg-signal-critical/10`, `text-fg/80`);
  `bg-[color:var(--x)]/10` compiles to nothing.
- Tab bars scroll sideways on narrow screens; the active tab scrolls into view.

## 6. New screens start as a mockup

A new screen, or a change to how one is organised (new tabs, moved sections, a new
flow), starts as a static mockup with realistic content in both themes, and is built
only after it's approved. Small fixes skip this. The `daalu-ui` skill
(`.claude/skills/daalu-ui/`) walks through it.

## 7. Done means seen

A UI change is done when it has been looked at, not just type-checked: desktop (1440)
and mobile (390), dark and light, with realistic data. Screenshot it with Playwright
with `scripts/ui-shots.mjs` (real data, read-only) and check it against this file.
The key screens are guarded by screenshot tests (`e2e/visual.spec.ts`,
`npm run test:visual`, run in CI); update their baselines in the same commit as an
intended change.

## Shared components

| Component | File | Use for |
|---|---|---|
| `TabBar`, `useUrlTab` | `components/ui/tabs.tsx` | Page-level sections |
| `Accordion`, `AccordionItem` | `components/ui/accordion.tsx` | Folded lists, one open at a time |
| `ConfirmButton` | `components/ui/confirm-button.tsx` | One-click changes/removals: asks in place, says the consequence |
| `LoadState` | `components/ui/load-state.tsx` | Loading / error / empty for a query; never show "0" or "none" while loading |

Pages still hand-rolling the same underline tab bar (`/managed-infra`, `/operations`,
`/settings`) should move to `TabBar` when next touched.
