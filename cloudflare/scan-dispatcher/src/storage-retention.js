import {collectSqlRuntimeGarbage, RUNTIME_RETENTION_LIMITS} from "./runtime-sql.js";

export const STORAGE_RETENTION_POLICY = Object.freeze({runtime:RUNTIME_RETENTION_LIMITS,
  deliveredOutboxDays:7, scanRunDays:30, closedEpisodeDays:90, activePositionsExpire:false,
  episodeCleanupEnabled:true, episodeClosureContract:"terminal-closed-v1", observationCleanupEnabled:false,
  historyMaxQueries:16, runtimeMaxQueries:24});

export function terminalClosureMarker(proof) {
  // This must be a reconciled entire-cohort inventory, never gross flows or a
  // receipt component. Current scanner outflow evidence cannot satisfy it.
  if (!proof || proof.version!==1 || proof.scope!=="entire_original_cohort"
      || proof.status!=="complete" || proof.history_complete!==true || proof.outflows_resolved!==true
      || proof.original_cohort_denominator_complete!==true || proof.original_sales_proven!==true
      || !Array.isArray(proof.issues) || proof.issues.length
      || ["cohort_wallet_coverage_pct","cohort_token_coverage_pct","balance_coverage_pct","token_balance_coverage_pct"]
        .some(key => proof[key]!==100)) return null;
  const raw=proof.amounts_raw;
  if (!raw || ["bought","sold","original","transferred","unknown"].some(key => typeof raw[key]!=="string" || !/^(0|[1-9]\d*)$/.test(raw[key]))
      || ["cohort_balance_raw","remaining_upper_bound_raw","active_positions_raw"].some(key => proof[key]!=="0")
      || ["original","transferred","unknown"].some(key => raw[key]!=="0")
      || BigInt(raw.bought)<=0n || raw.bought!==raw.sold) return null;
  if (typeof proof.checked_at!=="string" || !Number.isFinite(Date.parse(proof.checked_at))) return null;
  const at=new Date(proof.checked_at).toISOString();
  return {episode:{closed_at:at},event:{event_type:"retention_check",observed_at:at,thesis_status:"closed",
    cohort_retained_pct:0,retained_supply_pct:0,data_quality_status:"complete",
    terminal_closure:{contract:STORAGE_RETENTION_POLICY.episodeClosureContract,scope:proof.scope,
      checked_at:at,cohort_balance_raw:"0",remaining_upper_bound_raw:"0",active_positions_raw:"0",
      outflows_resolved:true,history_complete:true,original_sales_proven:true}}};
}

function integer(value, fallback, minimum, maximum) {
  if (value !== undefined && typeof value !== "number"
      && (typeof value !== "string" || !/^\d+$/.test(value))) throw new Error("storage_retention_options_invalid");
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) throw new Error("storage_retention_options_invalid");
  return parsed;
}

function config(options) {
  if (!options || typeof options !== "object" || Array.isArray(options)
      || ["preview","force","runtime","history","episodes"].some(key => options[key] !== undefined && typeof options[key] !== "boolean")
      || (options.onQuery !== undefined && typeof options.onQuery !== "function")) throw new Error("storage_retention_options_invalid");
  return {...options, now:integer(options.now, Date.now(), 0, 8_640_000_000_000_000),
    maxBatches:integer(options.maxBatches, 4, 0, 4), batchRows:integer(options.batchRows, 256, 1, 256),
    maxQueries:integer(options.maxQueries, 32, 1, 32)};
}

function reserveFor(result, settings) {
  return () => {
    if (result.queries >= settings.maxQueries) throw new Error("storage_retention_query_budget");
    settings.onQuery?.(); result.queries++;
  };
}

function checkedPage(page) {
  if (page?.success === false || !Array.isArray(page?.results)) throw new Error("storage_retention_result_invalid");
  return page.results;
}

const TABLES = ["history_outbox","signal_episode_events","scan_runs","signal_episodes",
  "history_sql_queue_events","history_sql_cutover_events"];
const INDEXES = ["idx_history_outbox_delivered_retention","idx_signal_episodes_source_run_key"];

