# UX principles pass (November 2026)

What changed when the whole Daalu UI was audited against two external UX guides (seven
fundamentals, and a set of 2026 practical principles), why, how it was checked, and what's
left. The rules that came out of it live in [`frontend/DESIGN.md`](../../frontend/DESIGN.md);
read that for how to build a screen, and this for the history.

## What changed

**Foundations**

- Light-theme colours retuned to clear WCAG AA (4.5:1) on every surface.
- Global focus ring, skip link, and the OS reduced-motion setting respected throughout.
- The theme follows the OS by default and applies before first paint, so there's no dark flash.
- New shared `Button`, `Dialog` and `useToast`; errors reach the screen only through
  `errorText()`, as plain sentences.
- "Automation paused" and tripped breakers show in the header on every page.

**Every page (48)**

- About 20 one-click state changes now confirm in place and state the consequence. All native
  `confirm()` and `window.prompt` calls are gone.
- Editors warn before unsaved changes are lost (`useUnsavedGuard`).
- Raw `409 {"detail":…}` boxes became neutral "not set up yet" states with a next step.
- Pages that spun forever now time out to a clear unreachable state.
- Phone layouts keep their actions visible; wide tables become stacked cards.
- Motion is limited to three jobs: confirming an action, pointing at what needs you, softening
  a transition. Decorative loops are gone.

**Navigation**

- The sidebar went from 20 equal-weight pills, with Alerts sixth, to four groups by task:
  Respond, Automate, Infrastructure, Insights. Home stays on top; Usage & Pricing, Settings
  and Help sit at the bottom.
- Entries show what needs someone from any page: critical alerts, runs to approve,
  investigations to grade, automation paused.
- The sidebar stays on screen while a page scrolls, so Settings is always reachable.
- Below the `xl` breakpoint, where the right rail doesn't fit, an Assistant button opens a
  bottom sheet with Ask, Notifications and System health. The conversation survives closing
  the sheet and changing page, but not a reload.

## Decisions

- **Page names and routes stayed.** The mockup also proposed renames
  (Workflows → Remediations, Managed infra → Clusters & cloud, Operations → Network & devices)
  and merging Dashboards with Reports. Those are deferred: they would change the public handbook, while the grouping and badges solve the reported problems
  on their own.
- **The "AI Ops" sidebar section is gone.** Its pages joined the task groups, so handbook paths now
  read, for example, *Insights → Maturity*.
- **Rows have no per-row chip.** Twenty frosted chips were the visual noise the regroup set out
  to remove. A stronger tint over the circuit wallpaper keeps labels readable instead, and
  badges use an opaque background so their contrast doesn't depend on what's behind them.
- **Home tiles keep their own time windows.** "Open now" and the briefing's 24-hour window
  differ, so each tile says which window it shows rather than adopting the briefing's figures.

## How it was checked

- Three full sweeps of every screen in dark and light, desktop and phone (164 screenshots each),
  with fresh reviewers reading before/after pairs twice. They caught one layout regression and
  a list of smaller issues, all fixed.
- The navigation change, added afterwards, was checked with its own targeted screenshots in both
  themes at desktop and phone width, plus an overflow and page-error sweep of every route at 390,
  768, 1024 and 1440 wide, using test fixtures rather than live data. That sweep found one
  overflow, now fixed, from a hidden table header escaping its scroller. `/clusters/<slug>`,
  `/network/topology` and `/onboarding` have no fixtures, so they crash in the harness before
  rendering and weren't exercised against the new layout. Badge contrast was calculated for
  each tone in both themes (lowest 4.93:1).
- `tsc`, `next lint` and `next build` pass. `npm run test:visual` guards the key screens,
  including the phone Assistant sheet and the grouped sidebar.

## Follow-ups

None of these are started. Each needs backend work, so they were kept out of this frontend-only
change.

| Item | What it needs | Today |
|---|---|---|
| **Stop/Cancel** for running workflows, evals and diagnostics | Cancel endpoints in the API, and runners that stop at a safe point and record a `cancelled` state, then a `ConfirmButton` on each running row | The API has no cancel routes; a started run can only be waited out |
| **Undo after delete** | Soft delete: a `deleted_at` column via migration, delete routes that set it, list queries that exclude it, a purge job, then the toast's existing Undo action | Deletes are permanent (no model has `deleted_at`), so they confirm first |
| **Measuring whether this helped** (the second guide's last principle) | A privacy-reviewed event capture, a small set of task-level measures (for example, time from alert to approval), and somewhere to read them | The frontend has no product analytics |

Also open, with no backend work:

- **Handbook screenshots** still show the old sidebar until they are retaken.
- **The deferred renames** above, if still wanted. They need the handbook, guides and redirects
  changed together.
