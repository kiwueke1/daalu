# Extending — adding a new module

Adding a new domain (Support, Finance, Operations, HR, Healthcare,
Manufacturing…) is a self-contained exercise. You add one package
under `modules/`; the platform picks it up automatically.

## The shape

Every module exposes the same set of files. The `infra/` module is
the canonical example — read it alongside this guide.

```
src/daalu_automation/modules/<name>/
├── __init__.py          # side-effect imports
├── prompts.py           # LLM prompts for this module
├── briefing.py          # BriefingGenerator subclass
├── agent.py             # one or more Agent subclasses
├── integrations.py      # IntegrationAdapter subclasses (always include a synthetic one)
├── workflows.py         # @register_workflow coroutines
└── tasks.py             # celery wrappers
```

## Step-by-step: adding a "Support" module

### 1. Decide your event taxonomy

`support.<noun>.<verb>` — for example:

- `support.ticket.created`
- `support.ticket.escalated`
- `support.ticket.resolved`
- `support.churn.signal_detected`

### 2. Add domain tables (if you need any)

If your module has its own entities (tickets, customers), add a model
file under `src/daalu_automation/models/support.py` and re-export
from `models/__init__.py`. Use the existing mixins:

```python
from daalu_automation.models._mixins import (
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin,
)

class Ticket(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"
    ...
```

Then add a new value to `BriefingChannel` in `models/briefing.py`:

```python
class BriefingChannel(str, enum.Enum):
    ...
    support = "support"
```

### 3. Write prompts

`modules/support/prompts.py` — keep both the briefing system prompt and
any triage prompts here. The two existing modules give you the template:

```python
TICKET_TRIAGE_SYSTEM = """..."""
BRIEFING_SYSTEM       = """..."""
```

Output STRICT JSON; the briefing pipeline parses it.

### 4. Implement the briefing generator

```python
class SupportBriefingGenerator(BriefingGenerator):
    channel = BriefingChannel.support
    module = "support"
    def system_prompt(self): return BRIEFING_SYSTEM
    def user_prompt(self, ctx): ...
    def derive_metrics(self, ctx, ai_output): ...

register_briefing(SupportBriefingGenerator)
```

### 5. Implement the agent

```python
class SupportAgent(Agent):
    descriptor = AgentDescriptor(
        name="support-agent",
        module="support",
        description="Triages escalated tickets and detects churn signals.",
        subscribed_event_types=("support.ticket.escalated", "support.churn.signal_detected"),
    )
    async def handle(self, event: EventEnvelope) -> None:
        ...

register_agent(SupportAgent)
```

### 6. Implement at least one integration

Always include a synthetic adapter so demos work without external
credentials:

```python
class SyntheticSupportAdapter(IntegrationAdapter):
    descriptor = IntegrationDescriptor(
        provider="synthetic-support", module="support",
        display_name="Synthetic support (demo)",
        description="Generates fake support events for demos.",
        required_settings=(),
    )
    async def ingest(self) -> int: ...
register_integration(SyntheticSupportAdapter)

# Real adapter — register only when configured.
class ZendeskAdapter(IntegrationAdapter):
    descriptor = IntegrationDescriptor(
        provider="zendesk", module="support",
        display_name="Zendesk",
        description="...", required_settings=("ZENDESK_API_TOKEN",),
    )
    async def ingest(self) -> int: ...

if get_settings().zendesk_api_token:
    register_integration(ZendeskAdapter)
```

### 7. Workflows (optional)

```python
@register_workflow("support.ticket.coordinate_escalation", module="support")
async def coordinate_escalation(payload):
    ...
```

### 8. Celery tasks

`modules/support/tasks.py`:

```python
@celery_app.task(name="support.generate_briefing")
def generate_briefing_task(): ...

@celery_app.task(name="support.zendesk_ingest")
def zendesk_ingest_task(): ...
```

Then add to the beat schedule in
`src/daalu_automation/workers/celery_app.py`.

### 9. Wire the module up

`src/daalu_automation/modules/__init__.py`:

```python
from daalu_automation.modules import infra, support  # noqa: F401
```

`src/daalu_automation/workers/celery_app.py::_autoload_modules()`:

```python
import daalu_automation.modules.support.tasks  # noqa: F401
```

### 10. (Optional) Module-specific REST routes

If your module exposes resources the UI wants to browse (like
infra's `/incidents`), add a router under
`src/daalu_automation/api/routers/<name>.py` and include it in
`api/main.py`.

### 11. UI

The home dashboard, alerts page, recommendations page, operations
page, briefings page all auto-pick up the new module — they're
filterable by `module` and read directly from the platform tables.

For a dedicated page (e.g. `/support/tickets`), drop a new
`frontend/app/support/page.tsx` and a typed call into `frontend/lib/api.ts`.

Enable the module in the Operations page filter tabs:

```tsx
// frontend/app/operations/page.tsx
const MODULES = [
  ...
  { id: "support", label: "Support" }, // remove `disabled`
];
```

That's it. The new module ships agents, briefings, integrations, and
UI affordances without touching any platform code.

---

## Naming + convention summary

- Module name: lowercase, no hyphens. Matches the `module` column on
  every table and the Redis stream tag.
- Event types: `<module>.<noun>.<verb>` lowercase, snake-case inside
  segments.
- Integration provider slug: lowercase-hyphenated (`pagerduty`,
  `synthetic-infra`).
- Agent name: kebab-case (`infra-agent`).
- Workflow name: dotted by module (`infra.incident.coordinate`).
