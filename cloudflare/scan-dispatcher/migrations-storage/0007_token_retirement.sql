-- Automatic retirement is independent from manual deletion and proven full sales.
CREATE TABLE IF NOT EXISTS token_retirements (
  token_address TEXT PRIMARY KEY,
  retired_at TEXT NOT NULL,
  reactivated_at TEXT,
  last_signal_at TEXT,
  reason TEXT NOT NULL CHECK(reason='below_20k_24h'),
  cleanup_before TEXT NOT NULL,
  cleanup_phase INTEGER NOT NULL DEFAULT 0,
  cleanup_episode TEXT,
  cleanup_pending INTEGER NOT NULL DEFAULT 1 CHECK(cleanup_pending IN (0,1)),
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_retirements_cleanup
  ON token_retirements(cleanup_pending,updated_at,token_address);
-- IDs only: an archived queue receipt cannot recreate a purged episode.
CREATE TABLE IF NOT EXISTS retired_episode_ids (
  episode_id TEXT PRIMARY KEY,
  token_address TEXT NOT NULL,
  retired_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retired_episodes_token ON retired_episode_ids(token_address,episode_id);
CREATE INDEX IF NOT EXISTS idx_runtime_sql_retired_token ON runtime_sql_documents(token_key,touched_at)
  WHERE token_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_all_episode
  ON history_sql_queue_events(episode_id,event_id);
CREATE INDEX IF NOT EXISTS idx_history_sql_queue_token_retirement ON history_sql_queue_events
  (json_extract(payload_json,'$.episode.token_address'),json_extract(payload_json,'$.episode.caught_at'))
  WHERE json_valid(payload_json);
CREATE INDEX IF NOT EXISTS idx_history_sql_cutover_token_retirement ON history_sql_cutover_events
  (json_extract(payload_json,'$.episode.token_address'),json_extract(payload_json,'$.episode.caught_at'))
  WHERE json_valid(payload_json);

CREATE TRIGGER IF NOT EXISTS token_retirement_episode_insert BEFORE INSERT ON signal_episodes
WHEN EXISTS (SELECT 1 FROM token_retirements r WHERE r.token_address=NEW.token_address
  AND (r.reactivated_at IS NULL OR julianday(NEW.caught_at)<=julianday(r.retired_at)))
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;
CREATE TRIGGER IF NOT EXISTS token_retirement_episode_update BEFORE UPDATE ON signal_episodes
WHEN EXISTS (SELECT 1 FROM token_retirements r WHERE r.token_address=NEW.token_address
  AND (r.reactivated_at IS NULL OR julianday(NEW.caught_at)<=julianday(r.retired_at)))
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_episode_events_insert BEFORE INSERT ON signal_episode_events
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_episode_events_update BEFORE UPDATE ON signal_episode_events
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_wallets_insert BEFORE INSERT ON signal_wallets
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_wallets_update BEFORE UPDATE ON signal_wallets
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_observations_insert BEFORE INSERT ON wallet_observations
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_observations_update BEFORE UPDATE ON wallet_observations
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_observation_bundles_insert BEFORE INSERT ON wallet_observation_bundles
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_observation_bundles_update BEFORE UPDATE ON wallet_observation_bundles
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_outcomes_insert BEFORE INSERT ON signal_outcomes
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_signal_outcomes_update BEFORE UPDATE ON signal_outcomes
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_cluster_edge_evidence_insert BEFORE INSERT ON wallet_cluster_edge_evidence
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_wallet_cluster_edge_evidence_update BEFORE UPDATE ON wallet_cluster_edge_evidence
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT,'token_episode_retired'); END;

CREATE TRIGGER IF NOT EXISTS token_retirement_queue_insert BEFORE INSERT ON history_sql_queue_events
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
  OR (json_valid(NEW.payload_json) AND EXISTS(SELECT 1 FROM token_retirements r
    WHERE r.token_address=json_extract(NEW.payload_json,'$.episode.token_address')
      AND (r.reactivated_at IS NULL OR julianday(json_extract(NEW.payload_json,'$.episode.caught_at'))<=julianday(r.retired_at))))
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS token_retirement_cutover_insert BEFORE INSERT ON history_sql_cutover_events
WHEN EXISTS (SELECT 1 FROM retired_episode_ids r WHERE r.episode_id=NEW.episode_id)
  OR (json_valid(NEW.payload_json) AND EXISTS(SELECT 1 FROM token_retirements r
    WHERE r.token_address=json_extract(NEW.payload_json,'$.episode.token_address')
      AND (r.reactivated_at IS NULL OR julianday(json_extract(NEW.payload_json,'$.episode.caught_at'))<=julianday(r.retired_at))))
BEGIN SELECT RAISE(IGNORE); END;
