import {readSqlRuntime, writeSqlRuntime} from "./runtime-sql.js";

export const TOKEN_RETIREMENT_POLICY = Object.freeze({version:1,mcap_usd:20000,below_seconds:86400,
  quote_max_age_seconds:1200,max_observation_gap_seconds:7200,recapture_min_mcap_usd:30000,
  recapture_max_mcap_usd:500000,recapture_min_growth_pct:3});
export const retirementEnabled = env => env.TOKEN_RETIREMENT_ENABLED === "true";
const dbFor = env => {
  if (!env.RADAR_HISTORY_DB?.prepare) throw new Error("token_retirement_storage_unavailable");
  return env.RADAR_HISTORY_DB;
};
const stamp = value => typeof value === "string" && Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
const token = value => typeof value === "string" ? value.replace(/^solana:/,"") : "";
const rowToken = row => token(row?.token_address || row?.pool?.token_address);
const caught = row => stamp(row?.window_start || row?.signal_at || row?.caught_at || row?.captured_at || row?.created_at);
const checked = results => {
  if (!Array.isArray(results) || results.some(row => row?.success !== true)) throw new Error("token_retirement_sql_unverified");
  return results;
};

export async function retirementPage(env, cursor="") {
  if (!retirementEnabled(env)) return {ok:true,enabled:false,records:[],cursor:null};
  if (typeof cursor !== "string" || cursor.length>128) throw new Error("token_retirement_cursor_invalid");
  const rows = (await dbFor(env).prepare(`SELECT token_address,retired_at,reactivated_at,last_signal_at,
    reason,cleanup_pending FROM token_retirements WHERE token_address>?1 ORDER BY token_address LIMIT 501`)
    .bind(cursor).all()).results;
  const more=rows.length>500, records=rows.slice(0,500);
  return {ok:true,enabled:true,policy:TOKEN_RETIREMENT_POLICY,records,
    cursor:more ? records.at(-1).token_address : null};
}

export async function retirementMarkers(env, tokens) {
  if (!retirementEnabled(env)) return {};
  const unique=[...new Set(tokens.map(token).filter(Boolean))], markers={};
  for (let start=0;start<unique.length;start+=100) {
    const keys=unique.slice(start,start+100);
    const rows=(await dbFor(env).prepare(`SELECT token_address,retired_at,reactivated_at,last_signal_at,reason
      FROM token_retirements WHERE token_address IN (${keys.map((_,i)=>`?${i+1}`).join(",")})`)
      .bind(...keys).all()).results;
    for (const row of rows) markers[row.token_address]=row;
  }
  return markers;
}

export function currentRetirementRecord(row, markers) {
  const marker=markers[rowToken(row)];
  return !marker || Boolean(marker.reactivated_at && caught(row)>stamp(marker.retired_at));
}

export function snapshotTokenKeys(snapshot) {
  return [...new Set([...(snapshot.report?.signal_theses || []),...(snapshot.report?.alerts || []),
    ...(snapshot.history || []),...(snapshot.detail_signal_theses || []),...(snapshot.detail_current_alerts || []),
    ...(snapshot.detail_history || []),...(snapshot.history_ledger?.events || []).map(row=>row.episode)]
    .map(rowToken).concat(Object.keys(snapshot.market || {}),Object.keys(snapshot.token_detail_refs || {})).filter(Boolean))];
}

export function filterRetirementSnapshot(snapshot, markers) {
  const keep=row=>currentRetirementRecord(row,markers);
  const result={...snapshot,report:{...snapshot.report},token_retirements:{...snapshot.token_retirements,...markers}};
  for (const field of ["alerts","signal_theses","active"]) {
    if (Array.isArray(result.report[field])) result.report[field]=result.report[field].filter(keep);
  }
  for (const field of ["history","detail_signal_theses","detail_current_alerts","detail_history"]) {
    if (Array.isArray(result[field])) result[field]=result[field].filter(keep);
  }
  for (const field of ["summaries","pools"]) {
    if (Array.isArray(result.report[field])) result.report[field]=result.report[field].filter(row=>!markers[rowToken(row)]
      || markers[rowToken(row)].reactivated_at);
  }
  const visible=new Set([...(result.report.signal_theses || []),...(result.report.alerts || []),
    ...(result.history || [])].map(rowToken));
  for (const field of ["market","token_detail_refs"]) {
    if (snapshot[field]) result[field]=Object.fromEntries(Object.entries(snapshot[field]).filter(([key])=>
      !markers[token(key)] || visible.has(token(key))));
  }
  if (result.history_ledger?.events) result.history_ledger={...result.history_ledger,
    events:result.history_ledger.events.filter(row=>keep(row.episode))};
  return result;
}

