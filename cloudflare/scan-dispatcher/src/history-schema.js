export const HISTORY_SCHEMA_RELEASE = "0003_resumable_history.sql";

// Release-specific and canonical: tested byte-for-byte against migration 0003.
// Do not accept SQL, migration names or later versions from request input.
export const HISTORY_SCHEMA_SQL = `-- Additive: retain raw events, edge evidence, and historical cluster identities.
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
`;

const MARKER_SQL = "SELECT name FROM d1_migrations WHERE name=?1 LIMIT 1";
const TRACKING_SQL = "INSERT INTO d1_migrations(name) VALUES('0003_resumable_history.sql')";

function schemaState(ready, extra = {}) {
  return {enabled:true, migration:HISTORY_SCHEMA_RELEASE, ready,
    status:ready ? "ready" : "pending", error:ready ? null : "history_schema_upgrade_pending", ...extra};
}

function failureState(error) {
  const cause=String(error?.message || error).slice(0,1000);
  const quota=/\b7500\b|(?:daily|free).*?(?:write|quota|limit)|(?:write|quota|limit).*?daily/i.test(cause);
  return schemaState(false,{status:quota ? "pending" : "failed",
    error:quota ? "history_schema_upgrade_pending" : "history_schema_upgrade_failed",cause});
}

// Without this exact opt-in, preserve the normal migration-first contract and
// add no D1 queries. Only the five-minute cron/authenticated manual flush upgrade.
export async function historySchemaState(env, {upgrade=false} = {}) {
  if (env.HISTORY_SCHEMA_AUTO_UPGRADE === undefined) return {enabled:false,ready:true};
  if (env.HISTORY_SCHEMA_AUTO_UPGRADE !== HISTORY_SCHEMA_RELEASE) {
    return schemaState(false,{status:"failed",error:"history_schema_auto_upgrade_invalid_release"});
  }
  const db=env.RADAR_HISTORY_DB;
  if (typeof db?.prepare !== "function") return failureState("history_db_not_configured");
  const marker=async()=>Boolean(await db.prepare(MARKER_SQL).bind(HISTORY_SCHEMA_RELEASE).first());
  try {
    if (await marker()) return schemaState(true);
  } catch (error) { return failureState(error); }
  if (!upgrade) return schemaState(false);
  try {
    const statements=HISTORY_SCHEMA_SQL.split(";").map(sql=>sql.trim()).filter(Boolean);
    const results=await db.batch([...statements.map(sql=>db.prepare(sql)),db.prepare(TRACKING_SQL)]);
    if (!Array.isArray(results) || results.length!==statements.length+1 || results.some(row=>row?.success!==true)) {
      throw new Error("history_schema_upgrade_batch_incomplete");
    }
    return schemaState(true,{applied:true});
  } catch (error) {
    // Another cron/operator can commit between our marker read and batch.
    // Never replay ALTER blindly after a failure or manufacture a marker.
    try { if (await marker()) return schemaState(true,{concurrent_commit:true}); }
    catch (checkError) {
      return {...failureState(error),marker_check_error:String(checkError?.message || checkError).slice(0,1000)};
    }
    return failureState(error);
  }
}
