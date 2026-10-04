import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import test from "node:test";
import {createHash} from "node:crypto";
import {archiveHistoryEvent} from "../src/archive.js";
import {estimatedHistoryQueries, resumeHistoryEvent} from "../src/history-progress.js";
import {historyMaintenanceStatus, markHistoryDerivedDirty, pruneArchivedHistoryOutbox,
  runHistoryMaintenance} from "../src/history-maintenance.js";

const NOW = "2026-10-03T00:37:00.000Z";
const NEXT = "2026-10-04T00:37:00.000Z";
const LATER = "2026-10-03T12:00:00.000Z";
const CAUGHT = "2026-09-28T00:00:00.000Z";
const migration = () => readFileSync(new URL("../migrations-storage/0001_daily_learning.sql",import.meta.url),"utf8");

function fixture(t, {maintenance = true} = {}) {
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  for (const name of ["0001_wallet_edge_history.sql","0002_cluster_edge_evidence.sql","0003_resumable_history.sql"]) {
    sqlite.exec(readFileSync(new URL(`../migrations-history/${name}`,import.meta.url),"utf8"));
  }
  if (maintenance) sqlite.exec(migration());
  const db = {sqlite, calls:[], before:null};
  const execute = async (sql,values,method) => {
    db.calls.push({sql,values,method});
    const override = await db.before?.({sql,values,method});
    if (override !== undefined) return override;
    const statement = sqlite.prepare(sql);
    const args = /\?\d+/.test(sql) ? [Object.fromEntries(values.map((value,i) => [i+1,value]))] : values;
    if (method === "run") {
      const result = statement.run(...args);
      return {success:true,meta:{changes:Number(result.changes),rows_written:Number(result.changes)}};
    }
    const rows = statement.all(...args);
    return method === "first" ? rows[0] ?? null : {success:true,results:rows};
  };
  db.prepare = sql => {
    const prepared = values => ({
      bind:(...next) => prepared(next),run:() => execute(sql,values,"run"),
      all:() => execute(sql,values,"all"),first:() => execute(sql,values,"first"),
    });
    return prepared([]);
  };
  db.batch = async statements => {
    sqlite.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      sqlite.exec("COMMIT");
      return results;
    } catch (error) { sqlite.exec("ROLLBACK"); throw error; }
  };
  return {db,env:{RADAR_DB:db,RADAR_HISTORY_DB:db,HISTORY_DERIVED_MODE:"daily"}};
}

function signal(id, wallets = ["wallet-a"], token = id) {
  return {event_id:`signal:${id}`,episode:{episode_id:id,token_address:`token:${token}`,caught_at:CAUGHT,
    caught_mcap_usd:80_000,caught_liquidity_usd:20_000,token_age_days:40},
  event:{event_type:"signal",observed_at:CAUGHT},
  wallets:wallets.map(wallet => ({wallet_address:wallet,buy_count:1,buy_sol:2,bought_tokens:100,
    current_token_balance:100,retained_pct:100,common_funder:"private-funder",evidence_status:"complete"}))};
}

function outcome(row, {result = 120,
  observed = new Date(Date.parse(row.episode.caught_at)+4320*60_000).toISOString(), id = "72h"} = {}) {
  return {...structuredClone(row),event_id:`outcome:${row.episode.episode_id}:${id}`,wallets:[],
    event:{event_type:"outcome_72h",observed_at:observed},
    outcome:{entry_evidence_version:2,caught_at:row.episode.caught_at,caught_mcap_usd:80_000,horizons:{"72h":{
      at:observed,target_at:new Date(Date.parse(row.episode.caught_at)+4320*60_000).toISOString(),
      max_return_pct:result,return_pct:result,
      liquidity_usd:20_000,quality_status:"complete",max_drawdown_pct:-20,time_to_2x_minutes:120,
    }}}};
}

async function ingest(f,row, {mode = "daily", now = "2026-10-02T12:00:00.000Z",state = {}} = {}) {
  await resumeHistoryEvent(f.db,row,state,{now,remaining:() => 10_000,derivedMode:mode});
  assert.equal(state.phase,"done");
  return state;
}

async function finish(f, {now = NOW,maxQueries = 12} = {}) {
  const invocations = [];
  for (let i = 0; i < 250; i++) {
    const row = await runHistoryMaintenance(f.env,{now,maxQueries});
    invocations.push(row);
    assert.ok(row.queries<=maxQueries);
    assert.ok(row.writes<=maxQueries);
    if (row.job?.phase === "done") return invocations;
  }
  throw new Error("maintenance did not finish");
}