export async function guardRetirementSnapshot(env, snapshot) {
  if (!retirementEnabled(env)) return snapshot;
  return filterRetirementSnapshot(snapshot,await retirementMarkers(env,snapshotTokenKeys(snapshot)));
}

export async function eventIsRetired(env, raw, episodeId=raw?.episode?.episode_id) {
  if (!retirementEnabled(env)) return false;
  const episode=raw?.episode || {};
  const row=await dbFor(env).prepare(`SELECT 1 retired WHERE EXISTS
    (SELECT 1 FROM retired_episode_ids WHERE episode_id=?1) OR EXISTS
    (SELECT 1 FROM token_retirements WHERE token_address=?2
      AND (reactivated_at IS NULL OR julianday(?3)<=julianday(retired_at)))`)
    .bind(episodeId || "",token(episode.token_address),episode.caught_at || null).first();
  return Boolean(row);
}

export async function filterRetiredHistoryEvents(env, events) {
  if (!retirementEnabled(env) || !events.length) return events;
  const markers=await retirementMarkers(env,events.map(row=>row.episode?.token_address));
  const ids=events.map(row=>row.episode?.episode_id).filter(Boolean), retired=new Set();
  for (let start=0;start<ids.length;start+=100) {
    const page=ids.slice(start,start+100);
    const rows=(await dbFor(env).prepare(`SELECT episode_id FROM retired_episode_ids
      WHERE episode_id IN (${page.map((_,i)=>`?${i+1}`).join(",")})`).bind(...page).all()).results;
    rows.forEach(row=>retired.add(row.episode_id));
  }
  return events.filter(row=>!retired.has(row.episode?.episode_id) && currentRetirementRecord(row.episode,markers));
}

function validateAction(action, now) {
  const fail=()=>{throw Object.assign(new Error("token_retirement_evidence_invalid"),{status:400});};
  if (!action || typeof action !== "object" || !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(action.token_address || "")) fail();
  const at=stamp(action.retired_at), cap=action.mcap_usd;
  if (!at || at>now+300000 || typeof cap !== "number" || !Number.isFinite(cap) || cap<=0) fail();
  if (action.operation === "retire") {
    const since=stamp(action.below_since), last=stamp(action.last_quote_at), duration=last-since;
    if (!since || duration<86400000 || !Number.isSafeInteger(action.samples) || action.samples<13
        || typeof action.max_gap_seconds!=="number" || action.max_gap_seconds<=0 || action.max_gap_seconds>7200
        || action.samples<Math.ceil(duration/(action.max_gap_seconds*1000))+1
        || last>at || at-last>1200000 || now-at>1200000 || cap>=20000) fail();
  } else if (action.operation === "recapture") {
    const signal=stamp(action.signal_at), active=stamp(action.reactivated_at);
    if (!signal || signal<=at || active<signal || active>now+300000 || now-active>1200000
        || cap<30000 || cap>500000 || typeof action.growth_pct!=="number" || !Number.isFinite(action.growth_pct)
        || action.growth_pct<3 || typeof action.net_buy_sol!=="number" || !Number.isFinite(action.net_buy_sol)
        || action.net_buy_sol<=0 || !stamp(action.quote_at) || !stamp(action.attention_at)
        || active-stamp(action.quote_at)<0 || active-stamp(action.quote_at)>1200000
        || active-stamp(action.attention_at)<0 || active-stamp(action.attention_at)>1800000
        || !Number.isSafeInteger(action.price_samples) || action.price_samples<4
        || !stamp(action.price_start_at) || stamp(action.price_start_at)<=at
        || stamp(action.price_observed_at)<=stamp(action.price_start_at)
        || active-stamp(action.price_observed_at)<0 || active-stamp(action.price_observed_at)>1200000) fail();
  } else fail();
}

