import {historyEventEffects, historyEventId, normalizedEpisode, OUTCOME_HORIZONS,
  refreshMarketBaselines, refreshWalletScores} from "./history.js";
import {readHistoryArchive, validateHistoryArchiveReference} from "./archive.js";

const PAGE = 25;
const LEASE_MS = 120_000;
const MAX_QUERIES = 100;

class MaintenanceYield extends Error {
  constructor(reason) { super(reason); this.deferred = true; }
}

function timestamp(value) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) throw new Error("history_maintenance_time_invalid");
  return new Date(parsed).toISOString();
}

function bound(value, fallback, maximum) {
  const result = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(result) || result < 0 || result > maximum) {
    throw new Error("history_maintenance_budget_invalid");
  }
  return result;
}

export function historyDerivedMode(env) {
  const mode = env?.HISTORY_DERIVED_MODE || "per_event";
  if (!["per_event", "daily"].includes(mode)) throw new Error("history_derived_mode_invalid");
  return mode;
}

// A compact marker is committed before an event can be acknowledged. Replays
// never move its first readiness date or rewrite the frozen original cohort.
export async function markHistoryDerivedDirty(db, raw, now) {
  const effects = historyEventEffects(raw?.event?.event_type);
  if (!effects.refreshesScores && !effects.refreshesClusters) return false;
  const ready = timestamp(now);
  const episode = normalizedEpisode(raw.episode, ready);
  if (!episode) throw new Error("history_episode_token_required");
  await db.prepare(`INSERT INTO history_maintenance_dirty
    (event_id,episode_id,ready_at,last_ready_at,source_at) VALUES (?1,?2,?3,?3,?4)
    ON CONFLICT(event_id) DO UPDATE SET last_ready_at=excluded.last_ready_at
      WHERE excluded.last_ready_at>history_maintenance_dirty.last_ready_at`)
    .bind(historyEventId(raw), episode.episode_id, ready, timestamp(raw.event.observed_at || ready)).run();
  return true;
}

function budgetDb(db, options) {
  const usage = {queries:0, writes:0};
  const requireBudget = (queries, writes = 0) => {
    if (usage.queries + queries > options.maxQueries || usage.writes + writes > options.maxWrites) {
      throw new MaintenanceYield("history_maintenance_budget");
    }
  };
  const reserve = (queries, writes) => {
    requireBudget(queries, writes);
    options.onQuery?.(queries);
    if (writes) options.onWrite?.(writes);
    usage.queries += queries;
    usage.writes += writes;
  };
  const checked = result => {
    if (result?.success === false) throw new Error(result.error || "history_maintenance_sql_failed");
    return result;
  };
  const wrap = (statement, writes) => ({
    statement,
    bind(...values) { return wrap(statement.bind(...values), writes); },
    async all() { reserve(1,writes); return checked(await statement.all()); },
    async first(...args) { reserve(1,writes); return checked(await statement.first(...args)); },
    async run() { reserve(1,writes); return checked(await statement.run()); },
  });
  return {usage, requireBudget,
    queriesLeft:() => options.maxQueries - usage.queries,
    writesLeft:() => options.maxWrites - usage.writes,
    db:{
      prepare(sql) { return wrap(db.prepare(sql), /^\s*(?:INSERT|UPDATE|DELETE)\b/i.test(sql) ? 1 : 0); },
      async batch(statements) {
        reserve(1,statements.length);
        const results = await db.batch(statements.map(row => row.statement));
        if (!Array.isArray(results) || results.length !== statements.length) {
          throw new Error("history_maintenance_batch_incomplete");
        }
        results.forEach(checked);
        return results;
      },
    },
  };
}

