# 65. Database migration patterns

The migrations that don't break production share a small set
of patterns. This chapter catalogs them.

Chapter 50 covered the *operational* side. This is the
*authoring* side.

---

## Pattern 1: additive-only

Always prefer adding to changing. New column, new table, new
index — these are nearly always safe.

```python
def upgrade():
    op.add_column("users", sa.Column("preferences", JSONB, nullable=False, server_default="{}"))
```

Why this is safe: existing rows get the default; the column
appears with no migration of existing data needed.

What to watch: very wide tables can have slow ALTER TABLE
even for additive columns. PG 11+ has fast-path for adding
nullable + non-default columns; defaults still require a
rewrite on older versions.

---

## Pattern 2: NOT NULL with a sensible default

If the column needs to be NOT NULL, give it a default to
avoid a backfill step:

```python
def upgrade():
    op.add_column("users", sa.Column(
        "preferences", JSONB,
        nullable=False,
        server_default="{}",
    ))
```

The `server_default` is applied to existing rows. New
inserts that don't specify the column get the default too.

Avoid `default=` (Python-level) for NOT NULL columns — it
doesn't help with existing rows.

---

## Pattern 3: rename through dual-write

To rename `old_col` → `new_col`:

1. **Migration A:** add `new_col`. Code dual-writes to both.
2. **Migration B:** backfill `new_col` from `old_col`.
3. **Migration C:** switch reads to `new_col`. Keep writes
   dual.
4. **Migration D:** drop `old_col`. Switch writes to
   `new_col` only.

Four deploys, never a single one that could break either
direction.

This is overkill for cosmetic renames. For column data we
care about, it's the right level of safety.

---

## Pattern 4: drop column in two phases

1. **Migration A:** stop writing to and reading from the
   column.
2. **Deploy + verify.** Wait at least one release to confirm
   no client code paths still reference it.
3. **Migration B:** drop the column.

You can't `git revert` a `DROP COLUMN` — the data is gone.
The two-phase approach gives you a release-cycle's worth of
safety net.

---

## Pattern 5: CREATE INDEX CONCURRENTLY

```python
def upgrade():
    op.execute("CREATE INDEX CONCURRENTLY ix_alerts_tenant_severity ON alerts (tenant_id, severity)")
```

Plain `CREATE INDEX` takes an ACCESS EXCLUSIVE lock for the
duration. On large tables that's minutes. `CONCURRENTLY`
gives you a slower but lock-free creation.

Caveats:

- `CONCURRENTLY` can't be in a transaction. Alembic wraps
  migrations in transactions by default — disable per
  migration:

  ```python
  def upgrade():
      with op.get_context().autocommit_block():
          op.execute("CREATE INDEX CONCURRENTLY ...")
  ```

- If `CONCURRENTLY` fails partway, the index is left in
  `INVALID` state. Drop and retry.

---

## Pattern 6: JSONB for volatile shape

When the schema is going to evolve fast, use JSONB:

```python
preferences: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
```

JSONB queries are slower than typed columns but the
operational savings (no migration per shape change) outweigh
the cost for low-volume data.

The rule: tabular query patterns → typed columns. Document
storage → JSONB.

---

## Pattern 7: enum as varchar

We don't use Postgres ENUM types. Each "enum" column is a
`varchar` with a CHECK constraint (or a Python-side
constraint enforced by SQLAlchemy):

```python
status: Mapped[str] = mapped_column(String(32), nullable=False)
```

Why: Postgres ENUMs require special migration syntax to
add values (`ALTER TYPE ... ADD VALUE`); varchars are just
varchars. Trades a small validation cost for a lot of
migration simplicity.

---

## Pattern 8: feature flag, then drop

When introducing a behavior change that affects existing
data:

1. Add the new code path behind a feature flag (in
   `settings.py` or `feature_flags.py`).
2. Migrate data in a Celery task.
3. Switch the flag on per-tenant.
4. After all tenants are on the new path, remove the flag
   and the old code.

This is heavier than a straight code change, but it lets
you roll out gradually and catch issues per-tenant.

---

## Pattern 9: every migration has a downgrade

```python
def upgrade():
    op.add_column("users", sa.Column("preferences", JSONB))

def downgrade():
    op.drop_column("users", "preferences")
```

We don't always run downgrades in production, but they
serve two roles:

- Local dev: rolling back to test interactions.
- Documentation: makes the migration's effect explicit.

---

## What's next

Chapter 66 — cutting a release.
