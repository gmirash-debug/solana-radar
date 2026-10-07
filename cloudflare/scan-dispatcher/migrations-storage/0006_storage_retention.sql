-- Keep bounded retention pages and source-run dependency checks indexed.
CREATE INDEX IF NOT EXISTS idx_history_outbox_delivered_retention
  ON history_outbox(delivered_at,event_id) WHERE status='delivered';
CREATE INDEX IF NOT EXISTS idx_signal_episodes_source_run_key
  ON signal_episodes(source_run_key) WHERE source_run_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_episodes_closed_retention
  ON signal_episodes(closed_at,episode_id) WHERE closed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_retention_episode
  ON history_sql_queue_events(episode_id,status,archive_pending);
CREATE INDEX IF NOT EXISTS idx_history_sql_cutover_retention_episode
  ON history_sql_cutover_events(episode_id);

-- The marker and all dependent deletes share one atomic batch. A marker that
-- survives the parent delete is made invalid, forcing the entire batch back.
CREATE TABLE IF NOT EXISTS history_episode_retirement_work (
  episode_id TEXT PRIMARY KEY REFERENCES signal_episodes(episode_id) ON DELETE CASCADE,
  token TEXT NOT NULL CHECK(length(token)>0)
);