// The existing scoring functions have no as-of option. Restrict their known
// source SELECTs here; writes retain their established numeric contract.
function cutoffDb(db, cutoff) {
  let baselineRows;
  return {prepare(sql) {
    let adjusted = sql;
    const params = [];
    const indexes = [...sql.matchAll(/\?(\d+)/g)].map(match => Number(match[1]));
    const index = Math.max(0,...indexes) + 1;
    if (/FROM signal_episodes e\s+JOIN signal_outcomes o|FROM signal_wallets w\s+JOIN signal_episodes e/.test(sql)
        && sql.includes("AND o.numeric_contract_version >= 2 AND o.entry_verified=1")) {
      const guard = ` AND e.caught_at<=?${index} AND e.created_at<=?${index}
        AND o.evaluated_at<=?${index} AND o.updated_at<=?${index}
        ${sql.includes("FROM signal_wallets w") ? `AND w.created_at<=?${index}` : ""}`;
      adjusted = sql.replace("AND o.numeric_contract_version >= 2 AND o.entry_verified=1",
        "AND o.numeric_contract_version >= 2 AND o.entry_verified=1" + guard);
      params.push(cutoff);
    } else if (/FROM market_baselines\s+WHERE horizon_minutes = 4320/.test(sql)) {
      adjusted += ` AND computed_through<=?${index}`;
      params.push(cutoff);
    }
    const cachedBaseline = /FROM market_baselines\s+WHERE horizon_minutes = 4320/.test(sql);
    const prepared = db.prepare(adjusted);
    const wrap = values => ({
      bind(...next) { return wrap(next); },
      all:() => cachedBaseline ? baselineRows ||= prepared.bind(...values,...params).all()
        : prepared.bind(...values,...params).all(),
      first:(...args) => prepared.bind(...values,...params).first(...args),
      run:() => prepared.bind(...values,...params).run(),
      get statement() { return prepared.bind(...values,...params); },
    });
    return wrap([]);
  },
  batch(statements) { return db.batch(statements.map(row => row.statement)); },
  };
}

const DIRTY_BANDS = `WITH dirty_bands AS (
  SELECT DISTINCT e.mcap_band,e.liquidity_band,e.age_band,e.signal_family
  FROM history_maintenance_dirty d JOIN signal_episodes e ON e.episode_id=d.episode_id
  WHERE d.ready_at<=?1 AND d.source_at<=?1 AND e.created_at<=?1 AND e.caught_at<=?1
)`;

const AFFECTED_EPISODE = `EXISTS (SELECT 1 FROM signal_episodes e JOIN dirty_bands b
  ON e.mcap_band=b.mcap_band AND e.liquidity_band=b.liquidity_band
    AND e.age_band=b.age_band AND e.signal_family=b.signal_family
  WHERE e.episode_id=w.episode_id AND e.created_at<=?1 AND e.caught_at<=?1)`;

async function refreshDailyCluster(db, cluster, cutoff) {
  const stats = await db.prepare(`WITH scores AS (
    SELECT s.* FROM wallet_scores s JOIN wallet_cluster_members m USING(wallet_address)
    WHERE m.cluster_id=?1 AND s.numeric_contract_version>=2 AND s.computed_through<=?2
  ), ranked_score AS (
    SELECT edge_score,ROW_NUMBER() OVER (ORDER BY edge_score) n,COUNT(*) OVER () total FROM scores
  ), ranked_lift AS (
    SELECT lift_2x,ROW_NUMBER() OVER (ORDER BY lift_2x) n,COUNT(*) OVER () total FROM scores WHERE lift_2x IS NOT NULL
  ) SELECT COALESCE(SUM(eligible_episodes),0) eligible_episodes,COALESCE(SUM(wins_2x_72h),0) wins,
    COALESCE((SELECT AVG(edge_score) FROM ranked_score WHERE n IN ((total+1)/2,(total+2)/2)),0) edge_score,
    (SELECT AVG(lift_2x) FROM ranked_lift WHERE n IN ((total+1)/2,(total+2)/2)) lift,
    CASE WHEN MAX(confidence='validated') THEN 'validated' WHEN MAX(confidence='emerging')
      THEN 'emerging' ELSE 'unproven' END confidence FROM scores`).bind(cluster,cutoff).first();
  await db.prepare(`UPDATE wallet_clusters SET confidence=?2,eligible_episodes=?3,wins_2x_72h=?4,
    lift_2x=?5,edge_score=?6,computed_through=?7,updated_at=?7 WHERE cluster_id=?1 AND active=1`)
    .bind(cluster,stats.confidence,stats.eligible_episodes,stats.wins,stats.lift,stats.edge_score,cutoff).run();
}

