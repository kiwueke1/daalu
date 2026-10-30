# 12. The API client

`frontend/lib/api.ts` is the single point of contact between
the frontend and the backend. It's a thin TypeScript wrapper
around `fetch` with typed methods per endpoint.

This chapter covers its design and conventions.

---

## Shape

The client is structured as a singleton object with one
method per backend route:

```ts
export const apiClient = {
  auth: {
    login: (email: string, password: string) => post("/api/v1/auth/login", { email, password }),
    logout: () => post("/api/v1/auth/logout", {}),
    me: () => get<MeResponse>("/api/v1/auth/me"),
    listTokens: () => get<PatRow[]>("/api/v1/auth/tokens"),
    createToken: (name: string, expiresAt?: string) =>
      post("/api/v1/auth/tokens", { name, expires_at: expiresAt }),
    revokeToken: (id: string) => del(`/api/v1/auth/tokens/${id}`),
  },
  alerts: {
    list: (filters?: AlertFilters) => get<Alert[]>("/api/v1/alerts", { params: filters }),
    get: (id: string) => get<AlertDetail>(`/api/v1/alerts/${id}`),
    acknowledge: (id: string) => post(`/api/v1/alerts/${id}/acknowledge`, {}),
    snooze: (id: string, until: string) =>
      post(`/api/v1/alerts/${id}/snooze`, { until }),
  },
  // ... more
};
```

Per-endpoint, one method. The grouping mirrors the backend's
router structure.

---

## Base helpers

```ts
type RequestOpts = {
  params?: Record<string, any>;
  body?: any;
  signal?: AbortSignal;
};

async function request<T>(
  method: string,
  path: string,
  opts: RequestOpts = {},
): Promise<T> {
  const url = new URL(path, location.origin);
  if (opts.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
  }
  const r = await fetch(url.toString(), {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail ?? detail; } catch {}
    throw new ApiError(r.status, detail);
  }
  if (r.status === 204) return undefined as T;
  return r.json();
}

const get  = <T>(p: string, o?: RequestOpts) => request<T>("GET",    p, o);
const post = <T>(p: string, body: any, o?: RequestOpts) =>
  request<T>("POST",   p, { ...o, body });
const patch = <T>(p: string, body: any, o?: RequestOpts) =>
  request<T>("PATCH",  p, { ...o, body });
const del = <T>(p: string, o?: RequestOpts) =>
  request<T>("DELETE", p, o);
```

`credentials: "include"` sends the `daalu_session` cookie
automatically. PATs aren't used from the browser; they're for
scripts.

---

## Error handling

`ApiError` is a typed error with status code and detail
string:

```ts
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}
```

Components handle it explicitly:

```tsx
const mutation = useMutation({
  mutationFn: () => apiClient.alerts.acknowledge(id),
  onError: (e: ApiError) => {
    if (e.status === 401) router.push("/login");
    else toast.error(e.detail);
  },
});
```

A global interceptor in `app/providers.tsx` (React Query's
queryCache/`mutationCache.onError`) catches anything that
bubbles up.

---

## Types

Types mirror the backend Pydantic models. We don't currently
auto-generate them; they live in `lib/types.ts`:

```ts
export interface User {
  id: string;
  email: string;
  full_name: string | null;
  is_admin: boolean;
  tenant_id: string;
  preferences: Record<string, any>;
}

export interface Alert {
  id: string;
  title: string;
  severity: "info" | "warning" | "critical";
  status: "firing" | "resolved";
  labels: Record<string, string>;
  source: string;
  started_at: string;
  resolved_at: string | null;
  assignee_id: string | null;
}
// ...
```

We've considered auto-generating from the OpenAPI spec —
something like `openapi-typescript` would do it. The friction
isn't quite high enough to justify yet, but it's on the
backlog as the API grows.

---

## React Query integration

The API client is used inside React Query hooks:

```tsx
function useAlerts(filters: AlertFilters) {
  return useQuery({
    queryKey: ["alerts", filters],
    queryFn: ({ signal }) => apiClient.alerts.list(filters, { signal }),
  });
}

function useAcknowledgeAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.alerts.acknowledge(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
}
```

The convention:

- `useX` for read queries.
- `useXMutation` (or just `useDoX`) for writes.
- Mutations invalidate any query keys whose data they
  affected, triggering refetch.

We've kept this layer thin on purpose — Tanstack Query does
most of the work.

---

## Server-side fetches

For Next.js server components, the same client doesn't work
directly because we need explicit cookie forwarding. We have
a thin server-side wrapper in `lib/api.server.ts`:

```ts
import { cookies, headers } from "next/headers";

async function serverRequest<T>(method: string, path: string, body?: any) {
  const cookieHeader = cookies().toString();
  const r = await fetch(`${process.env.API_URL}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      Cookie: cookieHeader,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!r.ok) throw new ApiError(r.status, await r.text());
  return r.json();
}
```

Used in `getServerSideProps`-like patterns inside App Router
server components.

---

## Streaming responses

For the Assistant's chat — which streams tokens — we use a
separate code path:

```ts
export async function* chatStream(message: string, signal: AbortSignal) {
  const r = await fetch("/api/v1/copilot/chat", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ message }),
    signal,
  });
  const reader = r.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    while (buffer.includes("\n\n")) {
      const [event, rest] = buffer.split("\n\n", 2);
      buffer = rest;
      yield parseSSEEvent(event);
    }
  }
}
```

SSE rather than WebSocket — simpler, sufficient for one-way
streams, plays well with HTTP/2.

---

## What's next

Chapter 13 — shared components and the design system.
