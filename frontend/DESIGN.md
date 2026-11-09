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
- Status copy is computed from data ("2 critical alerts", "No open alerts"), never a
  static "All systems nominal". Two numbers about the same thing on one screen come from
  the same source.
- No developer text in the product: no mock/adapter internals, feature-flag names,
  raw backend reason strings, private-repo links, or stale model names.

## 4c. Accessibility (WCAG 2.2 AA)

- Text colours clear 4.5:1 on every surface in both themes (the tokens are tuned for
  it; don't fade them further with `/60`-style opacity on body text). Nothing smaller
  than 12px for anything a user must read; 11px only for uppercase micro-labels.
- Every control has a name: icon-only buttons get `aria-label` (use `Button
  variant="icon"`, which requires it); every input has a visible label above it, never
  a placeholder alone.
- Everything works by keyboard. Clickable rows put a real `<Link>` or `<button>` in the
  row; never `onClick` on a `<div>`/`<tr>` alone. The global `:focus-visible` ring is
  the focus style; don't remove outlines without a replacement.
- Overlays use `Dialog` (role, label, Escape, focus trap, focus return).
- Status is never colour alone: pair the dot/colour with a word.
- Results of async actions are announced: `useToast()` (aria-live) or `role="status"`.
- Tap targets are at least 24×24px (36px for primary touch actions).

## 4d. Motion with a purpose

Motion is allowed only when it does one of three jobs. If you can't name the job in a
sentence, don't animate.

| Job | Use | Where |
|---|---|---|
| Confirm an action | `motion-press` (buttons), `useToast()`, `motion-success` tick | every button via `Button`; after a mutation succeeds |
| Direct attention | `motion-attention` (pops in, rings twice, stops), `motion-update` (a changed number tints once) | "needs you" badges, the header automation-paused pill, a metric that just changed, a new row from a run |
| Soften a transition | `motion-enter` (content arriving), `.collapse-grid` (accordion height), `motion-dialog`/`motion-drawer`/`motion-sheet` | loaded content replacing `LoadState`, tab panels, route changes (app shell does this), accordions, overlays |

- Nothing loops for decoration. Loops are only for "in progress" (spinners, a
  "listening"/"loading" shimmer) and stop when the work stops.
- Durations come from `--dur-fast` (120ms), `--dur` (200ms), `--dur-slow` (320ms)
  with `--ease-out`. Under 400ms, always.
- `prefers-reduced-motion` turns it all off (globals.css); don't add motion that
  bypasses it (no JS-driven animation loops).

## 4e. Errors and copy

- Errors shown to people go through `errorText()` (`lib/errors.ts`): FastAPI's
  `detail` when it's a sentence, otherwise a plain sentence by status. Never
  `String(err)`, `err.message` or a raw body.
- A "not set up yet" answer (e.g. 409 "no nautobot integration") is not an error:
  `LoadState notSetUp={…}` shows a neutral next step with a link.
- A button that can't work right now is disabled and says why (`disabledReason`).
- Buttons say what happens ("Authorize this terminal", "Switch to Hybrid · $99/mo"),
  never "Submit"/"OK"; error copy says what to do next.

## 5. Layout

- The app shell's `<main>` has no height of its own. A page that needs an inner scroll
  area sizes itself to the viewport (`h-[calc(100dvh-4rem)]`, 4rem = header), not
  `h-full`.
- Headers and action rows `flex-wrap`, so nothing overflows at 390px.
- Tables sit in `overflow-x-auto` wrappers (never `overflow-hidden`), or become stacked
  cards below `md`, so action columns stay reachable on a phone.
- Those wrappers are also `relative`. An `sr-only` table header is absolutely positioned, so
  without it the label escapes the scroller and widens the whole page.
- Colours with opacity use the named tokens (`bg-signal-critical/10`, `text-fg/80`);
  `bg-[color:var(--x)]/10` compiles to nothing.
- Tab bars scroll sideways on narrow screens; the active tab scrolls into view.

## 5b. Navigation

- The sidebar is grouped by what you came to do: **Respond**, **Automate**,
  **Infrastructure**, **Insights**. A new page joins a group; it doesn't get a new one
  without a mockup. The list fits 1440×900 without scrolling, so count rows before adding.
- An entry's badge is something a person can act on, in words ("1 to approve", "Paused"),
  from the same query as the page it points to. Nothing shows until the query has answered.
- Below `xl` the right rail becomes the Assistant button and bottom sheet
  (`AssistantSheet`). State that must outlive the sheet is held above it, because `Dialog`
  unmounts its children on close. Full-screen tools hide the button (`hidesAssistantButton`).
- Phone inputs are at least 16px, or iOS zooms the page when they take focus. Sheets don't
  focus a field on open, so the keyboard doesn't cover what the person opened it to read.

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
| `LoadState` | `components/ui/load-state.tsx` | Loading / error / empty / not-set-up for a query; never show "0" or "none" while loading |
| `Button`, `buttonClass` | `components/ui/button.tsx` | Every button: primary / secondary / danger / ghost / icon, press feedback, `disabledReason` |
| `Dialog` | `components/ui/dialog.tsx` | Modals, side drawers and phone bottom sheets (`variant="sheet"`): labelled, Escape, focus trap and return, scroll lock, motion |
| `useToast` | `components/ui/toast.tsx` | Confirming what just happened (with an optional Undo/View), announcing failures |
| `errorText` | `lib/errors.ts` | The only way an error reaches the screen |
| `useUnsavedGuard` | `components/ui/use-unsaved-guard.ts` | Any editor or form: warns before reload/close while there are unsaved edits |
| `useAutomationState` | `lib/use-automation-state.ts` | Whether the AI can act (paused, breakers tripped), so every surface agrees |