export async function historyMaintenanceStatus(env) {
  if (historyDerivedMode(env) !== "daily") return {enabled:false, mode:"per_event"};
  if (!env?.RADAR_HISTORY_DB?.prepare) return {enabled:false, error:"history_db_not_configured"};
  const db = env.RADAR_HISTORY_DB;
  const pending = await db.prepare(`SELECT COUNT(*) pending_events,MIN(ready_at) oldest_pending_at
    FROM history_maintenance_dirty`).first();
  const job = await db.prepare(`SELECT job_day,cutoff_at,phase,updated_at,completed_at
    FROM history_maintenance_jobs ORDER BY job_day DESC LIMIT 1`).first();
  return {enabled:true, mode:"daily", ...pending, job};
}

// Call on the daily cron and on bounded continuation ticks. There is at most
// one job per UTC day; unfinished jobs resume before a newer day may start.
export async function runHistoryMaintenance(env, options = {}) {
  if (historyDerivedMode(env) !== "daily") return {enabled:false, mode:"per_event"};
  if (!env?.RADAR_HISTORY_DB?.prepare) return {enabled:false, error:"history_db_not_configured"};
  const now = timestamp(options.now || new Date().toISOString());
  const maxQueries = bound(options.maxQueries,40,MAX_QUERIES);
  const maxWrites = bound(options.maxWrites,maxQueries,MAX_QUERIES * PAGE);
  const budget = budgetDb(env.RADAR_HISTORY_DB,{...options,maxQueries,maxWrites});
  const db = budget.db;
  const result = {enabled:true, mode:"daily", deferred:false};
  let job, token;
  const finish = () => ({...result, ...budget.usage, job:job ? {
    job_day:job.job_day,cutoff_at:job.cutoff_at,phase:job.phase,completed_at:job.completed_at || null,
  } : null});
  const save = async state => {
    const saved = await db.prepare(`UPDATE history_maintenance_jobs SET phase=?2,state_json=?3,
      updated_at=?4,completed_at=?5 WHERE job_day=?1 AND lease_token=?6 RETURNING job_day`)
      .bind(job.job_day,job.phase,JSON.stringify(state),now,job.completed_at || null,token).first();
    if (!saved) throw new MaintenanceYield("history_maintenance_lease_lost");
  };
  try {
    budget.requireBudget(7,3);
    job = await db.prepare(`SELECT * FROM history_maintenance_jobs WHERE completed_at IS NULL
      ORDER BY job_day LIMIT 1`).first();
    if (!job) {
      const day = now.slice(0,10);
      job = await db.prepare("SELECT * FROM history_maintenance_jobs WHERE job_day=?1").bind(day).first();
      if (job?.completed_at) { result.reason="history_maintenance_daily_complete"; return finish(); }
      const pending = await db.prepare(`SELECT event_id FROM history_maintenance_dirty
        WHERE ready_at<=?1 AND source_at<=?1 LIMIT 1`).bind(now).first();
      if (!pending) { result.reason="history_maintenance_idle"; return finish(); }
      await db.prepare(`INSERT OR IGNORE INTO history_maintenance_jobs (job_day,cutoff_at,created_at,updated_at)
        VALUES (?1,?2,?2,?2)`).bind(day,now).run();
      job = await db.prepare("SELECT * FROM history_maintenance_jobs WHERE job_day=?1").bind(day).first();
    }
    token = crypto.randomUUID();
    const acquired = await db.prepare(`UPDATE history_maintenance_jobs SET lease_token=?2,lease_until=?3
      WHERE job_day=?1 AND completed_at IS NULL AND (lease_until IS NULL OR lease_until<=?4)
      RETURNING job_day`).bind(job.job_day,token,new Date(Date.parse(now)+LEASE_MS).toISOString(),now).first();
    if (!acquired) { token=null; result.deferred=true; result.reason="history_maintenance_busy"; return finish(); }
    let state = JSON.parse(job.state_json || "{}");
    const scoped = cutoffDb(db,job.cutoff_at);
    while (job.phase !== "done") {
      // Reserve the entire idempotent unit plus cursor commit and lease release.
      budget.requireBudget(6,3);
      if (job.phase === "baselines") {
        const row = state.band || await db.prepare(`${DIRTY_BANDS} SELECT *,
          mcap_band||'|'||liquidity_band||'|'||age_band||'|'||signal_family band_key FROM dirty_bands
          WHERE mcap_band||'|'||liquidity_band||'|'||age_band||'|'||signal_family>?2
          ORDER BY band_key LIMIT 1`).bind(job.cutoff_at,state.after || "").first();
        if (!row) { job.phase="wallets"; state={}; }
        else {
          state.band=row;
          const horizons = Object.values(OUTCOME_HORIZONS);
          await refreshMarketBaselines(scoped,job.cutoff_at,row,horizons[state.horizon || 0]);
          state.horizon = (state.horizon || 0) + 1;
          if (state.horizon >= horizons.length) { state.after=row.band_key; state.horizon=0; delete state.band; }
        }
      } else if (job.phase === "wallets") {
        if (!state.page?.length) {
          const page = await db.prepare(`${DIRTY_BANDS} SELECT DISTINCT w.wallet_address FROM signal_wallets w
            WHERE w.wallet_address>?2 AND w.cohort_role='at_catch' AND w.created_at<=?1
              AND ${AFFECTED_EPISODE} ORDER BY w.wallet_address LIMIT ${PAGE}`)
            .bind(job.cutoff_at,state.after || "").all();
          state.page=page.results.map(row => row.wallet_address); state.index=0;
        }
        if (!state.page.length) { job.phase="clusters"; state={}; }
        else {
          const wallet = state.page[state.index];
          await refreshWalletScores(scoped,[wallet],job.cutoff_at); state.after=wallet;
          if (++state.index >= state.page.length) { delete state.page; delete state.index; }
        }
      } else if (job.phase === "clusters") {
        if (!state.page?.length) {
          const page = await db.prepare(`${DIRTY_BANDS} SELECT c.cluster_id FROM wallet_clusters c
            WHERE c.active=1 AND c.numeric_contract_version>=2 AND c.cluster_id>?2 AND EXISTS (
              SELECT 1 FROM wallet_cluster_members m JOIN signal_wallets w USING(wallet_address)
              WHERE m.cluster_id=c.cluster_id AND w.cohort_role='at_catch' AND w.created_at<=?1
                AND ${AFFECTED_EPISODE}) ORDER BY c.cluster_id LIMIT ${PAGE}`)
            .bind(job.cutoff_at,state.after || "").all();
          state.page=page.results.map(row => row.cluster_id); state.index=0;
        }
        if (!state.page.length) { job.phase="cleanup"; state={}; }
        else {
          const cluster = state.page[state.index];
          await refreshDailyCluster(db,cluster,job.cutoff_at); state.after=cluster;
          if (++state.index >= state.page.length) { delete state.page; delete state.index; }
        }
      } else if (job.phase === "cleanup") {
        const size = Math.min(PAGE,budget.writesLeft()-2);
        const page = await db.prepare(`SELECT event_id FROM history_maintenance_dirty
          WHERE ready_at<=?1 AND last_ready_at<=?1 AND source_at<=?1 AND event_id>?2 ORDER BY event_id LIMIT ${size}`)
          .bind(job.cutoff_at,state.after || "").all();
        if (!page.results.length) { job.phase="done"; job.completed_at=now; state={}; }
        else {
          await db.batch(page.results.map(row => db.prepare(`DELETE FROM history_maintenance_dirty
            WHERE event_id=?1 AND ready_at<=?2 AND last_ready_at<=?2 AND source_at<=?2`).bind(row.event_id,job.cutoff_at)));
          state.after=page.results.at(-1).event_id;
        }
      } else throw new Error("history_maintenance_phase_invalid");
      await save(state);
    }
  } catch (error) {
    if (!error.deferred) throw error;
    result.deferred=true; result.reason=error.message;
  } finally {
    if (token && job && budget.queriesLeft() > 0 && budget.writesLeft() > 0) {
      await db.prepare(`UPDATE history_maintenance_jobs SET lease_token=NULL,lease_until=NULL
        WHERE job_day=?1 AND lease_token=?2`).bind(job.job_day,token).run();
    }
  }
  return finish();
}

