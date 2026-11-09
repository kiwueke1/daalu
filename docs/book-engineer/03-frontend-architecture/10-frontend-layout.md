# 10. Next.js app router layout

The frontend lives in `frontend/`. It's a Next.js 14
application using the App Router. This chapter covers the
overall structure; Chapter 11 is the per-page tour, Chapter
12 is the API client, Chapter 13 covers shared components.

---

## High-level layout

```
frontend/
├── app/                     # App Router routes, globals.css, providers.tsx
├── components/              # shared UI components (layout/, ui/, per-domain dirs)
├── lib/                     # client utilities (API client, auth, theme, errors)
├── public/                  # static assets
├── e2e/                     # Playwright screenshot tests + fixtures
├── scripts/                 # ui-shots.mjs (real-data screenshots)
├── DESIGN.md                # the UI rules every screen follows
├── package.json
├── next.config.js
├── tailwind.config.ts
├── tsconfig.json
└── Dockerfile
```

We're on Next.js 14 with the App Router. Pages live under
`app/`; each route is a directory with a `page.tsx`.

Server components vs client components:

- The default in App Router is server components.
- In practice every page is a client component (`"use client"`
  at the top). The few `page.tsx` files without it, such as
  `app/network/page.tsx` and `app/proposals/page.tsx`, only
  call `redirect()` to the tab that replaced them.
- Data is fetched in the browser with React Query; there is no
  server-side data fetch.

---

## Routing

App Router maps directories under `app/` to URLs:

```
app/page.tsx              → /
app/login/page.tsx        → /login
app/alerts/page.tsx       → /alerts
app/alerts/[id]/page.tsx  → /alerts/<id>
app/settings/page.tsx     → /settings
app/help/page.tsx         → /help
```

There is one layout, `app/layout.tsx`, and no route groups.
The authenticated chrome (sidebar, header, right rail) is not a
layout file: it is the `AppShell` client component, which the
root layout wraps around every page and which decides per route
what to draw (see "The app shell" below).

---

## Providers

`app/providers.tsx` exports the client-side context wrapper:

```tsx
"use client";
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 15_000, refetchOnWindowFocus: false, retry: false },
        },
      })
  );
  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider>{children}</AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
```

React Query is the data layer for nearly every page.
`ThemeProvider` (`lib/theme.tsx`) holds dark/light.
`ToastProvider` (`components/ui/toast.tsx`) backs `useToast()`.
`AuthProvider` (`lib/auth.tsx`) loads the current user from
`GET /api/v1/auth/me` and exposes `useAuth()`.

---

## The root layout

`app/layout.tsx` (trimmed):

