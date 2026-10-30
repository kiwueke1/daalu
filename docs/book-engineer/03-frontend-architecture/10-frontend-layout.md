# 10. Next.js app router layout

The frontend lives in `frontend/`. It's a Next.js 14
application using the App Router. This chapter covers the
overall structure; Chapter 11 is the per-page tour, Chapter
12 is the API client, Chapter 13 covers shared components.

---

## High-level layout

```
frontend/
├── app/                     # App Router routes
├── components/              # shared UI components
├── lib/                     # client utilities (API client, auth helpers)
├── styles/                  # Tailwind config + globals.css
├── public/                  # static assets
├── package.json
├── next.config.js
├── tailwind.config.js
├── tsconfig.json
└── Dockerfile
```

We're on Next.js 14 with the App Router. Pages live under
`app/`; each route is a directory with a `page.tsx`.

Server components vs client components:

- The default in App Router is server components.
- We mark client components explicitly with `"use client"` at
  the top of files that need interactivity (most of them, in
  practice).
- Server components do the initial data fetch on the server,
  bumping perceived performance.

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

Nested layouts:

- `app/layout.tsx` — root layout with `<html>`, `<body>`,
  global providers.
- `app/(authed)/layout.tsx` — wraps every authed page with
  the sidebar and top bar.

We use route groups (parentheses-named directories) for
shared layouts without affecting the URL. `(authed)` is the
main one; `(public)` would group login/signup if we split
them out.

---

## Providers

`app/providers.tsx` exports the client-side context wrapper:

```tsx
"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/components/theme-provider";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: 1,
      },
    },
  }));
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
}
```

Wrapped at `app/layout.tsx`:

```tsx
<body>
  <Providers>{children}</Providers>
</body>
```

React Query is the data layer for nearly every page.
ThemeProvider toggles dark/light/system.

---

## The root layout

`app/layout.tsx`:

```tsx
import "./globals.css";

export const metadata = {
  title: "Daalu Automation",
  description: "Operations cockpit",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
```

`suppressHydrationWarning` is for the theme provider — the
class applied on initial server render doesn't match the
client because we read `localStorage` for the theme. The
provider handles the mismatch correctly; this just tells
React not to scream about it.

---

## The authed layout

`app/(authed)/layout.tsx`:

```tsx
import { Sidebar } from "@/components/layout/sidebar";
import { TopBar } from "@/components/layout/top-bar";
import { AssistantPanel } from "@/components/assistant/panel";
import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth";

export default async function AuthedLayout({
  children,
}: { children: React.ReactNode }) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return (
    <div className="flex h-screen">
      <Sidebar />
      <div className="flex flex-1 flex-col">
        <TopBar user={user} />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
      <AssistantPanel />
    </div>
  );
}
```

The `getCurrentUser` helper hits `/api/v1/auth/me` server-side
with the request's cookies; if it returns 401, we redirect to
`/login`. This makes every authed page deny-by-default at the
layout level.

---

## Data fetching patterns

We use two patterns, chosen by latency sensitivity:

### Server components for initial loads

```tsx
// app/alerts/page.tsx
import { fetchAlerts } from "@/lib/api";

export default async function AlertsPage() {
  const alerts = await fetchAlerts();   // server-side fetch
  return <AlertsList initial={alerts} />;
}
```

The initial render has data; React Query takes over from
there.

### React Query for interactive updates

```tsx
"use client";
function AlertsList({ initial }: { initial: Alert[] }) {
  const { data } = useQuery({
    queryKey: ["alerts"],
    queryFn: () => apiClient.alerts.list(),
    initialData: initial,
    refetchInterval: 30_000,
  });
  return <Table data={data} />;
}
```

The `initialData` from the server avoids a flash of empty
content.

---

## Cookie auth from server components

The session cookie is HttpOnly. Server components can read it
because they execute on the server with access to the request
headers:

```tsx
// lib/auth.ts
import { cookies } from "next/headers";

export async function getCurrentUser() {
  const c = cookies();
  const session = c.get("daalu_session");
  if (!session) return null;
  const r = await fetch(`${process.env.API_URL}/api/v1/auth/me`, {
    headers: { Cookie: `daalu_session=${session.value}` },
  });
  if (!r.ok) return null;
  return r.json();
}
```

Client components call `/api/v1/auth/me` without explicit
cookie handling — the browser sends the cookie automatically.

---

## Styling

Tailwind + `globals.css` + a small set of CSS variables for
theming:

```css
:root {
  --background: 0 0% 100%;
  --foreground: 240 10% 3.9%;
  /* ... */
}
.dark {
  --background: 240 10% 3.9%;
  --foreground: 0 0% 98%;
}
```

Components reference these as `bg-background text-foreground`.
Theme toggle swaps the `.dark` class on `<html>`.

We pick a few accent colors per user pref:

```css
[data-accent="indigo"] { --primary: 240 88% 60%; }
[data-accent="green"]  { --primary: 142 76% 36%; }
```

The accent comes from `users.preferences.accent`.

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
