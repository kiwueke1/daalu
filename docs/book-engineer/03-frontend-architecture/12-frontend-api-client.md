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

`lib/api.ts` throws two error types:

```ts
export class UnauthorizedError extends Error { … }   // any 401

export class ApiError extends Error {
  status: number;
  body: string;    // the raw response text
  detail: string;  // FastAPI's `detail` when the body is JSON, else the body
  constructor(status: number, body: string, message: string) { … }
}
```

A 401 is handled inside `request()` itself: unless the browser
is already on `/login`, `/accept-invite`, `/signup` or
`/verify-email`, it replaces the location with
`/login?next=<current path>` and throws `UnauthorizedError`.
Components never redirect on 401 themselves.

Every other non-2xx response throws `ApiError`. There is no global
`onError` interceptor; each query or mutation decides what to
show. What it shows always goes through `errorText()` from
`lib/errors.ts`, the only way an error reaches the screen:

```tsx
// app/reports/schedules/page.tsx
const toast = useToast();
const remove = useMutation({
  mutationFn: (id: string) => api.reports.schedules.remove(id),
  onSuccess: () => {
    qc.invalidateQueries({ queryKey: ["report-schedules"] });
    toast.success("Schedule deleted. It won't send again.");
  },
  onError: (e) => toast.error(errorText(e)),
});
```

`errorText(error)` returns one plain sentence that says what to do
next:

- `UnauthorizedError` → "Your session has ended. Sign in again to
  continue."
- `ApiError` with a readable `detail` (short, not HTML, not JSON,
  no traceback) → the `detail` itself. A 403 is prefixed with
  "Only a tenant admin can do this."
- `ApiError` without a readable `detail` → a sentence chosen by
  status (400/422, 404, 409, 429, 502–504, other 5xx).
- A `fetch` network `TypeError` → "Couldn't reach Daalu. Check
  your connection, then retry."

Never render `String(err)`, `err.message` or `ApiError.body`
directly: those leak raw JSON, HTML error pages and stack traces
into the product. For a query, pass the error to `LoadState`
(`components/ui/load-state.tsx`), which renders "Couldn't load
this." with the `errorText()` sentence underneath, plus a Retry
button when retrying could help (a 5xx, 408, 429 or network
error, not other 4xx). A "not set up yet" answer (for example a 409 "no
nautobot integration") is not an error: `LoadState`'s `notSetUp`
prop shows a neutral next step with a link instead.

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

There are none. Every page is a client component and calls the
browser client above, which relies on the browser sending the
HttpOnly `daalu_session` cookie (`credentials: "include"`). There
is no `lib/api.server.ts`.

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
