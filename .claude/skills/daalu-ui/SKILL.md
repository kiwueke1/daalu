---
name: daalu-ui
description: Build or change any Daalu web UI screen (frontend/ — pages, components, layout, copy on buttons or cards) to the product's design bar, and prove it by screenshot before calling it done. Use for every frontend UI change, a new page or panel, a redesign, "the page is cluttered", "make this look better", a UI audit, or updating screenshot baselines. Triggers "UI", "page", "screen", "layout", "tabs", "design", "frontend", "cluttered", "mockup", "screenshot test".
---

# Daalu UI

The design bar lives in **`frontend/DESIGN.md`**. Read it first, every time; it's short.
This skill is how you meet it.

## 0. Before building: mockup first for anything new

A **new screen**, or a change to how a screen is **organised** (new tabs, moved
sections, a new flow), gets a mockup the user approves before code:

- Build a static HTML mockup with realistic content (real alert names, real numbers,
  not lorem ipsum), in both themes, and publish it as an Artifact for review.
- Show the folded state (what the user sees first) and one opened state.
- Only start coding after the user picks or approves it. Small fixes (spacing, a label,
  a bug) skip this step.

## 1. Build with the shared pieces

- Sections the user reads one at a time → `TabBar` + `useUrlTab`
  (`frontend/components/ui/tabs.tsx`).
- Lists of detail → `Accordion` / `AccordionItem`
  (`frontend/components/ui/accordion.tsx`), one open at a time.
- Colours only from the CSS variables in `frontend/app/globals.css` (both themes define
  them). No hex values in components.
- Every action that changes something says why and what happens, before the button.
- If you need a pattern that isn't in `components/ui/` and will be used twice, add it
  there and list it in `DESIGN.md`.

## 2. Prove it by looking at it

Type-check and lint are not enough. Screenshot the screens you touched:

```bash
cd frontend
npx next dev -p 3107            # in the background
# Real data, read-only (every non-GET is refused in the browser):
export DAALU_UI_TOKEN=<session JWT>   # see "Token" below
node scripts/ui-shots.mjs --routes=/alerts,/alerts/<id> --out=/tmp/ui-shots
#   --tab="Remediation workflow"  click a tab first
#   --themes=dark,light --viewports=desktop,mobile (the default is all four)
#   --record=<dir>   also save the API responses (for fixtures)
```

Then **open every PNG** and check it against `DESIGN.md`, and read
`/tmp/ui-shots/report.json`: `overflowPx > 0` means the page is wider than the screen
(fix it), `errors` lists console and page errors (fix them). Check 1440 and 390 wide,
dark and light. Fix what you find and shoot again.

**Token** (for `--source=prod`, the default): mint a session JWT for your own user in
the prod API pod (`KUBECONFIG=$KUBECONFIG`, namespace `daalu-automation`,
`deploy/daalu-api`, container `api`) with
`daalu_automation.core.auth.issue_token(user_id=…, tenant_id=…, is_admin=…)`. Keep it
out of the repo. Without a token, use `--source=fixtures --fixtures=e2e/fixtures`.

For a sweep of every screen: `--routes-file=scripts/ui-routes.txt` (update the ids in it
when they go stale), then review the screenshots in parallel batches.

## 3. Keep the screenshot tests green

`frontend/e2e/visual.spec.ts` guards the key screens with pixel baselines in
`e2e/__screenshots__`, rendered in the Playwright container so they match CI.

```bash
cd frontend
npm run test:visual           # compare (needs docker)
npm run test:visual:update    # after an INTENDED change; review the PNG diffs
```

- A changed screen that's in the suite → update the baselines in the same commit and
  look at the new PNGs before committing.
- A new important screen → add a test (fixtures from `ui-shots --record`, scrubbed of
  personal data; freeze the clock with `page.clock.setFixedTime`).
- The mobile tests also assert no horizontal overflow.

## 4. Close the loop

- A new rule or pattern you had to invent → add it to `DESIGN.md`.
- Episode guides under `docs/content/` that describe a screen you changed → update the
  on-screen steps and labels.

## Gotchas

- `<main>` in the app shell has no height; a page needing its own scroll area sizes to
  `h-[calc(100dvh-4rem)]`. Negative margins that cancel `<main>`'s padding must match it
  per breakpoint (`-mx-4 md:-mx-6 lg:-mx-10`).
- Theme comes from `localStorage["daalu-theme"]`; the screenshot tools set it.
- `useSearchParams` (hence `useUrlTab`) needs a `<Suspense>` boundary above it.
- Models write markdown loosely (bare headings, commands at column 0); parse what the
  agent writes defensively rather than trusting the prompt's format.