export async function updateRetirements(env, actions, {now=Date.now()}={}) {
  if (!retirementEnabled(env)) throw new Error("token_retirement_disabled");
  if (!Array.isArray(actions) || !actions.length || actions.length>25) throw new Error("token_retirement_actions_invalid");
  actions.forEach(action=>validateAction(action,now));
  const db=dbFor(env), statements=[], updated=new Date(now).toISOString();
  for (const action of actions) {
    if (action.operation === "retire") {
      statements.push(db.prepare(`INSERT INTO token_retirements
        (token_address,retired_at,reason,cleanup_before,updated_at) VALUES(?1,?2,'below_20k_24h',?2,?3)
        ON CONFLICT(token_address) DO UPDATE SET retired_at=excluded.retired_at,reactivated_at=NULL,
        last_signal_at=NULL,cleanup_before=excluded.cleanup_before,cleanup_phase=0,cleanup_episode=NULL,
        cleanup_pending=1,updated_at=excluded.updated_at
        WHERE julianday(excluded.retired_at)>julianday(token_retirements.retired_at)
          AND (token_retirements.reactivated_at IS NULL OR julianday(excluded.retired_at)>julianday(token_retirements.reactivated_at))`)
        .bind(action.token_address,action.retired_at,updated));
      statements.push(db.prepare(`INSERT OR IGNORE INTO retired_episode_ids(episode_id,token_address,retired_at)
        SELECT e.episode_id,e.token_address,r.retired_at FROM signal_episodes e
        JOIN token_retirements r ON r.token_address=e.token_address
        WHERE r.token_address=?1 AND julianday(e.caught_at)<=julianday(r.retired_at)`)
        .bind(action.token_address));
      for (const table of ["history_sql_queue_events","history_sql_cutover_events"]) {
        statements.push(db.prepare(`INSERT OR IGNORE INTO retired_episode_ids(episode_id,token_address,retired_at)
          SELECT q.episode_id,r.token_address,r.retired_at FROM ${table} q
          JOIN token_retirements r ON r.token_address=json_extract(q.payload_json,'$.episode.token_address')
          WHERE json_valid(q.payload_json) AND r.token_address=?1
            AND julianday(json_extract(q.payload_json,'$.episode.caught_at'))<=julianday(r.retired_at)`)
          .bind(action.token_address));
      }
    } else {
      statements.push(db.prepare(`UPDATE token_retirements SET reactivated_at=?3,last_signal_at=?4,updated_at=?5
        WHERE token_address=?1 AND retired_at=?2
          AND (reactivated_at IS NULL OR julianday(reactivated_at)<=julianday(?3))`)
        .bind(action.token_address,action.retired_at,action.reactivated_at,action.signal_at,updated));
    }
  }
  checked(await db.batch(statements));
  const records=Object.values(await retirementMarkers(env,actions.map(action=>action.token_address)));
  for (const action of actions) {
    const row=records.find(row=>row.token_address===action.token_address);
    if (row?.retired_at!==action.retired_at || Boolean(row.reactivated_at)!==(action.operation==="recapture")) {
      throw Object.assign(new Error("token_retirement_conflict"),{status:409});
    }
  }
  const stored=await readSqlRuntime(env,"dashboard");
  if (stored?.value) {
    const filtered=await guardRetirementSnapshot(env,stored.value);
    await writeSqlRuntime(env,"dashboard",filtered,updated,(stored.revision || 0)+1);
  }
  return {ok:true,records,cleanup:await cleanupRetirements(env,{now,maxQueries:12})};
}

const CHILDREN=["signal_outcomes","wallet_observation_bundles","wallet_observations",
  "signal_episode_events","signal_wallets","wallet_cluster_edge_evidence","history_maintenance_dirty",
  "history_sql_queue_events","history_sql_cutover_events"];