function cleanupTables(available, now) {
  const cutoff = days => new Date(now - days * 86400_000).toISOString();
  const tables = [];
  if (available.has("history_outbox") && available.has("signal_episode_events")) {
    const guards = [];
    if (available.has("history_sql_queue_events")) guards.push(`NOT EXISTS (SELECT 1 FROM history_sql_queue_events q
      WHERE q.event_id=d.event_id AND (q.status='pending' OR q.archive_pending=1))`);
    if (available.has("history_sql_cutover_events")) guards.push("NOT EXISTS (SELECT 1 FROM history_sql_cutover_events c WHERE c.event_id=d.event_id)");
    tables.push({name:"history_outbox", key:"event_id", version:"updated_at", cutoff:cutoff(7),
      index:"idx_history_outbox_delivered_retention", requiredIndex:"idx_history_outbox_delivered_retention",
      age:"d.status='delivered' AND d.delivered_at<?1 AND julianday(d.delivered_at)<julianday(?1)",
      proof:`EXISTS (SELECT 1 FROM signal_episode_events e WHERE e.event_id=d.event_id AND e.payload_json=d.payload_json)
        ${guards.map(guard => `AND ${guard}`).join(" ")}`,
      order:"d.delivered_at,d.event_id", bytes:"length(CAST(payload_json AS BLOB))"});
  }
  if (available.has("scan_runs") && available.has("signal_episodes")) tables.push({name:"scan_runs",
    key:"run_key", version:"generated_at", cutoff:cutoff(30),
    requiredIndex:"idx_signal_episodes_source_run_key",
    age:"d.generated_at<?1 AND julianday(d.generated_at)<julianday(?1)",
    proof:"NOT EXISTS (SELECT 1 FROM signal_episodes e WHERE e.source_run_key=d.run_key)",
    order:"d.generated_at,d.run_key", bytes:`length(CAST(lanes_scanned_json AS BLOB))
      +length(CAST(stats_json AS BLOB))+length(CAST(lane_stats_json AS BLOB))`});
  return tables;
}

const fromTable = table => `${table.name} d${table.index ? ` INDEXED BY ${table.index}` : ""}`;

async function historyInventory(db, tables, reserve) {
  const result = {};
  for (const table of tables) {
    reserve();
    // Preview counts age candidates, not proven duplicates; no JSON is loaded.
    const row = await db.prepare(`SELECT COUNT(*) candidate_rows FROM ${fromTable(table)} WHERE ${table.age}`)
      .bind(table.cutoff).first();
    if (!Number.isSafeInteger(row?.candidate_rows) || row.candidate_rows < 0) throw new Error("storage_retention_result_invalid");
    result[table.name] = {candidate_rows:row.candidate_rows, cutoff:table.cutoff,
      proof_required:true, deleted_rows:0, deleted_bytes:0};
  }
  return result;
}

