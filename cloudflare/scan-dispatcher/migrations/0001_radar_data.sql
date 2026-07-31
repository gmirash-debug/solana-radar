CREATE TABLE IF NOT EXISTS state_docs (
  key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  source_updated_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
  run_key TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  lane TEXT,
  profile TEXT,
  lanes_scanned_json TEXT NOT NULL,
  stats_json TEXT NOT NULL,
  lane_stats_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_generated_at ON scan_runs (generated_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
  alert_key TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  token_key TEXT NOT NULL,
  pool_address TEXT,
  token_address TEXT,
  symbol TEXT,
  lane TEXT,
  signal_family TEXT,
  score REAL,
  tier TEXT,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_generated_at ON alerts (generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_token_key ON alerts (token_key, generated_at DESC);

CREATE TABLE IF NOT EXISTS deleted_tokens (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  token_address TEXT,
  pool_address TEXT,
  symbol TEXT,
  name TEXT,
  active INTEGER NOT NULL,
  deleted_at TEXT,
  restored_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deleted_tokens_active ON deleted_tokens (active, updated_at DESC);

CREATE TABLE IF NOT EXISTS discovery_state (
  token_key TEXT PRIMARY KEY,
  pool_address TEXT,
  market_json TEXT,
  baseline_json TEXT,
  queue_json TEXT,
  outcome_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_discovery_state_updated_at ON discovery_state (updated_at DESC, token_key ASC);
