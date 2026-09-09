-- raw の最新ハッシュだけを取り出し、巨大な payload の読み込みを避ける。
BEGIN;
CREATE INDEX idx_events_raw_archive_latest
    ON events (source_uri, event_type, known_at DESC, created_at DESC, id DESC)
    INCLUDE (payload_hash)
    WHERE source_uri IS NOT NULL AND payload_hash IS NOT NULL;
COMMIT;
