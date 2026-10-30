# Daalu Automation — operator + developer command surface.
#
# Most targets are thin wrappers around scripts in `dev/` and
# `deploy/`. Run `make help` for the menu.

CLUSTER_NAME ?= daalu-dev
ENV          ?= dev

.PHONY: help bootstrap-dev bootstrap-prod down test lint typecheck \
        build images monitoring argocd tilt clean


help:                ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ── Local development ──────────────────────────────────────────────────
bootstrap-dev:       ## Bring up a full local stack on Kind + monitoring + app.
	bash dev/bootstrap.sh

tilt:                ## Inner-loop dev — file watch + live reload (needs bootstrap-dev first).
	tilt up

down:                ## Tear the Kind cluster down.
	kind delete cluster --name $(CLUSTER_NAME)

# ── CI-equivalent local checks ─────────────────────────────────────────
test:                ## Run the backend test suite (uses local Postgres + Redis).
	pytest -q --maxfail=1

lint:                ## Ruff lint + format check.
	ruff check src tests
	ruff format --check src tests

typecheck:           ## mypy across src/.
	mypy src

# ── Build / images ─────────────────────────────────────────────────────
build images:        ## Build local images (no push).
	docker build -t daalu-automation:dev .
	docker build -t daalu-automation-frontend:dev frontend

# ── Prod-shape installs (against a real cluster — KUBECONFIG must be set) ──
bootstrap-prod:      ## From-scratch install of monitoring + ArgoCD on a new cluster.
	bash deploy/bootstrap-prod.sh

monitoring:          ## Install/upgrade the monitoring stack only.
	bash deploy/k8s/monitoring/install.sh $(ENV)

argocd:              ## Install ArgoCD + register the app-of-apps.
	bash deploy/argocd/install.sh

clean:               ## Remove build artefacts (does not touch clusters).
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf frontend/.next frontend/node_modules
