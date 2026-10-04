CREATE TABLE IF NOT EXISTS history_sql_cutover_meta (
  id INTEGER PRIMARY KEY CHECK(id=1),
  pending_rows INTEGER NOT NULL DEFAULT 0,
  pending_bytes INTEGER NOT NULL DEFAULT 0,
  CHECK(pending_rows>=0 AND pending_bytes>=0)
);
INSERT OR IGNORE INTO history_sql_cutover_meta(id) VALUES(1);
CREATE TABLE IF NOT EXISTS history_sql_cutover_events (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  source_at INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  payload_bytes INTEGER NOT NULL CHECK(payload_bytes>=0)
);
CREATE INDEX IF NOT EXISTS idx_history_sql_cutover_order
  ON history_sql_cutover_events(source_at,event_id);
CREATE TRIGGER IF NOT EXISTS history_sql_cutover_capacity BEFORE INSERT ON history_sql_cutover_events
WHEN NOT EXISTS(SELECT 1 FROM history_sql_cutover_events WHERE event_id=NEW.event_id)
BEGIN
  SELECT CASE WHEN EXISTS(SELECT 1 FROM history_sql_cutover_meta WHERE id=1
    AND (pending_rows+1>2048 OR pending_bytes+NEW.payload_bytes>16777216))
    THEN RAISE(ABORT,'history_cutover_capacity') END;
END;
CREATE TRIGGER IF NOT EXISTS history_sql_cutover_insert AFTER INSERT ON history_sql_cutover_events
BEGIN
  UPDATE history_sql_cutover_meta SET pending_rows=pending_rows+1,
    pending_bytes=pending_bytes+NEW.payload_bytes WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS history_sql_cutover_delete AFTER DELETE ON history_sql_cutover_events
BEGIN
  UPDATE history_sql_cutover_meta SET pending_rows=pending_rows-1,
    pending_bytes=pending_bytes-OLD.payload_bytes WHERE id=1;
END;