export async function cleanupSqlHistoryRetention(env, options = {}) {
  const result = {ok:true, preview:Boolean(options?.preview), queries:0, batches:0, deleted_rows:0,
    deleted_bytes:0, candidates:null, tables:{}, error:null, outcome_unknown:false,
    bytes_basis:"deleted_json_payload_bytes_not_physical", preserved_tables:["wallet_cluster_edges",
      "wallet_clusters","wallet_cluster_members","wallet_scores","market_baselines"],
    episode_dependencies_retire_only_with_parent:true};
  let writeInFlight=false;
  try {
    const settings = config({...options,maxQueries:options.maxQueries ?? STORAGE_RETENTION_POLICY.historyMaxQueries}), db = env.RADAR_DB;
    if (settings.maxQueries > STORAGE_RETENTION_POLICY.historyMaxQueries) throw new Error("storage_retention_options_invalid");
    if (!db?.prepare || db !== env.RADAR_HISTORY_DB) throw new Error("storage_retention_canonical_db_required");
    const reserve = reserveFor(result,settings);
    reserve();
    const names=[...TABLES,...INDEXES];
    const schema = checkedPage(await db.prepare(`SELECT name FROM sqlite_schema WHERE type IN ('table','index')
      AND name IN (${names.map((_,i) => `?${i+1}`).join(",")})`).bind(...names).all());
    const available=new Set(schema.map(row => row.name)), tables=cleanupTables(available,settings.now);
    if (tables.some(table => !available.has(table.requiredIndex))) throw new Error("storage_retention_index_migration_required");
    result.tables=await historyInventory(db,tables,reserve);
    result.unavailable_tables=["history_outbox","scan_runs"].filter(name => !result.tables[name]);
    if (!settings.preview) {
      for (const table of tables) {
        while (result.batches < settings.maxBatches && result.tables[table.name].candidate_rows) {
          if (result.queries + 2 + tables.length > settings.maxQueries) {
            result.deferred_reason="storage_retention_query_budget"; break;
          }
          reserve();
          const page = checkedPage(await db.prepare(`SELECT d.${table.key} key,d.${table.version} version
            FROM ${fromTable(table)} WHERE ${table.age} AND ${table.proof}
            ORDER BY ${table.order} LIMIT ?2`).bind(table.cutoff,settings.batchRows).all());
          if (!page.length) break;
          const args=[table.cutoff];
          const tuples=page.map(row => {const first=args.length+1;args.push(row.key,row.version);return `(?${first},?${first+1})`;});
          reserve(); writeInFlight=true;
          // Proof is checked again at deletion, never inferred from a preview.
          const response=await db.prepare(`DELETE FROM ${table.name} AS d WHERE ${table.age} AND ${table.proof}
            AND (d.${table.key},d.${table.version}) IN (VALUES ${tuples.join(",")})
            RETURNING ${table.key} key,${table.bytes} bytes`).bind(...args).all();
          if (response?.success === false || !Array.isArray(response?.results)) {
            throw Object.assign(new Error("storage_retention_result_invalid"),{outcomeUnknown:true});
          }
          const removed=response.results;
          const bytes=removed.reduce((sum,row) => {
            if (!Number.isSafeInteger(row.bytes) || row.bytes<0 || !Number.isSafeInteger(sum+row.bytes)) {
              throw Object.assign(new Error("storage_retention_result_invalid"),{outcomeUnknown:true});
            }
            return sum+row.bytes;
          },0);
          writeInFlight=false; result.batches++;
          result.deleted_rows+=removed.length; result.deleted_bytes+=bytes;
          result.tables[table.name].deleted_rows+=removed.length; result.tables[table.name].deleted_bytes+=bytes;
          result.candidates=null;
          if (page.length<settings.batchRows || !removed.length) break;
        }
      }
      const fresh=await historyInventory(db,tables,reserve);
      for (const table of tables) result.tables[table.name].candidate_rows=fresh[table.name].candidate_rows;
      if (result.batches===settings.maxBatches && Object.values(fresh).some(row => row.candidate_rows)) {
        result.deferred_reason ||= "storage_retention_batch_budget";
      }
    }
    result.candidates=Object.fromEntries(Object.entries(result.tables).map(([name,row]) => [name,row.candidate_rows]));
    if (settings.episodes !== false) {
      const remaining=settings.maxQueries-result.queries;
      if (!remaining) result.episodes={enabled:false,deferred_reason:"episode_retention_query_budget"};
      else {
        result.episodes=await cleanupClosedSqlEpisodes(env,{...settings,onQuery:reserve,
          maxQueries:remaining,maxBatches:settings.maxBatches-result.batches});
        result.batches+=result.episodes.batches; result.deleted_rows+=result.episodes.deleted_rows;
        result.deleted_bytes+=result.episodes.deleted_bytes;
        if (!result.episodes.ok) {
          result.ok=false; result.error=result.episodes.error; result.outcome_unknown=result.episodes.outcome_unknown;
          result.candidates=null;
        }
      }
    }
  } catch (error) {
    result.ok=false; result.error=String(error?.message || error); result.candidates=null;
    result.outcome_unknown=writeInFlight && Boolean(error?.outcomeUnknown);
    for (const table of Object.values(result.tables)) table.candidate_rows=null;
  }
  return result;
}

export async function runStorageRetention(env, options = {}) {
  const result = {ok:true, preview:Boolean(options?.preview), queries:0, batches:0, deleted_rows:0,
    deleted_bytes:0, runtime:null, history:null, error:null, outcome_unknown:false};
  try {
    const settings=config(options);
    const onQuery=reserveFor(result,settings);
    if (settings.runtime !== false) {
      result.runtime=await collectSqlRuntimeGarbage(env,{...settings, onQuery,
        maxQueries:Math.min(settings.maxQueries,STORAGE_RETENTION_POLICY.runtimeMaxQueries),
        garbageThresholdBytes:settings.garbageThresholdBytes ?? RUNTIME_RETENTION_LIMITS.garbageThresholdBytes});
      result.batches+=result.runtime.batches; result.deleted_rows+=result.runtime.deleted_rows;
      result.deleted_bytes+=result.runtime.deleted_bytes;
      if (!result.runtime.ok) throw Object.assign(new Error(result.runtime.error),{outcomeUnknown:result.runtime.outcome_unknown});
    }
    if (settings.history !== false) {
      result.history=await cleanupSqlHistoryRetention(env,{...settings,onQuery,
        maxQueries:Math.min(settings.maxQueries-result.queries,STORAGE_RETENTION_POLICY.historyMaxQueries),
        maxBatches:settings.maxBatches-result.batches});
      result.batches+=result.history.batches; result.deleted_rows+=result.history.deleted_rows;
      result.deleted_bytes+=result.history.deleted_bytes;
      if (!result.history.ok) throw Object.assign(new Error(result.history.error),{outcomeUnknown:result.history.outcome_unknown});
    }
  } catch (error) {
    result.ok=false; result.error=String(error?.message || error); result.outcome_unknown=Boolean(error?.outcomeUnknown);
  }
  return result;
}