test("daily ingestion keeps source records and immediate graph topology, not per-event scores", async t => {
  const f = fixture(t), original = signal("one",["a","b"]);
  await ingest(f,original);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_wallets").get().n,2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges").get().n,1);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_clusters WHERE active=1").get().n,1);
  const before = f.db.sqlite.prepare("SELECT * FROM signal_wallets ORDER BY wallet_address").all();
  const source = outcome(original);
  await ingest(f,source);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM market_baselines").get().n,0);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_scores").get().n,0);
  assert.deepEqual(f.db.sqlite.prepare("SELECT * FROM signal_wallets ORDER BY wallet_address").all(),before);
  await ingest(f,source,{now:LATER});
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,2);
  assert.equal(f.db.sqlite.prepare("SELECT ready_at FROM history_maintenance_dirty WHERE event_id=?").get(source.event_id).ready_at,
    "2026-10-02T12:00:00.000Z");
});

test("existing per-event mode works without the daily migration or extra writes", async t => {
  const f = fixture(t,{maintenance:false});
  const original = signal("old");
  await ingest(f,original,{mode:"per_event"});
  await ingest(f,outcome(original),{mode:"per_event"});
  assert.equal(f.db.sqlite.prepare("SELECT eligible_episodes FROM wallet_scores").get().eligible_episodes,1);
  const calls = f.db.calls.length;
  const result = await runHistoryMaintenance({...f.env,HISTORY_DERIVED_MODE:"per_event"});
  assert.equal(result.enabled,false);
  assert.equal(f.db.calls.length,calls);
});

test("daily queue estimates admit lightweight outcomes without changing old mode estimates", () => {
  const row = outcome(signal("estimate"));
  assert.equal(estimatedHistoryQueries(row,{},"daily"),8);
  assert.equal(estimatedHistoryQueries(row),22);
  assert.equal(estimatedHistoryQueries(row,{phase:"derived_dirty"},"daily"),1);
  assert.equal(estimatedHistoryQueries(row,{phase:"scores",locked:true},"daily"),2);
  row.wallets=Array.from({length:30},(_,i) => ({wallet_address:`w${i}`}));
  assert.equal(estimatedHistoryQueries(row,{},"daily"),13);
  assert.equal(estimatedHistoryQueries(signal("topology"),{},"daily"),40);
});

test("bounded daily jobs resume and recompute all affected-band wallets before cluster scores", async t => {
  const f = fixture(t);
  const first = signal("first",["a","b"]), second = signal("second",["c"]);
  await ingest(f,first);
  await ingest(f,second);
  await ingest(f,outcome(first));
  await ingest(f,outcome(second,{result:0}));
  // Only the first episode is dirty: its changed baseline still affects c.
  f.db.sqlite.exec("DELETE FROM history_maintenance_dirty WHERE episode_id='second'");
  const frozen = f.db.sqlite.prepare("SELECT * FROM signal_wallets ORDER BY episode_id,wallet_address").all();
  const invocations = await finish(f);
  assert.ok(invocations.length>2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_scores").get().n,3);
  assert.equal(f.db.sqlite.prepare("SELECT hit_2x_rate,eligible_episodes FROM market_baselines WHERE horizon_minutes=4320").get().hit_2x_rate,0.5);
  const cluster = f.db.sqlite.prepare("SELECT * FROM wallet_clusters WHERE active=1").get();
  assert.equal(cluster.computed_through,NOW);
  assert.equal(cluster.eligible_episodes,2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,0);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,4);
  assert.deepEqual(f.db.sqlite.prepare("SELECT * FROM signal_wallets ORDER BY episode_id,wallet_address").all(),frozen);
  const baselineLast = f.db.calls.findLastIndex(row => /INSERT INTO market_baselines/.test(row.sql));
  const walletFirst = f.db.calls.findIndex(row => /INSERT INTO wallet_scores/.test(row.sql));
  assert.ok(walletFirst>baselineLast);
  assert.ok(f.db.calls.filter(row => /INSERT INTO market_baselines/.test(row.sql)).length<=1,
    "identical bands should be recalculated once per horizon, not once per episode");
});

