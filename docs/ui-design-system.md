# UI design system

The UI should feel like **Linear + Palantir + Vercel + modern SOC
dashboards + OpenAI-level polish** — alive, intelligent, premium,
AI-native. Not like a dashboard. Like an *intelligent operational
operating system*.

This document captures the tokens and component conventions so future
contributors stay consistent.

## Design language

| Quality | Source |
|---|---|
| Cinematic | Hero gradient + soft particles, glassmorphism on cards |
| Minimal | High signal-to-noise, ample padding, restraint on chrome |
| Alive | SSE-driven feeds, count-up metrics, animated event entries |
| Intelligent | AI summaries dominate over raw numbers |
| Premium | Subtle glow on accent surfaces, gradient borders on hero |

## Color system

Defined in `frontend/tailwind.config.ts`:

| Token | Hex | Use |
|---|---|---|
| `bg-base` | `#0B1020` | App background (NOT pure black) |
| `bg-secondary` | `#111827` | Secondary surface (sidebar, right panel) |
| `bg-card` | `#151C2F` | Default card surface |
| `bg-elevated` | `#1A2338` | Elevated panels (popovers, inner cards) |
| `accent.blue` | `#4F8CFF` | Primary — active states, key actions |
| `accent.cyan` | `#22D3EE` | Secondary — info severity, neutral signal |
| `accent.violet` | `#8B5CF6` | AI surfaces — gradient counterpart |
| `accent.emerald` | `#10B981` | Success — resolve, succeeded workflow |
| `accent.amber` | `#F59E0B` | Warning severity, "needs setup" |
| `accent.red` | `#EF4444` | Critical severity, failed workflow |
| `muted` | `#94A3B8` | Subdued text, metadata |

Glow utilities — apply sparingly: `.glow-blue`, `.glow-red`, plus the
custom `gradient-border` class on hero/feature cards.

## Typography

System uses `Inter` with `Geist`/`Satoshi` fallbacks. Feature flags:
`"ss01", "ss02", "cv01", "cv02"` (open-square shapes, alt 1/a).

| Element | Size | Weight |
|---|---|---|
| Hero summary | 32-36px | 600 |
| Section title | 20px | 600 |
| Card title | 15px | 500 |
| Body | 14px | 400 |
| Metadata / chips | 10-12px | 400, uppercase tracked |

## Motion

Animations are subtle and informational, never decorative:

| Animation | Where | Spec |
|---|---|---|
| `animate-fadeIn` | New cards on mount | 280ms, slight Y |
| `animate-shimmer` | AI surfaces, "thinking" states | 3.2s ease |
| `animate-pulseSoft` | Active indicators, critical dots | 2.8s |
| Event slide-in | Live feed entries | framer-motion, `y: -6 → 0` |

Avoid: heavy bounce, long durations, full-bleed motion.

## Components

The component tree is small on purpose — most of the UI is composed
out of a handful of cards.

### Layout

- `Sidebar` (`components/layout/sidebar.tsx`) — 240px expanded,
  glass + blur, active route gets a soft blue glow + animated dot
- `Header` (`components/layout/header.tsx`) — 72px sticky, contains
  the command bar
- `RightPanel` (`components/layout/right-panel.tsx`) — 360px,
  desktop-only. Hosts AI Assistant, Notifications, Active Workflows

### Cards (`components/cards/`)

| Component | When to use |
|---|---|
| `AIInsightCard` | An AI-derived observation. Has gradient border + shimmer |
| `MetricsCard` | A single metric with optional delta + trend line |
| `EventFeedCard` | The live event stream (SSE, animated entries) |
| `WorkflowStatusCard` | Aggregated running/waiting/succeeded/failed counts |
| `AIRecommendationCard` | One actionable AI suggestion + Approve/Dismiss |

### Marquee components

- `HeroBriefing` (`components/hero-briefing.tsx`) — the full-width AI
  briefing card on the home dashboard. Gradient border, hero gradient,
  metric chips, markdown body. Regeneratable in-place.
- `CommandBar` (`components/command-bar.tsx`) — ⌘K opens it. Routes
  through `/api/v1/copilot/ask`. Suggestions, history, glow on focus.

## Card anatomy

All cards follow the same shell:

```tsx
<div className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5
                hover:shadow-glow transition-shadow">
  <div className="flex items-center gap-2 text-[10px] uppercase
                  tracking-wider text-muted">
    {/* eyebrow / chips */}
  </div>
  <div className="text-base font-medium mt-1">
    {/* title */}
  </div>
  <div className="text-sm text-white/70 mt-2">
    {/* body */}
  </div>
  <div className="flex gap-2 mt-3">
    {/* actions */}
  </div>
</div>
```

Padding: `16-24px`. Radius: `20-24px`. Border: `border-line` (a 6%
white). Hover lifts the soft glow shadow.

## Buttons

Primary action — blue/violet gradient with the `shadow-glow` token:

```tsx
<button className="text-xs h-8 px-3 rounded-lg
                   bg-gradient-to-r from-accent-blue to-accent-violet
                   shadow-glow text-white">…</button>
```

Secondary — transparent dark glass:

```tsx
<button className="text-xs h-8 px-3 rounded-lg border border-line
                   hover:bg-white/5">…</button>
```

Danger — emerald accent for "resolve", red shadow for destructive
operations.

## Mobile

Phase-1 ships a responsive home page (the sidebar hides under `md`)
but the right panel is desktop-only. The Phase-2 mobile pass should:

- collapse the sidebar to a bottom nav
- float the AI assistant as a FAB
- enable swipe between Operations module tabs

## Charts

Recharts, minimal styling. Single accent stroke, no grid, no axes for
mini-trend lines. Use `ResponsiveContainer` so cards stay fluid.

## Don't do

- emoji-laden UI (the system already feels alive)
- pure black backgrounds (use the layered dark surfaces)
- bright fills that compete with the AI accent colors
- toast spam — important things become Alerts; transient things stay
  in the live feed
- full-bleed motion or autoplaying videos