export async function previewStorageRetention(env, options = {}) {
  return runStorageRetention(env,{...options,preview:true});
}

const EPISODE_TABLES=["signal_episodes","signal_episode_events","signal_outcomes","signal_wallets",
  "wallet_observations","wallet_observation_bundles","wallet_cluster_edge_evidence","history_outbox",
  "history_sql_queue_events","history_sql_cutover_events","history_maintenance_dirty",
  "history_maintenance_jobs","history_cluster_lock","history_cluster_work","history_episode_retirement_work"];
const EPISODE_INDEXES=["idx_signal_episodes_closed_retention","idx_history_sql_queue_retention_episode",
  "idx_history_sql_cutover_retention_episode"];
const EPISODE_CHILDREN=["signal_episode_events","signal_wallets","wallet_observations","signal_outcomes",
  "wallet_observation_bundles","wallet_cluster_edge_evidence"];
const EPISODE_ROWS=`1+${EPISODE_CHILDREN.map(name => `(SELECT COUNT(*) FROM ${name} c WHERE c.episode_id=d.episode_id)`).join("+")}`;
const EPISODE_AGE="d.closed_at<?1 AND julianday(d.closed_at)<julianday(?1)";
const EPISODE_PROOF=`d.data_quality_status='complete' AND julianday(d.caught_at)<=julianday(d.closed_at)
  AND julianday(d.last_signal_at)<=julianday(d.closed_at)
  AND EXISTS (SELECT 1 FROM signal_episode_events c WHERE c.episode_id=d.episode_id
    AND c.event_type='retention_check' AND c.thesis_status='closed' AND c.observed_at=d.closed_at
    AND c.cohort_retained_pct=0 AND c.retained_supply_pct=0 AND c.data_quality_status='complete')
  AND NOT EXISTS (SELECT 1 FROM signal_episode_events c WHERE c.episode_id=d.episode_id
    AND (julianday(c.observed_at) IS NULL OR julianday(c.observed_at)>julianday(d.closed_at)
      OR (c.observed_at=d.closed_at AND c.thesis_status IS NOT 'closed')
      OR c.raw_object_key IS NULL OR trim(c.raw_object_key)=''))
  AND (SELECT COUNT(*) FROM signal_outcomes o WHERE o.episode_id=d.episode_id
    AND o.horizon_minutes IN (60,360,1440,4320,10080) AND o.status='complete')=5
  AND NOT EXISTS (SELECT 1 FROM signal_outcomes o WHERE o.episode_id=d.episode_id
    AND (o.status IS NOT 'complete' OR COALESCE(trim(o.error),'')!=''
      OR julianday(o.evaluated_at) IS NULL OR julianday(o.due_at) IS NULL
      OR julianday(o.evaluated_at)<julianday(o.due_at) OR julianday(o.evaluated_at)>julianday(d.closed_at)))
  AND NOT EXISTS (SELECT 1 FROM history_outbox WHERE status IS NOT 'delivered')
  AND NOT EXISTS (SELECT 1 FROM history_sql_queue_events q WHERE q.episode_id=d.episode_id
    AND (q.status IS NOT 'delivered' OR q.archive_pending!=0 OR q.lease_token IS NOT NULL OR q.archive_lease_token IS NOT NULL))
  AND NOT EXISTS (SELECT 1 FROM history_sql_cutover_events q WHERE q.episode_id=d.episode_id)
  AND NOT EXISTS (SELECT 1 FROM history_maintenance_dirty q WHERE q.episode_id=d.episode_id)
  AND NOT EXISTS (SELECT 1 FROM history_maintenance_jobs WHERE completed_at IS NULL)
  AND NOT EXISTS (SELECT 1 FROM history_cluster_lock WHERE job_id IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM history_cluster_work)
  AND NOT EXISTS (SELECT 1 FROM wallet_cluster_edge_evidence c WHERE c.episode_id=d.episode_id)
  AND NOT EXISTS (SELECT 1 FROM signal_wallets c WHERE c.episode_id=d.episode_id
    AND (c.raw_object_key IS NULL OR trim(c.raw_object_key)=''))
  AND NOT EXISTS (SELECT 1 FROM wallet_observations c WHERE c.episode_id=d.episode_id
    AND (c.raw_object_key IS NULL OR trim(c.raw_object_key)=''
      OR (julianday(c.observed_at)>=julianday(d.closed_at) AND c.current_token_balance IS NOT 0)))
  AND NOT EXISTS (SELECT 1 FROM wallet_observation_bundles b WHERE b.episode_id=d.episode_id
    AND NOT EXISTS (SELECT 1 FROM signal_episode_events c WHERE c.event_id=b.event_id AND c.episode_id=d.episode_id))`;

