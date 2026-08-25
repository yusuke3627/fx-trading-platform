# Integration tests

What needs a real PostgreSQL rather than a fake: the claim protocol's
`FOR UPDATE SKIP LOCKED`, JSONB round-trips, visibility windows, and the column
lists every row mapper reads. A fake answers from the objects it was handed, so
a column missing from a SELECT only ever surfaces against a database.

```bash
export TRADING_DB_DSN=postgresql://localhost/trading_test
for f in migrations/*.sql; do psql "$TRADING_DB_DSN" -v ON_ERROR_STOP=1 -f "$f"; done
pytest tests/integration
```

Without `TRADING_DB_DSN` these skip themselves. CI connects to the database in
a step of its own before running them, because a skipped suite is green and
would hide a broken DSN just as well as a working one.

Use a database of their own. Most tests scope their rows by a throwaway
identifier and delete them afterwards, but the command tests empty
`execution_commands` first: `claim_next()` takes the oldest READY row in the
whole table and cannot be scoped to one test's data.
