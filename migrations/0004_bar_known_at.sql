-- 0004_bar_known_at.sql
-- market_bars.known_at moves onto this system's clock (real UTC) while
-- start_at / end_at stay on the broker's. See docs/adr/ADR-005.
--
-- OANDA Japan's MT5 server runs UTC+3 and labels its timestamps UTC, so a bar
-- observed at 03:41 real UTC closes at 06:41 in broker time. The old check
-- read the two columns as one clock and now rejects every correct row.

BEGIN;

ALTER TABLE market_bars DROP CONSTRAINT market_bars_known_at_check;

COMMIT;
