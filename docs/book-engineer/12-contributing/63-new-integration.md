# 63. Adding a new integration adapter

An integration adapter connects Daalu to one external
service (cloud, observability, ticketing, etc.). All adapters
share the same interface defined in
`modules/integrations/base.py`.

This chapter walks through adding one. Running example: a
GitHub adapter for pulling issue/PR events.

---

## 1. Pick the type

The `Integration.type` enum (in `models/integration.py`):

```python
class IntegrationType(str, Enum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    SLACK = "slack"
    NAUTOBOT = "nautobot"
    PROMETHEUS = "prometheus"
    # ...
    GITHUB = "github"   # ← new
```

Add the new value, write a migration.

---

## 2. Implement BaseAdapter

`modules/integrations/github/adapter.py`:

```python
from daalu_automation.modules.integrations.base import (
    BaseAdapter, ProbeResult, CrawlResult, HealthResult, Tool
)

class GitHubAdapter(BaseAdapter):
    """Daalu adapter for GitHub."""

    type = "github"

    async def probe(self) -> ProbeResult:
        """Cheap health check — used every 60s."""
        try:
            r = await self._http.get("/user")
            return ProbeResult.ok(metadata={"login": r.json()["login"]})
        except Exception as exc:
            return ProbeResult.error(str(exc))

    async def crawl(self) -> CrawlResult:
        """Full inventory walk. Used on initial sync and on schedule."""
        repos = await self._http.get_paged("/user/repos")
        async with db_session() as db:
            await sync_github_repos(db, self.integration_id, repos)
        return CrawlResult.success(item_count=len(repos))

    async def tools(self) -> list[Tool]:
        """Tools exposed to the Assistant."""
        return [
            Tool(spec=GITHUB_LIST_ISSUES_SPEC, func=self._list_issues),
            Tool(spec=GITHUB_OPEN_ISSUE_SPEC, func=self._open_issue),
        ]

    async def health(self) -> HealthResult:
        return await self.probe()
```

The base class handles credential loading, retry, rate-limit
backoff. You just implement these methods.

---

## 3. Register in the registry

`modules/integrations/registry.py`:

```python
from daalu_automation.modules.integrations.github.adapter import GitHubAdapter

ADAPTERS = {
    "aws": AwsAdapter,
    "gcp": GcpAdapter,
    # ...
    "github": GitHubAdapter,
}
```

Now `Integration(type="github")` rows can be created via the
API and the registry will instantiate the right class.

---

## 4. Wizard schema

`modules/integrations/github/schema.py`:

```python
GITHUB_WIZARD = WizardSchema(
    type="github",
    name="GitHub",
    icon="github",
    fields=[
        TextField("oauth_token", label="OAuth token", secret=True,
                  help="Personal access token with repo scope"),
        TextField("organization", label="Organization", required=False,
                  help="If set, only this org is in scope"),
    ],
)
```

The frontend reads this and renders the wizard.

---

## 5. Credential storage

The adapter receives credentials through `self.credentials`,
already decrypted from the tenant DEK. You don't need to
handle encryption yourself.

```python
class GitHubAdapter(BaseAdapter):
    @property
    def token(self) -> str:
        return self.credentials["oauth_token"]
```

---

## 6. Tool specs

Each tool is defined as a `ToolSpec`:

```python
GITHUB_LIST_ISSUES_SPEC = ToolSpec(
    name="github.list_issues",
    description="List GitHub issues for a repo.",
    args_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repo full name (owner/name)"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
        },
        "required": ["repo"],
    },
    write=False,
    cluster_required=False,
)
```

For write tools, set `write=True` — the framework
automatically routes the call through a change proposal
instead of executing.

---

## 7. Tests

Cover the happy paths and the error modes:

- Probe success.
- Probe failure with bad credentials.
- Crawl with a small fixture.
- Each tool with mocked HTTP.

Mock the HTTP layer with `respx`:

```python
@pytest.fixture
def github_mock(respx_mock):
    respx_mock.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "test"})
    )
```

---

## 8. Documentation

- New chapter in the customer book's Part V (Integrations
  Catalog), Chapter 33–42 range.
- Mention in this chapter's running example list.

---

## 9. Submit

PR checklist:

- Adapter implementation.
- Wizard schema.
- Registry entry.
- Migration for enum value.
- Tests including error modes.
- Customer-book chapter.

A reviewer will check: HTTP retries are correct, secrets
aren't logged, rate-limit handling matches the external
service's documented limits.

---

## What's next

Chapter 64 — adding a new agent.
