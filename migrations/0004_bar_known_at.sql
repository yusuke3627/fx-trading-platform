-- 0004_bar_known_at.sql
-- market_bars.known_at moves onto this system's clock (real UTC) while
-- start_at / end_at stay on the broker's. See docs/adr/ADR-005.
--
-- OANDA Japan's MT5 server runs UTC+3 and labels its timestamps UTC, so a bar
-- observed at 03:41 real UTC closes at 06:41 in broker time. The old check
-- read the two columns as one clock and now rejects every correct row.

BEGIN;

ALTER TABLE market_bars DROP CONSTRAINT market_bars_known_at_check;

-- Rows written before this migration hold known_at = end_at, a broker-clock
-- value that would now be read as real UTC and hide every one of them for the
-- length of the offset. Nothing in the row recovers the reception time the
-- column now means, so they are deleted rather than reinterpreted: bars are
-- derived data, and market_ticks — the series kept permanently — rebuilds
-- them.
DELETE FROM market_bars;

COMMIT;