async function episodeInventory(db,cutoff,reserve) {
  reserve();
  const row=await db.prepare(`SELECT COUNT(*) candidate_rows,
    COALESCE(SUM(CASE WHEN ${EPISODE_PROOF} THEN 1 ELSE 0 END),0) proven_rows
    FROM signal_episodes d INDEXED BY idx_signal_episodes_closed_retention WHERE ${EPISODE_AGE}`).bind(cutoff).first();
  if (![row?.candidate_rows,row?.proven_rows].every(value => Number.isSafeInteger(value) && value>=0)) {
    throw new Error("storage_retention_result_invalid");
  }
  return row;
}

export async function previewClosedSqlEpisodes(env,options = {}) {
  return cleanupClosedSqlEpisodes(env,{...options,preview:true});
}

export async function cleanupClosedSqlEpisodes(env,options = {}) {
  const result={ok:true,enabled:false,preview:Boolean(options?.preview),queries:0,batches:0,
    deleted_rows:0,deleted_bytes:0,deleted_episodes:0,candidate_rows:null,proven_rows:null,
    error:null,outcome_unknown:false,bytes_basis:"deleted_json_payload_bytes_not_physical",
    outbox_guard:"any_non_delivered_blocks_all_episodes",relationship_evidence_preserved:true,
    queries_basis:"reserved_sql_statements"};
  let writeInFlight=false;
  try {
    const settings=config({...options,maxQueries:options.maxQueries ?? STORAGE_RETENTION_POLICY.historyMaxQueries});
    if (settings.maxQueries>STORAGE_RETENTION_POLICY.historyMaxQueries) throw new Error("storage_retention_options_invalid");
    // invalidated_at is reversible; current producers do not emit terminal CLOSED.
    // Enable only after the producer also excludes active/unresolved positions.
    if (env.HISTORY_EPISODE_CLOSURE_CONTRACT !== STORAGE_RETENTION_POLICY.episodeClosureContract) {
      result.deferred_reason="episode_retention_terminal_closure_contract_required"; return result;
    }
    const db=env.RADAR_DB;
    if (!db?.prepare || db!==env.RADAR_HISTORY_DB) throw new Error("storage_retention_canonical_db_required");
    const reserve=reserveFor(result,settings), names=[...EPISODE_TABLES,...EPISODE_INDEXES];
    reserve();
    const schema=checkedPage(await db.prepare(`SELECT name FROM sqlite_schema WHERE type IN ('table','index')
      AND name IN (${names.map((_,i) => `?${i+1}`).join(",")})`).bind(...names).all());
    const available=new Set(schema.map(row => row.name));
    if (names.some(name => !available.has(name))) {
      result.deferred_reason="episode_retention_schema_required"; result.missing_schema=names.filter(name => !available.has(name)); return result;
    }
    if (!settings.preview && !db.batch) throw new Error("episode_retention_atomic_batch_required");
    const cutoff=new Date(settings.now-90*86400_000).toISOString();
    result.cutoff=cutoff; result.enabled=true;
    Object.assign(result,await episodeInventory(db,cutoff,reserve));
    if (!settings.preview) {
      while (result.batches<settings.maxBatches && result.proven_rows) {
        // Five SQL statements, one atomic request, plus selection/fresh inventory.
        if (result.queries+7>settings.maxQueries) {result.deferred_reason="episode_retention_query_budget";break;}
        reserve();
        const page=checkedPage(await db.prepare(`SELECT d.episode_id,d.updated_at FROM signal_episodes d
          INDEXED BY idx_signal_episodes_closed_retention WHERE ${EPISODE_AGE} AND ${EPISODE_PROOF}
          AND (${EPISODE_ROWS})+1<=?2 ORDER BY d.closed_at,d.episode_id LIMIT 1`).bind(cutoff,settings.batchRows).all());
        if (!page.length) {result.deferred_reason="episode_retention_dependency_row_budget";break;}
        const token=crypto.randomUUID(), row=page[0], args=[cutoff,settings.batchRows,row.episode_id,row.updated_at,token];
        const eligible=`${EPISODE_AGE} AND ${EPISODE_PROOF} AND (${EPISODE_ROWS})+1<=?2
          AND d.episode_id=?3 AND d.updated_at=?4`;
        const marked="d.episode_id IN (SELECT episode_id FROM history_episode_retirement_work WHERE token=?1)";
        const jsonBytes=`COALESCE((SELECT SUM(length(CAST(payload_json AS BLOB))) FROM signal_episode_events c
          WHERE c.episode_id=d.episode_id),0)+COALESCE((SELECT SUM(length(CAST(observations_json AS BLOB)))
          FROM wallet_observation_bundles c WHERE c.episode_id=d.episode_id),0)`;
        // The marker is transaction-local in effect: successful parent deletion
        // cascades it away. A remaining marker violates CHECK and rolls back bundles.
        const statements=[
          db.prepare(`INSERT INTO history_episode_retirement_work(episode_id,token)
            SELECT d.episode_id,?5 FROM signal_episodes d WHERE ${eligible}`).bind(...args),
          db.prepare(`SELECT d.episode_id,(${EPISODE_ROWS}) source_rows,${jsonBytes} bytes
            FROM signal_episodes d WHERE ${marked}`).bind(token),
          db.prepare(`DELETE FROM wallet_observation_bundles WHERE episode_id IN
            (SELECT episode_id FROM history_episode_retirement_work WHERE token=?1)`).bind(token),
          db.prepare(`DELETE FROM signal_episodes AS d WHERE ${eligible} AND d.episode_id IN
            (SELECT episode_id FROM history_episode_retirement_work WHERE token=?5) RETURNING episode_id`).bind(...args),
          db.prepare("UPDATE history_episode_retirement_work SET token='' WHERE token=?1").bind(token),
        ];
        for (const _ of statements) reserve();
        writeInFlight=true;
        const response=await db.batch(statements);
        if (!Array.isArray(response) || response.length!==statements.length || response.some(item => item?.success===false)) {
          throw Object.assign(new Error("storage_retention_result_invalid"),{outcomeUnknown:true});
        }
        let removed, metrics;
        try {
          removed=checkedPage(response[3]); metrics=checkedPage(response[1]);
          if (removed.length!==metrics.length || removed.some(item => !metrics.some(metric => metric.episode_id===item.episode_id))
              || metrics.some(metric => ![metric.source_rows,metric.bytes].every(value => Number.isSafeInteger(value) && value>=0)
                || metric.source_rows<1 || metric.source_rows+1>settings.batchRows)) {
            throw new Error("storage_retention_result_invalid");
          }
        } catch {throw Object.assign(new Error("storage_retention_result_invalid"),{outcomeUnknown:true});}
        writeInFlight=false; result.batches++;
        result.deleted_episodes+=removed.length;
        result.deleted_rows+=metrics.reduce((sum,item) => sum+item.source_rows,0);
        result.deleted_bytes+=metrics.reduce((sum,item) => sum+item.bytes,0);
        result.candidate_rows=result.proven_rows=null;
        if (!removed.length) {result.deferred_reason="episode_retention_concurrent_protection";break;}
        // Null means a fresh selection is required; only the final inventory is advertised.
        result.proven_rows=1;
      }
      Object.assign(result,await episodeInventory(db,cutoff,reserve));
      if (result.proven_rows && !result.deferred_reason) result.deferred_reason="episode_retention_batch_budget";
    }
    if (result.candidate_rows && !result.proven_rows) result.deferred_reason ||= "episode_retention_closure_or_dependency_proof_missing";
  } catch (error) {
    result.ok=false; result.error=String(error?.message || error);
    result.outcome_unknown=writeInFlight && Boolean(error?.outcomeUnknown);
    result.candidate_rows=result.proven_rows=null;
  }
  return result;
}