export async function cleanupRetirements(env,{now=Date.now(),maxQueries=8}={}) {
  if (!retirementEnabled(env)) return {enabled:false,queries:0};
  if (!Number.isSafeInteger(maxQueries) || maxQueries<4 || maxQueries>16) throw new Error("token_retirement_cleanup_budget_invalid");
  const db=dbFor(env), result={enabled:true,queries:0,deleted_rows:0,completed_tokens:0};
  const run=async sql=>{result.queries++;const value=await sql.run();checked([value]);return value;};
  const all=async sql=>{result.queries++;return (await sql.all()).results;};
  const [busy]=await all(db.prepare(`SELECT 1 busy WHERE EXISTS(SELECT 1 FROM history_cluster_lock WHERE job_id IS NOT NULL)
    OR EXISTS(SELECT 1 FROM history_maintenance_jobs WHERE completed_at IS NULL AND lease_until>?1)`)
    .bind(new Date(now).toISOString()));
  if (busy) return {...result,deferred_reason:"derived_history_busy"};
  const removed=await run(db.prepare(`DELETE FROM runtime_sql_documents WHERE rowid IN
    (SELECT d.rowid FROM token_retirements r JOIN runtime_sql_documents d ON d.token_key=r.token_address
      WHERE d.content_id IS NOT NULL AND d.touched_at<?1
        AND NOT EXISTS(SELECT 1 FROM runtime_sql_references x WHERE x.part=d.name) LIMIT 128)`)
    .bind(now-3600000));
  result.deleted_rows+=removed.meta?.changes || 0;
  while (result.queries+4<=maxQueries) {
    const [job]=await all(db.prepare(`SELECT * FROM token_retirements WHERE cleanup_pending=1
      ORDER BY updated_at,token_address LIMIT 1`));
    if (!job) return result;
    const [episode]=await all(db.prepare(`SELECT episode_id,mcap_band,liquidity_band,age_band,signal_family
      FROM signal_episodes WHERE token_address=?1 AND julianday(caught_at)<=julianday(?2)
      ORDER BY caught_at,episode_id LIMIT 1`).bind(job.token_address,job.cleanup_before));
    if (!episode) {
      // Only per-token documents; shared compressed checkpoint chunks are collected
      // after the scanner republishes a purged root, never by deleting sibling data.
      const statements=[db.prepare(`DELETE FROM discovery_state WHERE token_key IN (?1,?2) AND julianday(updated_at)<=julianday(?3)`)
          .bind(job.token_address,`solana:${job.token_address}`,job.cleanup_before),
        db.prepare(`DELETE FROM alerts WHERE token_key IN (?1,?2) AND julianday(generated_at)<=julianday(?3)`)
          .bind(job.token_address,`solana:${job.token_address}`,job.cleanup_before),
        db.prepare(`DELETE FROM state_docs WHERE key IN (?1,?2) AND julianday(source_updated_at)<=julianday(?3)`)
          .bind(`signal_thesis:${job.token_address}`,`signal_thesis:solana:${job.token_address}`,job.cleanup_before),
        db.prepare(`DELETE FROM history_sql_queue_events WHERE rowid IN (SELECT q.rowid FROM history_sql_queue_events q
          JOIN retired_episode_ids r ON r.episode_id=q.episode_id WHERE r.token_address=?1 LIMIT 128)`).bind(job.token_address),
        db.prepare(`DELETE FROM history_sql_cutover_events WHERE rowid IN (SELECT q.rowid FROM history_sql_cutover_events q
          JOIN retired_episode_ids r ON r.episode_id=q.episode_id WHERE r.token_address=?1 LIMIT 128)`).bind(job.token_address),
        db.prepare(`UPDATE token_retirements SET cleanup_pending=0,cleanup_episode=NULL,cleanup_phase=0
          WHERE token_address=?1 AND cleanup_before=?2 AND cleanup_pending=1
            AND NOT EXISTS(SELECT 1 FROM history_sql_queue_events q JOIN retired_episode_ids r ON r.episode_id=q.episode_id WHERE r.token_address=?1)
            AND NOT EXISTS(SELECT 1 FROM history_sql_cutover_events q JOIN retired_episode_ids r ON r.episode_id=q.episode_id WHERE r.token_address=?1)`)
          .bind(job.token_address,job.cleanup_before)];
      result.queries++; const batch=checked(await db.batch(statements));
      result.deleted_rows+=batch.slice(0,-1).reduce((n,row)=>n+(row.meta?.changes || 0),0);
      result.completed_tokens+=batch.at(-1).meta?.changes || 0;
      continue;
    }
    const id=episode.episode_id, phase=job.cleanup_episode===id ? job.cleanup_phase : 0;
    let sql;
    if (phase===0) sql=db.prepare(`DELETE FROM market_baselines WHERE mcap_band=?1 AND liquidity_band=?2
      AND age_band=?3 AND signal_family=?4`).bind(episode.mcap_band,episode.liquidity_band,episode.age_band,episode.signal_family);
    else if (phase===1) sql=db.prepare(`UPDATE wallet_scores SET numeric_contract_version=1,confidence='unproven',edge_score=0
      WHERE wallet_address IN (SELECT w.wallet_address FROM signal_wallets w JOIN wallet_scores s
        ON s.wallet_address=w.wallet_address WHERE w.episode_id=?1 AND s.numeric_contract_version>=2 LIMIT 128)`).bind(id);
    else if (phase===2) sql=db.prepare(`UPDATE wallet_clusters SET numeric_contract_version=1,confidence='unproven',edge_score=0
      WHERE cluster_id IN (SELECT c.cluster_id FROM wallet_cluster_members m JOIN wallet_clusters c ON c.cluster_id=m.cluster_id
        JOIN signal_wallets w ON w.wallet_address=m.wallet_address WHERE w.episode_id=?1 AND c.numeric_contract_version>=2 LIMIT 128)`).bind(id);
    else if (phase===3) {
      // Recompute shared edges from their remaining proofs, not the retired proof.
      sql=db.prepare(`UPDATE wallet_cluster_edges SET evidence_count=(SELECT COUNT(*) FROM wallet_cluster_edge_evidence x
          WHERE x.edge_id=wallet_cluster_edges.edge_id AND x.episode_id!=?1),
        evidence_json=(SELECT json_group_array(json_object('episode_id',x.episode_id,'observed_at',x.observed_at))
          FROM wallet_cluster_edge_evidence x WHERE x.edge_id=wallet_cluster_edges.edge_id AND x.episode_id!=?1)
        WHERE edge_id IN (SELECT edge_id FROM wallet_cluster_edge_evidence WHERE episode_id=?1 ORDER BY edge_id LIMIT 128)`)
        .bind(id);
    } else if (phase<4+CHILDREN.length) {
      const table=CHILDREN[phase-4];
      sql=db.prepare(`DELETE FROM ${table} WHERE rowid IN (SELECT rowid FROM ${table} WHERE episode_id=?1 LIMIT 128)`).bind(id);
    } else sql=db.prepare("DELETE FROM signal_episodes WHERE episode_id=?1 AND token_address=?2 AND julianday(caught_at)<=julianday(?3)")
      .bind(id,job.token_address,job.cleanup_before);
    // Phase 3's proofs must be removed in the same transaction, else repeated
    // bounded edge pages would process the first 128 entries forever.
    let changes;
    if (phase===3) {
      result.queries++;
      const rows=checked(await db.batch([sql,
        db.prepare(`DELETE FROM wallet_cluster_edge_evidence WHERE rowid IN
          (SELECT rowid FROM wallet_cluster_edge_evidence WHERE episode_id=?1 ORDER BY edge_id LIMIT 128)`).bind(id),
        db.prepare(`DELETE FROM wallet_cluster_edges WHERE edge_id IN
          (SELECT edge_id FROM wallet_cluster_edges WHERE evidence_count=0 LIMIT 128)`)]));
      changes=rows[1].meta?.changes || 0;
      result.deleted_rows+=changes+(rows[2].meta?.changes || 0);
    } else {
      const row=await run(sql);changes=row.meta?.changes || 0;
      if (phase>=4) result.deleted_rows+=changes;
    }
    if (result.queries>=maxQueries) return {...result,pending:true};
    const next=phase<4+CHILDREN.length ? phase+(changes<128 ? 1 : 0) : 0;
    await run(db.prepare(`UPDATE token_retirements SET cleanup_episode=?3,cleanup_phase=?4
      WHERE token_address=?1 AND cleanup_before=?2 AND cleanup_pending=1`)
      .bind(job.token_address,job.cleanup_before,phase<4+CHILDREN.length ? id : null,next));
  }
  return {...result,pending:true};
}
