-- Independent storage migrations; do not reuse the D1 migration sequence.
CREATE TABLE IF NOT EXISTS history_maintenance_dirty (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  ready_at TEXT NOT NULL,
  last_ready_at TEXT NOT NULL,
  source_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_maintenance_dirty_ready
  ON history_maintenance_dirty(ready_at, episode_id, event_id);
CREATE INDEX IF NOT EXISTS idx_history_maintenance_dirty_episode
  ON history_maintenance_dirty(episode_id, ready_at);

CREATE TABLE IF NOT EXISTS history_maintenance_jobs (
  job_day TEXT PRIMARY KEY,
  cutoff_at TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'baselines',
  state_json TEXT NOT NULL DEFAULT '{}',
  lease_token TEXT,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_maintenance_jobs_pending
  ON history_maintenance_jobs(completed_at, job_day);