test("fixed UTC cutoff excludes future outcomes and arrivals stay dirty for the next day", async t => {
  const f = fixture(t), original = signal("cutoff");
  original.episode.caught_at="2026-09-30T01:00:00.000Z";
  original.event.observed_at=original.episode.caught_at;
  await ingest(f,original);
  const future = outcome(original,{observed:"2026-10-03T01:00:00.000Z"});
  // A future result can be stored but must not become eligible retrospectively.
  await ingest(f,future);
  assert.equal(f.db.sqlite.prepare("SELECT status FROM signal_outcomes WHERE episode_id='cutoff' AND horizon_minutes=4320").get().status,
    "complete","test must use an otherwise eligible future outcome");
  await runHistoryMaintenance(f.env,{now:NOW,maxQueries:12});
  const later = signal("later",["later-wallet"]);
  await ingest(f,later,{now:LATER});
  await ingest(f,outcome(later),{now:LATER});
  await finish(f);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_scores WHERE numeric_contract_version>=2").get().n,0);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty WHERE episode_id='later'").get().n,2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty WHERE episode_id='cutoff'").get().n,1,
    "future source is protected even when ready_at was older");
  const repeat = await runHistoryMaintenance(f.env,{now:LATER});
  assert.equal(repeat.reason,"history_maintenance_daily_complete");
  await finish(f,{now:NEXT});
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,0);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_scores WHERE numeric_contract_version>=2").get().n,2,
    "future outcome becomes eligible only at the next daily cutoff");
});

test("keyset wallet pages persist across invocations instead of rescanning for every wallet", async t => {
  const f = fixture(t);
  const original = signal("paged",Array.from({length:30},(_,i) => `wallet-${String(i).padStart(3,"0")}`));
  original.wallets.forEach(row => { row.common_funder=null; });
  await ingest(f,original);
  await ingest(f,outcome(original));
  const invocations = await finish(f,{maxQueries:40});
  assert.ok(invocations.length>3);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM wallet_scores").get().n,30);
  const pages = f.db.calls.filter(row => /SELECT DISTINCT w.wallet_address FROM signal_wallets w/.test(row.sql));
  assert.equal(pages.length,3,"25-wallet keyset pages plus the final empty page");
  const baselineReads = f.db.calls.filter(row => /FROM market_baselines\s+WHERE horizon_minutes = 4320/.test(row.sql));
  assert.ok(baselineReads.length<10,"reuse baselines within each invocation, not one read per wallet");
});

test("failure after a derived write replays safely without changing sources or frozen priors", async t => {
  const f = fixture(t), original = signal("replay");
  await ingest(f,original);
  await ingest(f,outcome(original));
  const frozen = f.db.sqlite.prepare("SELECT * FROM signal_wallets").all();
  let wrote = false;
  f.db.before = ({sql}) => {
    if (/INSERT INTO market_baselines/.test(sql)) wrote=true;
    else if (wrote && /SET phase=/.test(sql)) throw new Error("cursor unavailable");
  };
  await assert.rejects(runHistoryMaintenance(f.env,{now:NOW,maxQueries:40}),/cursor unavailable/);
  f.db.before=null;
  await finish(f);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM market_baselines").get().n,1);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,2);
  assert.deepEqual(f.db.sqlite.prepare("SELECT * FROM signal_wallets").all(),frozen);
});

test("a replay during the daily job preserves its marker for the next cutoff", async t => {
  const f = fixture(t), original = signal("concurrent-replay");
  const source = outcome(original);
  await ingest(f,original);
  await ingest(f,source);
  await runHistoryMaintenance(f.env,{now:NOW,maxQueries:7});
  await ingest(f,source,{now:LATER});
  await finish(f);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,1);
  const pending = f.db.sqlite.prepare("SELECT * FROM history_maintenance_dirty").get();
  assert.equal(pending.event_id,source.event_id);
  assert.equal(pending.ready_at,"2026-10-02T12:00:00.000Z");
  assert.equal(pending.last_ready_at,LATER);
  await finish(f,{now:NEXT});
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,0);
  assert.equal(f.db.sqlite.prepare("SELECT eligible_episodes FROM wallet_scores").get().eligible_episodes,1);
});

test("dry budgets defer without SQL and write/query limits protect every invocation", async t => {
  const f = fixture(t), original = signal("budget");
  await ingest(f,original);
  await ingest(f,outcome(original));
  const calls = f.db.calls.length;
  const result = await runHistoryMaintenance(f.env,{now:NOW,maxQueries:0,maxWrites:0});
  assert.equal(result.deferred,true);
  assert.equal(result.queries,0);
  assert.equal(f.db.calls.length,calls);
  for (const maxQueries of [1,5,7,10,12]) {
    const next = await runHistoryMaintenance(f.env,{now:NOW,maxQueries,maxWrites:3});
    assert.ok(next.queries<=maxQueries);
    assert.ok(next.writes<=3);
  }
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,2);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,2);
  await finish(f);
});

