-- Additive: retain raw events, edge evidence, and historical cluster identities.
ALTER TABLE wallet_clusters ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE wallet_clusters ADD COLUMN retired_at TEXT;
ALTER TABLE wallet_clusters ADD COLUMN replaced_by TEXT;
ALTER TABLE wallet_cluster_edges ADD COLUMN is_infrastructure INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signal_outcomes ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE signal_outcomes ADD COLUMN entry_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signal_wallets ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE wallet_observations ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE wallet_scores ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE market_baselines ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE wallet_clusters ADD COLUMN numeric_contract_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS history_cluster_work (
  job_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  component TEXT,
  visited INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, wallet_address)
);
CREATE INDEX IF NOT EXISTS idx_history_cluster_frontier
  ON history_cluster_work(job_id, component, visited, wallet_address);
CREATE TABLE IF NOT EXISTS history_cluster_lock (
  id INTEGER PRIMARY KEY CHECK(id=1),
  job_id TEXT
);
INSERT OR IGNORE INTO history_cluster_lock(id) VALUES (1);

-- One immutable observation bundle per event avoids rewriting frozen catch
-- rows on every retention check. Public readers union this with legacy rows.
CREATE TABLE IF NOT EXISTS wallet_observation_bundles (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  observations_json TEXT NOT NULL,
  numeric_contract_version INTEGER NOT NULL DEFAULT 2,
  FOREIGN KEY (episode_id) REFERENCES signal_episodes(episode_id)
);
CREATE INDEX IF NOT EXISTS idx_wallet_observation_bundles_episode
  ON wallet_observation_bundles(episode_id, observed_at DESC);
