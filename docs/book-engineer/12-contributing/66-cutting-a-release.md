# 66. Cutting a release

Daalu doesn't have formal semantic-versioned releases — we
deploy continuously from `master`. But there are a few
practices that map roughly to "release engineering."

This chapter is the discipline for the moments where you
*are* doing release-like work.

---

## Continuous deployment

`master` is always deployable. Every merged commit:

1. Triggers `build-images.yml` if it touches build paths.
2. Image is tagged with the commit SHA and pushed to GHCR.
3. CD bot writes a `chore(cd): pin images to <sha>` commit
   updating `deploy/k8s/...` manifests.
4. ArgoCD detects the pin update and would deploy if the
   application were correctly configured.
5. In practice, an engineer runs `install.sh` with the SHA.

The whole pipeline takes 5–15 minutes from merge to live.

---

## Pre-deploy checks

Before running `install.sh` against production:

1. **Run integration tests locally.** `uv run pytest tests/
   integration` should pass on the commit you're about to
   deploy.
2. **Check CI status.** The build-images workflow must have
   completed successfully for the SHA you're pinning.
3. **Inspect the diff.** `git log --oneline <last-prod-sha>..
   HEAD` — read every commit. Any surprises? Bail.
4. **Check migrations.** If a new migration has landed, read
   it. Apply locally first. Check `alembic history` for
   linearity (no branches).
5. **Check secrets.** If new env vars were added, the
   `daalu-automation-secrets` Secret needs them. `install.sh`
   regenerates from `.env`.

---

## Deploy

```bash
bash deploy/scripts/install.sh
```

The script handles cert-manager secrets, GHCR pull secret,
app secrets, image-tag resolution, helm upgrade, rollout
verification. See Chapter 45.

---

## Post-deploy checks

1. **Smoke script.** Run the post-deploy smoke script
   (chapter 61). All six lines should print.
2. **Live UI sanity.** Log in to `ops.example.com`, click
   through home, alerts, integrations. Anything weird?
3. **Error rate.** Grafana dashboard "Daalu Platform
   Overview" — error rate should be flat post-deploy. A
   spike means trouble.
4. **Rollout completeness.** `kubectl -n daalu-automation
   get pods` — all Ready, no CrashLoopBackOff.
5. **Migration check.** If a migration ran, verify the
   change is visible:

   ```bash
   kubectl -n daalu-automation exec daalu-postgres-0 -- psql -U daalu -c "\d users"
   ```

---

## Rollback

If something's wrong:

```bash
# rollback Helm to the previous release
helm rollback daalu-automation
```

Helm rollback reverts the chart manifests; pods restart on
the previous image. Database migrations are *not* rolled
back automatically — they're append-only in the application's
mental model.

If a deploy broke because of a bad migration:

1. Pin the image to the previous SHA.
2. Manually downgrade the migration if needed.
3. Investigate without time pressure.

---

## When a release needs to be coordinated

Some changes need more care than "just deploy it":

- **Breaking API changes.** Coordinate with the frontend
  team; deploy in lockstep.
- **Schema changes that need a backfill.** Run the backfill
  before/after the deploy; have a feature flag for the new
  code path.
- **Changes that affect customers' federated edge or vLLM.**
  Customers need to upgrade their charts. Communicate via
  release notes + in-app banner.

For these, we treat the deploy as a release: write release
notes, schedule the window, coordinate.

---

## Release notes

For user-visible changes, write notes in the changelog
(e.g. `CHANGELOG.md`):

```markdown
## 2026-05-24

- **Settings page** — profile, preferences, API tokens, team
  management.
- **Help & Feedback page** — version info, feedback form.
- **Personal access tokens** — `dpat_…` tokens for scripts
  and CI.
- **/version endpoint** — returns the deployed commit SHA
  and build time.

### Fixes

- Monitoring: corrected `kube-prom-stack-*` resource names in
  the verification doc.
- daalu-api Service port is now named `http` so the
  ServiceMonitor can discover it.
```

These appear on the customer's *Help & Feedback* page in the
"Changelog" section.

---

## Semantic versioning for charts

The customer-side charts (`daalu-edge`, `daalu-vllm`) **do**
get semantic versions because customers pin them. Chart
version is updated in `Chart.yaml`:

- Patch — backwards-compatible fixes.
- Minor — new features, backwards-compatible.
- Major — breaking changes (rare).

Customers `helm upgrade` at their cadence.

---

## End

That's the end of the engineering manual. If a topic is
missing, file a PR adding the chapter. The manual is the
contract between the engineering team and itself — keeping
it current is everyone's job.

---

End of the *Engineering & Operations Manual*.

For the customer-facing companion, see the *Operator's
Handbook* in the sibling `docs/book-customer/` directory.
