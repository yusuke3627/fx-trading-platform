# Integration tests

PostgreSQL-backed tests (claim protocol with FOR UPDATE SKIP LOCKED, fill
idempotency, snapshot queries). They require a database:

```bash
export TRADING_DB_DSN=postgresql://localhost/trading_test
for f in migrations/*.sql; do psql "$TRADING_DB_DSN" -v ON_ERROR_STOP=1 -f "$f"; done
pytest tests/integration
```

Added together with the first vertical slice.
