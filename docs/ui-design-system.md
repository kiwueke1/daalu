# UI design system

The rules for building a Daalu screen live in
[`frontend/DESIGN.md`](../frontend/DESIGN.md), next to the code, and that file wins over anything
here. This page is the map. The history of the last full pass, including what's still open, is in
[`design/ux-principles.md`](design/ux-principles.md).

## Principles

- **One thing at a time.** Tabs for peer sections, one-open accordions for detail.
- **Say what matters when folded,** and show what needs someone from anywhere.
- **Actions explain themselves:** why, what will happen, who acts, before the button.
- **Accessible by default:** WCAG 2.2 AA in both themes, full keyboard use.
- **Motion has a job:** confirm an action, direct attention, or soften a transition. Nothing loops
  for decoration, and the OS reduced-motion setting turns it all off.

## Where things are

| What | Where |
|---|---|
| Colour, surface and motion tokens, both themes | `frontend/app/globals.css` |
| Tailwind names for those tokens | `frontend/tailwind.config.ts` |
| Shared components (`Button`, `Dialog`, `TabBar`, `Accordion`, `LoadState`, …) | `frontend/components/ui/`, listed in `DESIGN.md` |
| App frame: sidebar, header, right rail, phone Assistant sheet | `frontend/components/layout/` |
| Charts | Recharts |
| Screenshot tooling | `frontend/scripts/ui-shots.mjs` |
| Screenshot regression tests | `frontend/e2e/visual.spec.ts` |
| How to build and verify a UI change | `.claude/skills/daalu-ui/` |

Never hard-code a colour in a component: both themes define every token, and the tokens are tuned to
clear contrast on every surface.
