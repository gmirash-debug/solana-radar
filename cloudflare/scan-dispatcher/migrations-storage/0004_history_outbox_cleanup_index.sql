-- Cover the delivered-only keyset before loading either potentially large JSON
-- document. This table belongs to the canonical operational/history database.
CREATE INDEX IF NOT EXISTS idx_history_outbox_delivered_keyset
  ON history_outbox(event_id,delivered_at) WHERE status='delivered';
