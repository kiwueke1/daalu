# 4. The FastAPI application

The cloud-side API server is `src/daalu_automation/api/`.
This chapter walks through how it's organized and what each
part does.

---

## Entrypoint

`src/daalu_automation/api/main.py` is the entrypoint:

- Defines `create_app() -> FastAPI` (called by uvicorn or
  `daalu server` CLI).
- Sets up logging via `observability.init_observability()`.
- Configures middlewares.
- Mounts routers.
- Wires lifespan hooks (startup: `create_tables()`,
  `ensure_default_tenant()`).

The runtime command in the Dockerfile is effectively:

```bash
daalu server --host 0.0.0.0 --port 8000
```

`daalu server` is defined in `cli.py` and ultimately calls
`uvicorn.run(create_app, ...)`.

---

## Middlewares

In order (outermost first):

### 1. CORSMiddleware

Standard FastAPI CORS. Allowed origins come from
`settings.cors_origins`. Browser preflight passes through to
the rest of the stack.

### 2. AuthGateMiddleware

Defined in `api/main.py:90`. Its job is to **prevent
anonymous traffic from reaching authenticated endpoints**.
The gate is fast — exact path match against a small set of
public paths, then a delegating check for JWT/PAT presence.

```python
async def dispatch(self, request, call_next):
    path = request.url.path
    if path in self._public:
        return await call_next(request)
    if path.startswith("/api/v1/auth/"):
        return await call_next(request)
    if request.method == "POST" and path == self._ingest_path:
        return await call_next(request)
    if request.method == "POST" and self._cluster_callback_re.match(path):
        return await call_next(request)
    if request.method == "POST" and self._sot_webhook_re.match(path):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            token = authz.split(None, 1)[1]
    if not token:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    # PATs are opaque; defer validation to current_user.
    if not looks_like_pat(token):
        try:
            decode_token(token)
        except TokenError as e:
            return JSONResponse({"detail": f"invalid token: {e}"}, status_code=401)
    return await call_next(request)
```

The gate is **decode-only** for JWTs — it doesn't load the
user from the DB. That's `current_user`'s job. The separation
keeps the gate cheap (no DB hit on every request) and the
route handlers explicit about what user they're acting as.

Public paths are configured in `config.py`'s
`auth_public_paths`. Currently:

```
/health, /version, /metrics, /, /api/v1/auth/login,
/api/v1/auth/logout, /api/v1/auth/me, /docs, /redoc,
/openapi.json
```

`/metrics` is on the list because Prometheus scrapes it from
the monitoring namespace; the NetworkPolicy is what keeps
external callers from reaching it.

### 3. Request-id middleware

Adds `X-Request-Id` to every request. structlog binds it for
the duration of the request handler.

---

## Routers

Mounted under `/api/v1/<router>` in `create_app()`:

```python
app.include_router(auth.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(alerts.router, prefix="/api/v1")
app.include_router(alert_chat.router, prefix="/api/v1")
app.include_router(billing.router, prefix="/api/v1")
app.include_router(clusters.router, prefix="/api/v1")
app.include_router(copilot.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(infra.router, prefix="/api/v1")
app.include_router(integrations.router, prefix="/api/v1")
app.include_router(onboarding.router, prefix="/api/v1")
app.include_router(proposals.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
app.include_router(sot_devices.router, prefix="/api/v1")
app.include_router(sot_webhooks.router, prefix="/api/v1")
app.include_router(workflows.router, prefix="/api/v1")
```

Each router file follows a consistent pattern:

```python
router = APIRouter(prefix="/<resource>", tags=["<resource>"])

class <Resource>Response(BaseModel):
    ...

@router.get("/", response_model=list[<Resource>Response])
async def list_resources(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    ...
```

The `current_user` dependency does the auth (JWT or PAT) and
returns a `User` ORM row. From there the handler issues
SQLAlchemy queries scoped to `user.tenant_id`.

---

## Dependencies (`api/deps.py`)

The shared FastAPI dependencies:

### `current_user`

Resolves the authenticated user from cookie, JWT bearer, or
PAT bearer.

```python
async def current_user(
    daalu_session: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = daalu_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1]
    if not token:
        raise HTTPException(401, "not authenticated")
    if looks_like_pat(token):
        return await _resolve_pat(token, db)
    payload = decode_token(token)
    user = await db.get(User, payload["user_id"])
    if user is None or not user.is_active:
        raise HTTPException(401, "user not found or inactive")
    return user
```

### `_resolve_pat`

PAT lookup path. Hashes the token, queries `personal_access_
tokens` by hash, checks expiry/revocation, stamps
`last_used_at`, returns the user.

### `current_superuser`

Like `current_user` but also requires `user.is_superuser`.
Used for platform-team-only endpoints (e.g., the feedback
inbox at `GET /feedback`).

### `get_db`

Returns an `AsyncSession` from the SQLAlchemy session
factory. The session is created per-request and closed
automatically when the request ends.

---

## Lifespan hooks

`create_app()` wraps `FastAPI(lifespan=...)` so we can run
startup tasks:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    await ensure_default_tenant()
    yield
```

`create_tables` is a no-op in production (migrations
handle schema); it exists for tests and local dev where
running Alembic would be friction.

`ensure_default_tenant` creates the `00000…000` tenant on
first start. Used by webhook-only senders that pre-date a
real tenant.

---

## Error handling

A handful of registered exception handlers in `main.py`:

```python
@app.exception_handler(TokenError)
async def handle_token_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=401)

@app.exception_handler(IntegrityError)
async def handle_integrity_error(_, exc):
    log.warning("db.integrity_error", error=str(exc.orig))
    return JSONResponse({"detail": "conflict"}, status_code=409)
```

Anything not specifically handled becomes a 500. The
structlog middleware emits an `unhandled_exception` log line
with the traceback.

---

## Observability hooks

The instrumentation lives in `observability.py`:

- structlog configured for JSON output in production, pretty
  in dev.
- prometheus-fastapi-instrumentator wires `/metrics`. Every
  HTTP route auto-instrumented with histograms for duration
  and counters for status code.
- excluded handlers: `/metrics` and `/health` (don't measure
  the meters).

The /metrics endpoint is exposed unauthenticated by the
public-paths allowlist; the NetworkPolicy is what restricts
who can reach it.

---

## What's next

Chapter 5 goes deep on auth — JWT, PATs, the auth gate,
ingest API keys, sessions, password hashing.
