# 13. Shared components and the design system

`frontend/components/` houses the reusable UI building blocks.
We use Radix UI primitives + Tailwind for styling, layered
with our own opinionated set of components.

---

## Component categories

```
frontend/components/
├── ui/                  # primitive components (Button, Input, Card, …)
├── layout/              # Sidebar, TopBar, AssistantPanel
├── alerts/              # alert-specific components
├── proposals/           # proposal-specific components
├── billing/             # billing visualizations
├── assistant/           # assistant chat UI
├── forms/               # form helpers, fields, validators
└── theme/               # ThemeProvider, dark mode toggle
```

The pattern: domain components compose primitive components.

---

## Primitives (`components/ui/`)

Following the **shadcn/ui** approach: we vendor the
components rather than installing them. Each primitive lives
as a .tsx file we own, can edit, and don't have to upgrade.

Examples:
- `<Button>` — variants (primary, secondary, ghost, destructive),
  sizes (sm, md, lg, icon).
- `<Input>`, `<Textarea>`, `<Select>`, `<Checkbox>`, `<Switch>`.
- `<Card>` — wrapper for panel-like content.
- `<Dialog>` — modal.
- `<Sheet>` — side panel that slides in.
- `<Popover>`, `<Tooltip>`, `<HoverCard>`.
- `<Table>` — semantic table styling, no built-in pagination.
- `<Tabs>` — uses Radix Tabs under the hood.
- `<Badge>` — for status pills.

Each is a single-file component, typed in TypeScript, using
`class-variance-authority` (CVA) for variant-based class
composition.

---

## Layout components

### `<Sidebar>`

`components/layout/sidebar.tsx`. The left navigation. Sourced
from a static `NAV` array of items:

```tsx
const NAV = [
  { href: "/", label: "Home", icon: Home },
  { href: "/integrations", label: "Managed Infra", icon: Cloud },
  { href: "/operations", label: "Operations", icon: Settings },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/alerts", label: "Alerts", icon: AlertTriangle },
  { href: "/automations", label: "Automations", icon: Workflow },
  { href: "/reports", label: "Reports", icon: BarChart },
  { href: "/billing", label: "Billing", icon: CreditCard },
  { href: "/devices", label: "Devices", icon: Server },
  { href: "/clusters", label: "Clusters", icon: Boxes },
  { href: "/proposals", label: "Proposals", icon: GitPullRequest },
];

const BOTTOM = [
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/help", label: "Help & Feedback", icon: HelpCircle },
];
```

The `usePathname()` hook highlights the active item. Each
link is a Next.js `<Link>` for client-side navigation.

### `<TopBar>`

Search input, notifications bell (popover with feed), profile
menu (dropdown with Settings, Help, Logout). Sticky to the top.

### `<AssistantPanel>`

The right-side AI Assistant panel. Always rendered (in the
authed layout) but collapsed to a thin strip by default.

Has its own state machine for the chat:
- `idle` — waiting for input.
- `streaming` — receiving tokens from the SSE stream.
- `tool-running` — between streaming chunks, a tool call is
  executing.
- `error` — terminal state for the current turn.

State is in a Zustand store at `components/assistant/store.ts`
because multiple components (panel header, message thread,
input) need to read and write it.

---

## Tables

We don't use a heavy table library (TanStack Table is great
but overkill for our needs). Tables are hand-rolled with the
`<Table>` primitive:

```tsx
<Table>
  <thead>
    <tr>
      <th>Severity</th>
      <th>Title</th>
      <th>Source</th>
      <th>First seen</th>
    </tr>
  </thead>
  <tbody>
    {alerts.map((a) => (
      <tr key={a.id} onClick={() => router.push(`/alerts/${a.id}`)}>
        <td><Badge variant={a.severity}>{a.severity}</Badge></td>
        <td>{a.title}</td>
        <td>{a.source}</td>
        <td>{relativeTime(a.started_at)}</td>
      </tr>
    ))}
  </tbody>
</Table>
```

Sorting is done client-side for small lists; server-side via
query params for larger ones. The filtering pattern: store
filters in URL search params with `useSearchParams`, derive
the query key from them.

---

## Charts

`components/charts/` uses **Recharts** for time-series, bars,
pies. Wrapped in our own helper components so a chart's
inputs are typed and a "no data" empty state is consistent.

For more bespoke visualizations (the diff view, the topology
graph) we drop down to D3 directly.

---

## Forms

Simple forms: controlled inputs + a `useForm` hook from
`react-hook-form` + Zod for schema validation:

```tsx
const schema = z.object({
  name: z.string().min(1).max(255),
  expiresAt: z.string().optional(),
});

function CreateTokenForm() {
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema) });
  const onSubmit = form.handleSubmit(async (values) => { ... });
  return (
    <form onSubmit={onSubmit}>
      <Field {...form.register("name")} label="Name" />
      <Field {...form.register("expiresAt")} label="Expires (optional)" type="date" />
      <Button type="submit">Create</Button>
    </form>
  );
}
```

The `<Field>` component handles label, input, and error
display.

---

## Color and theme

We define semantic color tokens in CSS variables:

```css
:root {
  --background: 0 0% 100%;
  --foreground: 240 10% 3.9%;
  --primary: 240 88% 60%;
  --destructive: 0 84% 60%;
  --muted: 240 5% 84%;
  --border: 240 6% 90%;
}
.dark {
  --background: 240 10% 3.9%;
  --foreground: 0 0% 98%;
  /* ... */
}
```

Tailwind references them as `bg-background text-foreground
border-border` via the `tailwind.config.js` mapping.

Accent color is a separate variable swapped via the
`data-accent` attribute on `<html>`, set by the
ThemeProvider based on `user.preferences.accent`.

---

## Accessibility

Radix primitives give us keyboard navigation, focus
management, and ARIA attributes for free. We layer on:

- Visible focus rings (Tailwind `focus-visible:ring-2 …`).
- `aria-label` on icon-only buttons.
- `role="alert"` on toast messages.
- Color isn't the only signal — every status badge has a
  word too.

We don't claim WCAG 2.1 AA compliance officially; the project
is on that track and audit reports go in `docs/accessibility/`.

---

## Icons

We use **lucide-react** for icons. They're tree-shaken so the
bundle only includes the ones we import.

---

## What's next

Part IV starts the SoT deep dive. Chapter 14 — the
reconciler architecture.