test("a missing daily migration cannot acknowledge source work; installing it resumes the same event", async t => {
  const f = fixture(t,{maintenance:false}), original = signal("schema");
  const state = {};
  await assert.rejects(resumeHistoryEvent(f.db,original,state,{now:NOW,remaining:() => 1000,derivedMode:"daily"}),
    /history_maintenance_dirty/);
  assert.equal(state.phase,"derived_dirty");
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
  f.db.sqlite.exec(migration());
  await ingest(f,original,{now:NOW,state});
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,1);
});

test("busy lease defers, stale lease resumes the original cutoff across a UTC day", async t => {
  const f = fixture(t), original = signal("lease");
  await ingest(f,original);
  await ingest(f,outcome(original));
  await runHistoryMaintenance(f.env,{now:NOW,maxQueries:7});
  f.db.sqlite.prepare("UPDATE history_maintenance_jobs SET lease_token='other',lease_until=?").run(LATER);
  const busy = await runHistoryMaintenance(f.env,{now:NOW});
  assert.equal(busy.reason,"history_maintenance_busy");
  const invocations = await finish(f,{now:NEXT});
  assert.equal(invocations.at(-1).job.cutoff_at,NOW);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_jobs").get().n,1);
  const status = await historyMaintenanceStatus(f.env);
  assert.equal(status.pending_events,0);
  assert.equal(status.job.phase,"done");
});

test("invalid modes and budgets reject; retention events do not create Learning markers", async t => {
  const f = fixture(t), original = signal("retention");
  await ingest(f,original);
  const recheck = {...original,event_id:"retention:one",event:{event_type:"retention_check",observed_at:NOW}};
  await ingest(f,recheck,{now:NOW});
  assert.equal(await markHistoryDerivedDirty(f.db,recheck,NOW),false);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_maintenance_dirty").get().n,1);
  await assert.rejects(runHistoryMaintenance({...f.env,HISTORY_DERIVED_MODE:"hourly"}),/mode_invalid/);
  await assert.rejects(runHistoryMaintenance(f.env,{maxQueries:101}),/budget_invalid/);
});

test("retention removes only identical archived delivered copies, never pending or unmatched sources", async t => {
  const f = fixture(t);
  f.db.sqlite.exec(`CREATE TABLE history_outbox (event_id TEXT PRIMARY KEY,payload_json TEXT,status TEXT,delivered_at TEXT)`);
  f.db.sqlite.exec(readFileSync(new URL("../migrations-storage/0004_history_outbox_cleanup_index.sql",import.meta.url),"utf8"));
  const objects = new Map();
  const metadata = row => row ? {key:row.key,size:row.bytes.byteLength,customMetadata:row.metadata,
    checksums:{sha256:Uint8Array.from(Buffer.from(row.sha256,"hex")).buffer}} : null;
  f.env.RADAR_ARCHIVE = {
    head:async key => metadata(objects.get(key)),
    put:async (key,bytes,options) => {
      objects.set(key,{key,bytes:Uint8Array.from(bytes),metadata:options.customMetadata,
        sha256:createHash("sha256").update(bytes).digest("hex")});
      return metadata(objects.get(key));
    },
    get:async key => {
      const row = objects.get(key);
      return row ? {...metadata(row),body:new Blob([row.bytes]).stream()} : null;
    },
  };
  for (const [id,status,proof] of [["safe","delivered",true],["pending","pending",true],
    ["missing","delivered",false],["mismatch","delivered",true]]) {
    const original = signal(id), payload = JSON.stringify(original);
    await ingest(f,original);
    if (proof) {
      const ref = await archiveHistoryEvent(f.env,original);
      f.db.sqlite.prepare("UPDATE signal_episode_events SET payload_json=?,raw_object_key=? WHERE event_id=?")
        .run(JSON.stringify({archive_ref:ref}),ref.key,original.event_id);
    }
    f.db.sqlite.prepare("INSERT INTO history_outbox VALUES (?,?,?,?)")
      .run(original.event_id,id === "mismatch" ? JSON.stringify({...original,changed_evidence:true}) : payload,
        status,"2026-09-20T00:00:00.000Z");
  }
  const result = await pruneArchivedHistoryOutbox(f.env,{now:NOW,maxQueries:3});
  assert.equal(result.deleted,1);
  assert.ok(result.queries<=3);
  assert.deepEqual(f.db.sqlite.prepare("SELECT event_id FROM history_outbox ORDER BY event_id").all()
    .map(row => row.event_id),["signal:mismatch","signal:missing","signal:pending"]);
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,4);
  assert.equal((await pruneArchivedHistoryOutbox({...f.env,RADAR_ARCHIVE:null})).deleted,0);
  assert.equal((await pruneArchivedHistoryOutbox({...f.env,RADAR_HISTORY_DB:{}})).reason,
    "history_retention_canonical_db_required");
  const capped = await pruneArchivedHistoryOutbox(f.env,{now:NOW,maxRequests:1});
  assert.equal(capped.reason,"history_retention_request_budget");
  assert.equal(capped.deleted,0);
  assert.equal(capped.queries+capped.archive_reads,1);
  const matched = f.db.sqlite.prepare("SELECT payload_json FROM signal_episode_events WHERE event_id='signal:mismatch'").get();
  const ref = JSON.parse(matched.payload_json).archive_ref;
  const row = objects.get(ref.key);
  row.bytes[10]^=1;
  const corrupt = await pruneArchivedHistoryOutbox(f.env,{now:NOW});
  assert.equal(corrupt.deferred,true);
  assert.equal(corrupt.deleted,0);
  assert.equal(corrupt.after,"");
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_outbox").get().n,3);
});

