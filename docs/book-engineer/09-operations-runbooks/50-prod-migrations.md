# 50. Database migrations in production

Migrations are the most-likely-to-cause-an-outage operation
we do routinely. This chapter is the discipline for not
breaking production with a migration.

---

## The default path

Migrations are applied automatically by the
`alembic-upgrade` init container in the daalu-api Deployment.
Each new image with a new migration runs the migration as
part of pod startup.

Pros:

- Migration runs once per deploy, atomically.
- Pod won't become Ready until migration succeeds.

Cons:

- A slow migration delays rollouts.
- A failing migration blocks the deploy.

For most migrations this is fine. For large or sensitive
ones, the discipline below applies.

---

## Categories of migration risk

### Low risk

- Adding a nullable column.
- Adding a table.
- Adding an index `CONCURRENTLY` (Postgres).
- Adding an enum value.

These rarely cause issues. Run as part of normal deploy.

### Medium risk

- Adding a non-nullable column with a default.
- Renaming a column (do it in two phases — add + dual-write,
  then drop).
- Dropping an unused index.

Test these carefully on staging. They're usually fine but
have failure modes.

### High risk

- Altering a column's type.
- Adding NOT NULL to an existing column.
- Long-running data backfills.
- Dropping a column (always do a deploy-without-the-column-
  in-code first, then drop).

These get the "staged migration" treatment below.

---

## Staged migration playbook

For high-risk migrations:

1. **Plan.** Write down the steps. Identify which can run
   without the init container.
2. **Test on a copy of prod.** `pg_dump` last night's
   prod snapshot, restore to a scratch instance, apply
   the migration, time it.
3. **Disable the init container for this deploy.** Set
   `api.runAlembicInitContainer=false`.
4. **Deploy.** The new code can be running against the old
   schema for as long as compatibility allows.
5. **Run the migration manually.** Either via `kubectl
   exec daalu-postgres -- psql` or via `daalu db
   upgrade` from a temporary pod.
6. **Re-enable the init container.** Next deploy gets it
   back.

---

## Backfill jobs

Long backfills (e.g., "encrypt every existing user's
preferences blob with the new KEK") don't belong in
migrations. They:

- Block deploys.
- Don't have observability.
- Can't be paused/resumed.

Instead, write a Celery task that does the backfill in
batches:

```python
@celery_app.task(name="daalu.backfill.encrypt_preferences", bind=True)
def encrypt_preferences_batch(self, batch_size=100):
    ...
```

Kick it off via `daalu admin trigger-backfill`. Monitor with
the standard task metrics.

---

## Locking

Postgres takes various locks during migrations. The dangerous
ones:

- `ALTER TABLE` without `IF NOT EXISTS` clause → ACCESS
  EXCLUSIVE lock. Blocks reads.
- Adding a non-nullable column with a default → table
  rewrite on older Postgres versions (PG 11+ has fast path).
- `CREATE INDEX` without `CONCURRENTLY` → AccessExclusive on
  index.

Best practices:

- Always use `CONCURRENTLY` for indexes in production.
- Always check `lock_timeout` is set; we configure
  `lock_timeout = '5s'` so a migration that can't get its
  lock fails fast rather than blocking traffic for minutes.
- Run during low-traffic windows when possible.

---

## Rollback

Every migration has a `downgrade()`. We test it.

In production, downgrading is rare — usually we roll forward
with a fix. But for the small set of cases where rollback is
right:

```bash
# from a temporary pod
daalu db downgrade -1
```

The CLI calls Alembic; same as `alembic downgrade -1`.

---

## The "no downgrades that lose data" rule

A downgrade that drops a column drops user data. We avoid
this by:

- Writing add-column migrations whose `downgrade()` only
  drops if the column is empty (defensive).
- Promoting "drop column" migrations through a deprecation
  step — first stop writing, then drop in a later release.

The downgrade chain still works for dev/test purposes; in
production we don't rely on it for data preservation.

---

## What's next

Chapter 51 — tenant operations.