```tsx
const THEME_BOOT = `try{var t=localStorage.getItem("daalu-theme");…document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" className={quicksand.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body className="ambient-bg min-h-screen text-[color:var(--text)]">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
```

The theme is applied before first paint. `THEME_BOOT` is an
inline `<head>` script that reads the saved choice from
`localStorage["daalu-theme"]`, falls back to the OS
`prefers-color-scheme`, and sets `data-theme` on `<html>`.
Without it a light-mode user saw a dark flash on every load,
because `ThemeProvider` only runs after hydration. With no saved
choice the provider keeps following the OS if it switches.
`suppressHydrationWarning` is there because the script changes
`data-theme` before React hydrates.

---

## The app shell

`components/layout/app-shell.tsx` picks one of four renderings
by route:

- **Public routes** (`/login`, `/signup`, `/verify-email`, and
  anything under the `/accept-invite` prefix): the page alone,
  no chrome, no auth wait.
- **Loading**: a centred "Loading…" while `useAuth()` resolves.
- **Focused routes** (`/cli/activate`): signed in, but no
  sidebar, header or rail, so a consent step has the whole screen.
- **Everything else**:

```tsx
<div className="flex min-h-screen">
  <a href="#main" className="skip-link">Skip to content</a>
  <Sidebar mobileOpen={mobileNavOpen} onMobileClose={…} />
  <div className="flex-1 min-w-0">
    <Header onMobileNavOpen={…} />
    <div className="flex">
      <main id="main" tabIndex={-1} className="flex-1 min-w-0 …">
        <div key={pathname} className="motion-enter">{children}</div>
      </main>
      {!skipGlobalRail && <RightPanel />}
    </div>
  </div>
  {showAssistantButton && <AssistantSheet />}
</div>
```

- `Sidebar` (`components/layout/sidebar.tsx`) is the grouped left
  nav. On a phone it is a drawer opened from the header's menu
  button; the shell closes it on route change and locks body
  scroll while it is open.
- `Header` (`components/layout/header.tsx`) is sticky and 64px
  tall. Pages that need an inner scroll area size themselves with
  `h-[calc(100dvh-4rem)]`; `<main>` has no height of its own.
- `RightPanel` (`components/layout/right-panel.tsx`) is the right
  rail, shown from the `xl` breakpoint up. Alert detail pages
  (`/alerts/<id>`) skip it (`ownsRightRail`) because they render
  their own remediation-chat rail.
- `AssistantSheet` (`components/layout/assistant-sheet.tsx`) is the
  rail's stand-in below `xl`: a floating Assistant button and a
  bottom sheet. It is hidden on `/alerts/<id>` and `/workspace`
  (`hidesAssistantButton`).
- The page content is re-keyed per route so each page fades in
  (`motion-enter`), and the skip link jumps to `#main`.

Auth is enforced by the API, not by the layout. Any API call that
returns 401 makes `lib/api.ts` send the browser to
`/login?next=<current path>` (except on the public routes). An
uncaught render error lands in `app/error.tsx`, the root error
boundary.

Chapter 13 covers the sidebar, header and rail in detail.

---

## Data fetching patterns

Every page fetches in the browser with React Query through the
`api` client in `lib/api.ts` (Chapter 12):

```tsx
// app/alerts/page.tsx
const openQ = useQuery({
  queryKey: ["alerts", "open"],
  queryFn: () => api.alerts.list({ status: "open" }),
  refetchInterval: 10_000,
});
```

Two conventions hold everywhere:

- **Query keys are shared on purpose.** The header status, the
  sidebar's Alerts badge and the Assistant sheet all use
  `["alerts", "open-summary"]`, so they are one fetch and always
  agree. Reuse an existing key when you show the same data.
- **Never render a zero or an empty state while loading or
  failed.** Wrap the result in `LoadState`
  (`components/ui/load-state.tsx`), which handles loading, error,
  empty and "not set up yet". A query backed by live infra
  (Prometheus, a GPU node) is wrapped in `withTimeout()` from
  `lib/timeout.ts`, so it gives up after 15 s
  (`LIVE_INFRA_TIMEOUT_MS`) with a `TimeoutError` instead of
  spinning forever; `isUnreachable(error)` tells that case (and a
  502/504) apart from other errors.

The session cookie is HttpOnly. Client components never read it;
the browser sends it with every same-origin request
(`credentials: "include"`).

---

## Styling

Tailwind + `app/globals.css`. Colours are CSS variables defined
twice in `globals.css`, once per theme, and switched by the
`data-theme` attribute on `<html>`:

```css
/* dark is the default */
:root {
  --accent: #22d3a6;
  --text: #e7efe9;
  --text-muted: #8a9a93;
  --critical: #f26161;
  /* ... */
}
html[data-theme="light"] {
  --text: #0a1612;
  --text-muted: #56655f;
  /* ... */
}
```

`tailwind.config.ts` maps them to class names (`text-fg`,
`bg-accent`, `bg-signal-critical`, …) through a `color-mix()`
helper, so opacity modifiers like `bg-accent/10` work.
Components use the tokens, never a hard-coded hex value. The text
tokens are tuned to clear WCAG AA (4.5:1) on every surface in both
themes. Motion classes (`motion-enter`, `motion-press`,
`motion-attention`, …) and their durations live at the bottom of
`globals.css`, and a `prefers-reduced-motion: reduce` block there
turns them all off. `frontend/DESIGN.md` is the rulebook for
tokens, motion, states and layout.

---

## Build

Standard Next.js build:

```bash
npm run build
npm run start
```

The Dockerfile multi-stages this. Final image is
~150 MB compressed.

---

## What's next

Chapter 11 — page-by-page tour of every route.
