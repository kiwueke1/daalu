# 8. The module system

A **module** in Daalu's terminology is a self-contained slab
of domain logic, bundled with its own router(s), models, and
optional Celery tasks. Modules let us add a new domain
(infra, support, finance, healthcare) without
threading changes through the whole codebase.

This chapter explains the module contract. Chapter 62 walks
through adding a new one.

---

## What a module looks like

A module lives at `src/daalu_automation/modules/<name>/`:

```
modules/<name>/
├── __init__.py              # public interface re-exports
├── models.py                # SQLAlchemy ORM models
├── service.py               # business logic
├── router.py                # FastAPI router (optional)
├── tasks.py                 # Celery tasks (optional)
├── adapters/                # integration adapters
└── tests/                   # module-scoped tests
```

The shape is conventional — there is no enforcement. We've
found over time that this layout is easier to navigate than
flat per-type directories.

---

## Existing modules

```
modules/
├── alerts/        # the alert pipeline (rules → events → alerts → notifications)
├── infra/         # incidents, services, observability adapters
├── integrations/  # adapter implementations for all integration types
└── sot/           # source-of-truth: reconciler, executor, drift handling
```

`sot/` is the largest module by volume; it contains the
reconciler, the executor logic, and the Nautobot adapter.

`integrations/` is special — it contains *one folder per
integration type* (aws/, gcp/, slack/, prometheus/, etc.).
Each subfolder has the adapter for that one external system.

---

## Module contract

For the rest of the application to use a module, it must:

1. Export its ORM models so SQLAlchemy can discover them.
   This is done by importing them in
   `models/__init__.py`:

   ```python
   from daalu_automation.models.infra import Incident, IncidentSeverity, ...
   ```

2. Expose a FastAPI router (if it has one), mounted in
   `api/main.py`:

   ```python
   from daalu_automation.api.routers import infra
   ...
   app.include_router(infra.router, prefix="/api/v1")
   ```

3. Define Celery tasks (if it has any) with module-prefixed
   names so the routing in `celery_app.py:task_routes`
   matches them.

4. Document its public service functions in `__init__.py` so
   other modules can import them as
   `from daalu_automation.modules.infra import …`.

There is no formal plugin or registration system. Modules
are linked statically at import time. The "module" concept
is a design-level boundary, not a runtime one.

---

## Module boundaries

A few rules we follow to keep modules genuinely modular:

### A module's models live in its own folder

`models/infra.py` is where the `infra` module's tables go.
The toplevel `models/__init__.py` re-exports them.

Cross-module foreign keys are allowed but discouraged. When
they exist, they're documented in the migration.

### Modules call each other through their `__init__.py`

If `alerts` needs something from `sot`, it imports from
`daalu_automation.modules.sot` — not from
`daalu_automation.modules.sot.service.specific_helper`. The
`__init__.py` is the module's public API.

### Modules do not import from `api/`

The `api/` layer can import from any module. Modules do not
import from `api/`. This keeps modules testable without a
FastAPI process.

### Modules can subscribe to events

A weak-coupling mechanism: modules can register callbacks for
named events. E.g., the `alerts` module subscribes to
`event.created` and decides whether to fire an alert. The
registry lives in `core/eventbus.py`. New modules subscribe
at import time.

---

## The integrations module

This module is somewhat different because it's an
**adapter library**, not a domain. Its folder layout:

```
modules/integrations/
├── __init__.py
├── base.py                  # Adapter base class
├── registry.py              # type → adapter class mapping
├── aws/
│   ├── __init__.py
│   ├── adapter.py
│   ├── inventory.py
│   ├── cost.py
│   └── tools.py
├── gcp/
├── azure/
├── slack/
├── pagerduty/
├── nautobot/
├── prometheus/
├── loki/
├── thanos/
├── opensearch/
├── grafana/
└── webhook/
```

Every adapter:

- Extends `BaseAdapter`.
- Implements `probe()`, `crawl()`, `tools()`, `health()`.
- Registers itself in `registry.py` so the API can dispatch
  `POST /integrations` to the right class.

Chapter 63 walks through adding a new adapter.

---

## What's *not* in a module

To set expectations:

- **The Helm chart.** Modules don't have their own k8s
  manifests; the chart deploys the whole application.
- **Frontend code.** The frontend isn't module-organized;
  it's page-organized. Some pages span multiple modules.
- **Cross-cutting cross-domain logic.** When a feature
  touches multiple modules deeply, it lives in `core/`
  rather than picking one module to own it.

---

## What's next

Chapter 9 — the cross-cutting core libraries.
