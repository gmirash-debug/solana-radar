PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS signal_episodes (
  episode_id TEXT PRIMARY KEY,
  token_address TEXT NOT NULL,
  pool_address TEXT,
  symbol TEXT,
  name TEXT,
  lane TEXT NOT NULL,
  signal_family TEXT NOT NULL,
  caught_at TEXT NOT NULL,
  last_signal_at TEXT NOT NULL,
  closed_at TEXT,
  caught_tier TEXT,
  caught_score REAL,
  caught_price_usd REAL,
  caught_mcap_usd REAL,
  caught_liquidity_usd REAL,
  token_age_days REAL,
  ath_mcap_usd REAL,
  ath_ratio REAL,
  market_stage TEXT,
  mcap_band TEXT NOT NULL,
  liquidity_band TEXT NOT NULL,
  age_band TEXT NOT NULL,
  source_run_key TEXT,
  source_kind TEXT NOT NULL DEFAULT 'live',
  data_quality_status TEXT NOT NULL DEFAULT 'partial',
  schema_version INTEGER NOT NULL DEFAULT 1,
  raw_object_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_episodes_token_caught
  ON signal_episodes (token_address, caught_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_episodes_caught
  ON signal_episodes (caught_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_episodes_baseline
  ON signal_episodes (mcap_band, liquidity_band, age_band, signal_family, caught_at DESC);

CREATE TABLE IF NOT EXISTS signal_episode_events (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  tier TEXT,
  score REAL,
  price_usd REAL,
  mcap_usd REAL,
  liquidity_usd REAL,
  retained_supply_pct REAL,
  cohort_retained_pct REAL,
  thesis_status TEXT,
  data_quality_status TEXT,
  payload_json TEXT,
  raw_object_key TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_episode_events_episode_time
  ON signal_episode_events (episode_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS signal_wallets (
  episode_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  cohort_role TEXT NOT NULL DEFAULT 'at_catch',
  wallet_class_at_signal TEXT,
  first_buy_at TEXT,
  last_buy_at TEXT,
  buy_count INTEGER,
  buy_sol REAL,
  bought_tokens REAL,
  average_entry_price REAL,
  entry_mcap_usd REAL,
  supply_pct_bought REAL,
  held_tokens_at_catch REAL,
  held_supply_pct_at_catch REAL,
  retained_pct_at_catch REAL,
  common_funder TEXT,
  common_executor TEXT,
  cluster_id_at_catch TEXT,
  prior_edge_score REAL,
  prior_edge_confidence TEXT,
  prior_episode_count INTEGER,
  prior_score_computed_through TEXT,
  evidence_status TEXT NOT NULL DEFAULT 'partial',
  raw_object_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (episode_id, wallet_address, cohort_role),
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_wallets_wallet
  ON signal_wallets (wallet_address, episode_id);
CREATE INDEX IF NOT EXISTS idx_signal_wallets_episode_role
  ON signal_wallets (episode_id, cohort_role);

CREATE TABLE IF NOT EXISTS wallet_observations (
  episode_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  current_token_balance REAL,
  balance_retained_pct REAL,
  additional_buy_tokens REAL,
  outbound_transfer_tokens REAL,
  behavior_status TEXT NOT NULL DEFAULT 'unknown',
  estimated_pnl_pct REAL,
  estimated_pnl_sol REAL,
  coverage_status TEXT NOT NULL DEFAULT 'partial',
  raw_object_key TEXT,
  PRIMARY KEY (episode_id, wallet_address, observed_at),
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wallet_observations_wallet
  ON wallet_observations (wallet_address, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_observations_episode
  ON wallet_observations (episode_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS signal_outcomes (
  episode_id TEXT NOT NULL,
  horizon_minutes INTEGER NOT NULL,
  due_at TEXT NOT NULL,
  evaluated_at TEXT,
  endpoint_price_usd REAL,
  endpoint_mcap_usd REAL,
  endpoint_liquidity_usd REAL,
  return_pct REAL,
  max_return_pct REAL,
  max_drawdown_pct REAL,
  time_to_1_5x_minutes REAL,
  time_to_2x_minutes REAL,
  time_to_5x_minutes REAL,
  hit_1_5x INTEGER,
  hit_2x INTEGER,
  hit_5x INTEGER,
  tradable_2x INTEGER,
  market_data_coverage_pct REAL,
  largest_gap_minutes REAL,
  status TEXT NOT NULL DEFAULT 'pending',
  source TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (episode_id, horizon_minutes),
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_due
  ON signal_outcomes (status, due_at);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_horizon
  ON signal_outcomes (horizon_minutes, status, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS market_baselines (
  baseline_key TEXT PRIMARY KEY,
  mcap_band TEXT NOT NULL,
  liquidity_band TEXT NOT NULL,
  age_band TEXT NOT NULL,
  signal_family TEXT NOT NULL,
  horizon_minutes INTEGER NOT NULL,
  eligible_episodes INTEGER NOT NULL,
  hit_1_5x_rate REAL,
  hit_2x_rate REAL,
  hit_5x_rate REAL,
  median_max_return_pct REAL,
  median_drawdown_pct REAL,
  computed_through TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_baselines_lookup
  ON market_baselines (mcap_band, liquidity_band, age_band, signal_family, horizon_minutes);

CREATE TABLE IF NOT EXISTS wallet_scores (
  wallet_address TEXT PRIMARY KEY,
  eligible_episodes INTEGER NOT NULL DEFAULT 0,
  distinct_tokens INTEGER NOT NULL DEFAULT 0,
  wins_1_5x_72h INTEGER NOT NULL DEFAULT 0,
  wins_2x_72h INTEGER NOT NULL DEFAULT 0,
  wins_5x_7d INTEGER NOT NULL DEFAULT 0,
  raw_hit_rate_2x REAL,
  baseline_hit_rate_2x REAL,
  bayesian_hit_rate_2x REAL,
  lift_2x REAL,
  median_lead_minutes REAL,
  median_retained_24h REAL,
  median_max_return_72h REAL,
  median_drawdown_72h REAL,
  edge_score REAL NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'unproven',
  first_seen_at TEXT,
  last_seen_at TEXT,
  computed_through TEXT NOT NULL,
  score_version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallet_scores_rank
  ON wallet_scores (confidence, edge_score DESC, lift_2x DESC, eligible_episodes DESC);

CREATE TABLE IF NOT EXISTS wallet_cluster_edges (
  edge_id TEXT PRIMARY KEY,
  wallet_a TEXT NOT NULL,
  wallet_b TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0,
  evidence_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cluster_edges_wallet_a
  ON wallet_cluster_edges (wallet_a, weight DESC);
CREATE INDEX IF NOT EXISTS idx_cluster_edges_wallet_b
  ON wallet_cluster_edges (wallet_b, weight DESC);

CREATE TABLE IF NOT EXISTS wallet_clusters (
  cluster_id TEXT PRIMARY KEY,
  confidence TEXT NOT NULL DEFAULT 'unproven',
  wallet_count INTEGER NOT NULL DEFAULT 0,
  eligible_episodes INTEGER NOT NULL DEFAULT 0,
  wins_2x_72h INTEGER NOT NULL DEFAULT 0,
  lift_2x REAL,
  edge_score REAL NOT NULL DEFAULT 0,
  relation_types_json TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  computed_through TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_cluster_members (
  cluster_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  membership_weight REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (cluster_id, wallet_address),
  FOREIGN KEY (cluster_id) REFERENCES wallet_clusters(cluster_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cluster_members_wallet
  ON wallet_cluster_members (wallet_address, cluster_id);