// Only redundant delivered outbox copies are eligible. The indexed SQL event
// must reference an existing content-addressed archive of the identical bytes.
export async function pruneArchivedHistoryOutbox(env, options = {}) {
  if (!env?.RADAR_DB?.prepare || !env.RADAR_ARCHIVE?.get) return {enabled:false, deleted:0};
  if (env.RADAR_DB !== env.RADAR_HISTORY_DB) {
    return {enabled:false, deleted:0, reason:"history_retention_canonical_db_required"};
  }
  const now = timestamp(options.now || new Date().toISOString());
  const before = timestamp(options.deliveredBefore || new Date(Date.parse(now)-7*86400_000).toISOString());
  const maxQueries = bound(options.maxQueries,30,MAX_QUERIES);
  const maxWrites = bound(options.maxWrites,maxQueries,MAX_QUERIES);
  const maxRequests = bound(options.maxRequests,40,50);
  const pageSize = bound(options.pageSize,PAGE,PAGE);
  const budget = budgetDb(env.RADAR_DB,{...options,maxQueries,maxWrites});
  let after = options.after || "", deleted = 0, checked = 0, archiveReads = 0;
  const usage = () => ({...budget.usage,archive_reads:archiveReads});
  const archiveOptions = {...options.archiveOptions,onOperation:async (kind,bytes) => {
    if (kind === "read") {
      if (budget.usage.queries+archiveReads+2>maxRequests) {
        throw new MaintenanceYield("history_retention_request_budget");
      }
      archiveReads++;
    }
    await options.archiveOptions?.onOperation?.(kind,bytes);
  }};
  try {
    budget.requireBudget(1);
    if (!maxRequests) throw new MaintenanceYield("history_retention_request_budget");
    const index = await budget.db.prepare(`SELECT name FROM sqlite_master
      WHERE type='index' AND name='idx_history_outbox_delivered_keyset'`).first();
    if (!index) return {enabled:false,deleted:0,reason:"history_retention_index_migration_required",...usage()};
    budget.requireBudget(1);
    if (budget.usage.queries+archiveReads+1>maxRequests) throw new MaintenanceYield("history_retention_request_budget");
    const page = await budget.db.prepare(`WITH candidates AS MATERIALIZED (
      SELECT event_id FROM history_outbox INDEXED BY idx_history_outbox_delivered_keyset
      WHERE status='delivered' AND delivered_at<?1 AND event_id>?2 ORDER BY event_id LIMIT ${pageSize}
    ) SELECT c.event_id,o.payload_json,e.payload_json event_payload,e.raw_object_key
      FROM candidates c JOIN history_outbox o ON o.event_id=c.event_id
      LEFT JOIN signal_episode_events e ON e.event_id=c.event_id ORDER BY c.event_id`).bind(before,after).all();
    for (const row of page.results) {
      budget.requireBudget(1,1);
      let ref, original;
      try {
        ref = validateHistoryArchiveReference(JSON.parse(row.event_payload || "{}").archive_ref);
        original = JSON.parse(row.payload_json);
      } catch { checked++; after=row.event_id; continue; }
      let verified = false;
      if (ref.key === row.raw_object_key && ref.event_id === row.event_id) {
        try {
          const archived = await readHistoryArchive(env,ref,archiveOptions);
          verified = sameJson(original,archived);
        } catch (error) {
          return {enabled:true,deleted,checked,after,complete:false,deferred:true,
            reason:error.message,...usage()};
        }
      }
      checked++;
      if (verified) {
        const result = await budget.db.prepare(`DELETE FROM history_outbox WHERE event_id=?1
          AND status='delivered' AND delivered_at<?2 AND payload_json=?3`)
          .bind(row.event_id,before,row.payload_json).run();
        deleted += Number(result.meta?.changes ?? result.meta?.rows_written ?? 0);
      }
      after=row.event_id;
    }
    return {enabled:true, deleted, checked, after, complete:page.results.length<pageSize, ...usage()};
  } catch (error) {
    if (!error.deferred) throw error;
    return {enabled:true, deleted, checked, after, complete:false, deferred:true, reason:error.message, ...usage()};
  }
}

function sameJson(left, right, depth = 0) {
  if (depth > 100) return false;
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object"
      || Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) return false;
  return keys.every(key => Object.hasOwn(right,key) && sameJson(left[key],right[key],depth+1));
}
