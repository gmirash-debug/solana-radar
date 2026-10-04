-- Apply to the canonical Turso database, not to the legacy Durable Object.
CREATE TABLE IF NOT EXISTS history_sql_queue_meta (
  id INTEGER PRIMARY KEY CHECK(id=1),
  pending_rows INTEGER NOT NULL DEFAULT 0,
  pending_bytes INTEGER NOT NULL DEFAULT 0,
  delivered_rows INTEGER NOT NULL DEFAULT 0,
  archive_pending_rows INTEGER NOT NULL DEFAULT 0,
  archive_pending_bytes INTEGER NOT NULL DEFAULT 0,
  max_pending_rows INTEGER NOT NULL DEFAULT 2048,
  max_pending_bytes INTEGER NOT NULL DEFAULT 16777216,
  max_receipts INTEGER NOT NULL DEFAULT 160000,
  hist_day TEXT NOT NULL DEFAULT '', hist_writes INTEGER NOT NULL DEFAULT 0,
  archive_day TEXT NOT NULL DEFAULT '', archive_writes INTEGER NOT NULL DEFAULT 0,
  archive_reads INTEGER NOT NULL DEFAULT 0, archive_write_bytes INTEGER NOT NULL DEFAULT 0,
  last_flush_at TEXT, last_flush_error TEXT, last_flush_delivered INTEGER NOT NULL DEFAULT 0,
  legacy_cursor TEXT NOT NULL DEFAULT '', legacy_complete INTEGER NOT NULL DEFAULT 0,
  legacy_imported INTEGER NOT NULL DEFAULT 0,
  CHECK(pending_rows>=0 AND pending_bytes>=0 AND delivered_rows>=0 AND hist_writes>=0
    AND archive_pending_rows>=0 AND archive_pending_bytes>=0)
);
INSERT OR IGNORE INTO history_sql_queue_meta(id) VALUES (1);

CREATE TABLE IF NOT EXISTS history_sql_queue_events (
  event_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, source_at INTEGER NOT NULL,
  payload_json TEXT, payload_bytes INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','delivered')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
  lease_token TEXT, lease_until INTEGER, delivered_at INTEGER, last_error TEXT,
  progress_json TEXT, archive_version INTEGER NOT NULL DEFAULT 0,
  archive_pending INTEGER NOT NULL DEFAULT 0 CHECK(archive_pending IN (0,1)), archive_ref_json TEXT,
  archive_lease_token TEXT, archive_lease_until INTEGER, archive_attempts INTEGER NOT NULL DEFAULT 0,
  archive_next_attempt_at INTEGER NOT NULL DEFAULT 0, archive_error TEXT,
  budget_day TEXT, budget_reserved INTEGER NOT NULL DEFAULT 0,
  CHECK(payload_bytes>=0),
  CHECK((status!='pending' AND archive_pending=0) OR payload_json IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_due
  ON history_sql_queue_events(next_attempt_at,source_at,event_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_episode
  ON history_sql_queue_events(episode_id,source_at,event_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_oldest
  ON history_sql_queue_events(source_at,event_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_receipts
  ON history_sql_queue_events(delivered_at,event_id) WHERE status='delivered' AND archive_pending=0;
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_archive_due
  ON history_sql_queue_events(archive_next_attempt_at,event_id) WHERE archive_pending=1;

-- Capacity checks and counters participate in the same short enqueue/finish
-- transaction. A mixed batch either commits in full or leaves no accepted rows.
CREATE TRIGGER IF NOT EXISTS history_sql_queue_capacity BEFORE INSERT ON history_sql_queue_events
BEGIN
  SELECT CASE WHEN NEW.status='pending' AND EXISTS (
    SELECT 1 FROM history_sql_queue_meta WHERE id=1 AND
      (pending_rows+archive_pending_rows+1>max_pending_rows
        OR pending_bytes+archive_pending_bytes+NEW.payload_bytes>max_pending_bytes)
  ) THEN RAISE(ABORT,'history_queue_pending_capacity') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM history_sql_queue_meta WHERE id=1 AND pending_rows+delivered_rows+1>max_receipts
  ) THEN RAISE(ABORT,'history_queue_receipt_capacity') END;
END;
CREATE TRIGGER IF NOT EXISTS history_sql_queue_insert AFTER INSERT ON history_sql_queue_events
BEGIN
  UPDATE history_sql_queue_meta SET
    pending_rows=pending_rows+(NEW.status='pending'),
    pending_bytes=pending_bytes+CASE WHEN NEW.status='pending' THEN NEW.payload_bytes ELSE 0 END,
    delivered_rows=delivered_rows+(NEW.status='delivered'),
    archive_pending_rows=archive_pending_rows+NEW.archive_pending,
    archive_pending_bytes=archive_pending_bytes+NEW.archive_pending*NEW.payload_bytes WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS history_sql_queue_update AFTER UPDATE OF status,payload_bytes,archive_pending ON history_sql_queue_events
BEGIN
  UPDATE history_sql_queue_meta SET
    pending_rows=pending_rows+(NEW.status='pending')-(OLD.status='pending'),
    pending_bytes=pending_bytes+CASE WHEN NEW.status='pending' THEN NEW.payload_bytes ELSE 0 END
      -CASE WHEN OLD.status='pending' THEN OLD.payload_bytes ELSE 0 END,
    delivered_rows=delivered_rows+(NEW.status='delivered')-(OLD.status='delivered'),
    archive_pending_rows=archive_pending_rows+NEW.archive_pending-OLD.archive_pending,
    archive_pending_bytes=archive_pending_bytes+NEW.archive_pending*NEW.payload_bytes
      -OLD.archive_pending*OLD.payload_bytes WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS history_sql_queue_delete AFTER DELETE ON history_sql_queue_events
BEGIN
  UPDATE history_sql_queue_meta SET pending_rows=pending_rows-(OLD.status='pending'),
    pending_bytes=pending_bytes-CASE WHEN OLD.status='pending' THEN OLD.payload_bytes ELSE 0 END,
    delivered_rows=delivered_rows-(OLD.status='delivered'),
    archive_pending_rows=archive_pending_rows-OLD.archive_pending,
    archive_pending_bytes=archive_pending_bytes-OLD.archive_pending*OLD.payload_bytes WHERE id=1;
END;
