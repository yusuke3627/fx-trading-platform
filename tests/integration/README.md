# Integration tests

PostgreSQL-backed tests (claim protocol with FOR UPDATE SKIP LOCKED, fill
idempotency, snapshot queries). They require a database:

```bash
export TRADING_DB_DSN=postgresql://localhost/trading_test
psql "$TRADING_DB_DSN" -f migrations/0001_initial.sql
pytest tests/integration
```

Added together with the first vertical slice.
