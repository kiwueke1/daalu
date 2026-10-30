# Contributing to Daalu

Thanks for helping. A few things make reviews fast.

## Setup

Follow "Running from source" in the [README](README.md). The backend tests need
Postgres and Redis (`docker run` commands are in the README).

```bash
make lint        # ruff check + format check
make typecheck   # mypy
make test        # pytest
cd frontend && npm run lint && npm run build
```

## Pull requests

- Keep each PR to one change, and say why in the description.
- Add or update tests for behaviour changes. Add a migration under
  `migrations/versions/` for every schema change; the
  [contributor handbook](docs/book-engineer/12-contributing/) has the patterns.
- UI changes follow [`frontend/DESIGN.md`](frontend/DESIGN.md). Put a screenshot
  in the PR.
- Don't hand-edit vendored code (`src/nv_config_manager_installer/`,
  `components/nautobot/`, `deploy/charts/`). Re-vendor it from upstream instead.
- Never commit secrets. CI runs gitleaks, and `.env` is gitignored.

## Adding things

- A new domain module: [docs/extending.md](docs/extending.md)
- A new integration or agent: [docs/book-engineer/12-contributing/](docs/book-engineer/12-contributing/)

By contributing, you agree that your contributions are licensed under Apache-2.0.