test("missing cleanup index defers explicitly without silently initializing schema or touching payloads", async t => {
  const f = fixture(t);
  f.db.sqlite.exec("CREATE TABLE history_outbox (event_id TEXT PRIMARY KEY,payload_json TEXT,status TEXT,delivered_at TEXT)");
  f.env.RADAR_ARCHIVE={get:async () => assert.fail("no R2 read before the cleanup migration")};
  const result = await pruneArchivedHistoryOutbox(f.env,{now:NOW});
  assert.equal(result.enabled,false);assert.equal(result.reason,"history_retention_index_migration_required");
  assert.equal(result.queries,1);assert.equal(result.deleted,0);
  assert.ok(!f.db.calls.some(row => /CREATE|ALTER|DELETE/.test(row.sql)));
});

test("cleanup EXPLAIN keyset selects covering metadata before bounded primary-key JSON lookups", async t => {
  const f = fixture(t);
  f.db.sqlite.exec("CREATE TABLE history_outbox (event_id TEXT PRIMARY KEY,payload_json TEXT,status TEXT,delivered_at TEXT)");
  f.db.sqlite.exec(readFileSync(new URL("../migrations-storage/0004_history_outbox_cleanup_index.sql",import.meta.url),"utf8"));
  const insert = f.db.sqlite.prepare("INSERT INTO history_outbox VALUES (?,?,?,?)");
  for (let i=0;i<1000;i++) insert.run(`event-${String(i).padStart(4,"0")}`,"x".repeat(5000),i%2 ? "pending" : "delivered","2026-09-20T00:00:00.000Z");
  f.env.RADAR_ARCHIVE={get:async () => assert.fail("unmatched candidates must not read R2")};
  const first = await pruneArchivedHistoryOutbox(f.env,{now:NOW,pageSize:5,after:"event-0100"});
  assert.equal(first.checked,5);assert.equal(first.after,"event-0110");assert.equal(first.complete,false);
  const selection = f.db.calls.find(row => row.sql.includes("WITH candidates AS MATERIALIZED"));
  const plan = f.db.sqlite.prepare(`EXPLAIN QUERY PLAN ${selection.sql}`)
    .all(Object.fromEntries(selection.values.map((value,i) => [i+1,value]))).map(row => row.detail);
  assert.ok(plan.some(detail => /SEARCH history_outbox USING COVERING INDEX idx_history_outbox_delivered_keyset \(event_id>\?\)/.test(detail)),plan.join("\n"));
  assert.ok(plan.some(detail => /SEARCH o USING INDEX sqlite_autoindex_history_outbox_1/.test(detail)),plan.join("\n"));
  assert.ok(plan.some(detail => /SEARCH e USING INDEX sqlite_autoindex_signal_episode_events_1/.test(detail)),plan.join("\n"));
  assert.ok(!plan.some(detail => /^SCAN (?:history_outbox|o|e)\b/.test(detail)),plan.join("\n"));
  const next = await pruneArchivedHistoryOutbox(f.env,{now:NOW,pageSize:5,after:first.after});
  assert.equal(next.checked,5);assert.equal(next.after,"event-0120");
  assert.equal(f.db.sqlite.prepare("SELECT COUNT(*) n FROM history_outbox").get().n,1000);
});
