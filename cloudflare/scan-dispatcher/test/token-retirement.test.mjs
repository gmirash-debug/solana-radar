import assert from "node:assert/strict";
import test from "node:test";
import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";
import {updateRetirements, cleanupRetirements, retirementPage, eventIsRetired,
  filterRetiredHistoryEvents, filterRetirementSnapshot, guardRetirementSnapshot} from "../src/token-retirement.js";
import worker from "../src/index.js";

const NOW=Date.parse("2026-10-09T12:00:00Z"), HOUR=3600000;
const iso=t=>new Date(t).toISOString();
const TOKEN="CrJPSvj625TnPdWS42aG5ybMcHeFvnNqq5AExVespump", OTHER="3Ydb2n8vAFdBJYpdiZEoDxRXLmiGDY1fuizVmMPMpump";
function fixture(t) {
  const sql=new DatabaseSync(":memory:");t.after(()=>sql.close());sql.exec("PRAGMA foreign_keys=ON");
  for (const file of ["migrations/0001_radar_data.sql","migrations/0002_history_outbox.sql",
    "migrations-history/0001_wallet_edge_history.sql","migrations-history/0002_cluster_edge_evidence.sql",
    "migrations-history/0003_resumable_history.sql","migrations-storage/0001_daily_learning.sql",
    "migrations-storage/0002_runtime_sql.sql","migrations-storage/0003_sql_queue.sql",
    "migrations-storage/0005_cutover_staging.sql","migrations-storage/0006_storage_retention.sql",
    "migrations-storage/0007_token_retirement.sql"]) sql.exec(readFileSync(new URL(`../${file}`,import.meta.url),"utf8"));
  const f={sql,calls:[],fail:null};
  const execute=(text,values,method)=> {
    f.calls.push(text);if(f.fail?.(text)) throw new Error("injected_sql_failure");
    const args=/\?\d+/.test(text) ? [Object.fromEntries(values.map((v,i)=>[i+1,v]))] : values;
    const rows=sql.prepare(text).all(...args);
    return method==="first" ? rows[0] || null : {success:true,results:rows,
      meta:{changes:Number(sql.prepare("SELECT changes() n").get().n)}};
  };
  const db={prepare(text) {
    const wrap=values=>({bind:(...next)=>wrap(next),first:async()=>execute(text,values,"first"),
      all:async()=>execute(text,values,"all"),run:async()=>execute(text,values,"run")});return wrap([]);
  },async batch(statements) {
    sql.exec("BEGIN IMMEDIATE");
    try {const result=[];for(const row of statements) result.push(await row.run());sql.exec("COMMIT");return result;}
    catch(error){sql.exec("ROLLBACK");throw error;}
  }};
  const forbidden=new Proxy({}, {get(){assert.fail("No R2, RPC, DO or external service in retirement");}});
  f.env={RADAR_DB:db,RADAR_HISTORY_DB:db,STORAGE_SQL_BACKEND:"turso",RUNTIME_STORAGE_BACKEND:"turso_sql",
    TOKEN_RETIREMENT_ENABLED:"true",RADAR_ARCHIVE:forbidden,RUNTIME_SNAPSHOTS:forbidden,R2_BUDGET:forbidden};
  f.episode=(id,token=TOKEN,at=NOW-48*HOUR)=>sql.prepare(`INSERT INTO signal_episodes
    (episode_id,token_address,lane,signal_family,caught_at,last_signal_at,mcap_band,liquidity_band,age_band,created_at,updated_at)
    VALUES(?,?,'reactivation','reactivation_wave',?,?,'m','l','a',?,?)`).run(id,token,iso(at),iso(at),iso(at),iso(at));
  f.queue=(id,episode,token=TOKEN,at=NOW-48*HOUR)=>sql.prepare(`INSERT INTO history_sql_queue_events
    (event_id,episode_id,source_at,payload_json,payload_bytes,status,next_attempt_at) VALUES(?,?,?, ?,100,'pending',?)`)
    .run(id,episode,NOW,JSON.stringify({episode:{episode_id:episode,token_address:token,caught_at:iso(at)}}),NOW);
  f.marker=()=>sql.prepare("SELECT * FROM token_retirements WHERE token_address=?").get(TOKEN);
  return f;
}
const retire=()=>({operation:"retire",token_address:TOKEN,retired_at:iso(NOW),below_since:iso(NOW-24*HOUR),
  last_quote_at:iso(NOW),samples:25,max_gap_seconds:3600,mcap_usd:19000});
const recapture=()=>({operation:"recapture",token_address:TOKEN,retired_at:iso(NOW),reactivated_at:iso(NOW+HOUR),
  signal_at:iso(NOW+HOUR-600000),quote_at:iso(NOW+HOUR),attention_at:iso(NOW+HOUR),
  growth_pct:5,net_buy_sol:15,mcap_usd:35000});
const raw=(id,at=NOW-48*HOUR)=>({episode:{episode_id:id,token_address:TOKEN,caught_at:iso(at)}});

test("retirement is authenticated, epoch fenced, and never offered on a public mutating endpoint",async t=> {
  const f=fixture(t);
  f.env.STORAGE_SQL_BACKEND="d1";
  f.env.RADAR_INGEST_SECRET="test-secret";f.env.STORAGE_EPOCH="test-epoch";
  const request=headers=>new Request("https://radar/api/runtime/token-lifecycle",{method:"POST",headers,
    body:JSON.stringify({actions:[retire()]})});
  assert.equal((await worker.fetch(request({"x-radar-storage-epoch":"test-epoch"}),f.env)).status,401);
  assert.equal((await worker.fetch(request({"x-radar-ingest-secret":"test-secret"}),f.env)).status,409);
  assert.equal(f.marker(),undefined);
});

test("24-hour boundary validation rejects fake/missing quotes, sparse checks, future time, and cap 20k",async t=> {
  const f=fixture(t);
  for (const extra of [{mcap_usd:20000},{mcap_usd:0},{mcap_usd:false},{samples:12},{max_gap_seconds:7201},
    {samples:13,max_gap_seconds:3600},{below_since:iso(NOW-23*HOUR)}, {last_quote_at:iso(NOW-HOUR)},
    {retired_at:iso(NOW+HOUR)}]) {
    await assert.rejects(()=>updateRetirements(f.env,[{...retire(),...extra}],{now:NOW}),/evidence_invalid/);
  }
  assert.equal(f.marker(),undefined);
  const result=await updateRetirements(f.env,[retire()],{now:NOW});
  assert.equal(result.ok,true);assert.equal(result.records[0].reason,"below_20k_24h");
  assert.equal(result.records[0].reactivated_at,null);
});

test("SQL failures roll back the fence and do not manufacture a successful deletion",async t=> {
  const f=fixture(t);f.episode("old");
  f.fail=text=>text.includes("INSERT OR IGNORE INTO retired_episode_ids");
  await assert.rejects(()=>updateRetirements(f.env,[retire()],{now:NOW}),/injected/);
  assert.equal(f.marker(),undefined);
  assert.ok(f.sql.prepare("SELECT 1 FROM signal_episodes WHERE episode_id='old'").get());
});

test("old and not-yet-normalized queued episodes are fenced without contacting R2 or resetting counters",async t=> {
  const f=fixture(t);f.episode("old");f.queue("old-event","old");f.queue("not-started","queued-only");
  f.sql.prepare("UPDATE history_sql_queue_meta SET hist_writes=123 WHERE id=1").run();
  await updateRetirements(f.env,[retire()],{now:NOW});
  assert.equal(await eventIsRetired(f.env,raw("old")),true);
  assert.equal(await eventIsRetired(f.env,{},"queued-only"),true);
  assert.equal((await filterRetiredHistoryEvents(f.env,[raw("old"),raw("new",NOW+HOUR)])).length,0);
  assert.equal(f.sql.prepare("SELECT hist_writes FROM history_sql_queue_meta").get().hist_writes,123);
  assert.throws(()=>f.episode("replay"),/token_episode_retired/);
});

test("cleanup is resumable and removes children, original cohorts, derived claims, queues and token docs",async t=> {
  const f=fixture(t);f.episode("old");f.episode("other",OTHER);f.queue("pending","old");
  f.sql.prepare(`INSERT INTO signal_wallets(episode_id,wallet_address,cohort_role,first_buy_at,buy_sol,buy_count,
    bought_tokens,created_at,updated_at) VALUES('old','wallet','at_catch',?,1,1,10,?,?)`)
    .run(iso(NOW-48*HOUR),iso(NOW-48*HOUR),iso(NOW));
  for (let i=0;i<260;i++) f.sql.prepare(`INSERT INTO signal_episode_events
    (event_id,episode_id,observed_at,event_type,payload_json,created_at) VALUES(?,'old',?,'snapshot','raw',?)`)
    .run(`event:${i}`,iso(NOW-HOUR),iso(NOW-HOUR));
  f.sql.prepare(`INSERT INTO wallet_observation_bundles VALUES('bundle','old',?,'[1]',2)`).run(iso(NOW));
  f.sql.prepare(`INSERT INTO state_docs VALUES(?,?,?,?)`).run(`signal_thesis:${TOKEN}`,"old",iso(NOW-HOUR),iso(NOW-HOUR));
  f.sql.prepare(`INSERT INTO discovery_state(token_key,market_json,updated_at) VALUES(?,'old',?)`).run(TOKEN,iso(NOW-HOUR));
  await updateRetirements(f.env,[retire()],{now:NOW});
  let calls=0;
  while (f.marker().cleanup_pending && calls++<100) {
    const result=await cleanupRetirements(f.env,{now:NOW,maxQueries:8});
    assert.ok(result.queries<=8);
    assert.ok(result.deleted_rows<=384);
  }
  assert.ok(calls<100);assert.equal(f.marker().cleanup_pending,0);
  for (const table of ["signal_wallets","signal_episode_events","wallet_observation_bundles","history_sql_queue_events","discovery_state","state_docs"]) {
    assert.equal(f.sql.prepare(`SELECT COUNT(*) n FROM ${table}`).get().n,0,table);
  }
  assert.deepEqual(f.sql.prepare("SELECT episode_id FROM signal_episodes").all().map(row=>row.episode_id),["other"]);
  assert.ok(f.sql.prepare("SELECT 1 FROM retired_episode_ids WHERE episode_id='old'").get());
  assert.throws(()=>f.episode("old"),/token_episode_retired/);
});

test("recapture needs a rising new wave in 30k-500k, not ranking alone, and never revives old episodes",async t=> {
  const f=fixture(t);f.episode("old");await updateRetirements(f.env,[retire()],{now:NOW});
  for (const extra of [{growth_pct:0},{net_buy_sol:0},{signal_at:iso(NOW)}, {mcap_usd:29999},{mcap_usd:500001},
    {quote_at:iso(NOW)}, {attention_at:iso(NOW)}]) {
    await assert.rejects(()=>updateRetirements(f.env,[{...recapture(),...extra}],{now:NOW+HOUR}),/evidence_invalid/);
  }
  await updateRetirements(f.env,[recapture()],{now:NOW+HOUR});
  f.episode("new",TOKEN,NOW+HOUR-600000);
  assert.equal((await filterRetiredHistoryEvents(f.env,[raw("old"),raw("new",NOW+HOUR-600000)])).length,1);
  for (let i=0;i<40 && f.marker().cleanup_pending;i++) await cleanupRetirements(f.env,{now:NOW+HOUR});
  assert.ok(f.sql.prepare("SELECT 1 FROM signal_episodes WHERE episode_id='new'").get());
  assert.equal(f.sql.prepare("SELECT 1 FROM signal_episodes WHERE episode_id='old'").get(),undefined);
  assert.throws(()=>f.episode("replay",TOKEN,NOW-48*HOUR),/token_episode_retired/);
});

test("old snapshots and direct token records cannot resurrect a retired token, even after recapture",async t=> {
  const f=fixture(t);await updateRetirements(f.env,[retire()],{now:NOW});
  const old={token_address:TOKEN,signal_at:iso(NOW-HOUR)};
  const snapshot={report:{signal_theses:[old,{token_address:OTHER,signal_at:iso(NOW-HOUR)}]},
    history:[{pool:{token_address:TOKEN},created_at:iso(NOW-HOUR)}],market:{[TOKEN]:{},[OTHER]:{}},
    token_detail_refs:{[TOKEN]:{id:"raw"}},detail_signal_theses:[old]};
  const result=await guardRetirementSnapshot(f.env,snapshot);
  assert.equal(result.report.signal_theses.length,1);assert.equal(result.history.length,0);
  assert.equal(result.market[TOKEN],undefined);assert.deepEqual(result.token_detail_refs,{});
  assert.equal(snapshot.report.signal_theses.length,2);
  const marker={token_address:TOKEN,retired_at:iso(NOW),reactivated_at:iso(NOW+HOUR)};
  const active=filterRetirementSnapshot(snapshot,{[TOKEN]:marker});
  assert.equal(active.report.signal_theses.length,1);
  assert.equal(active.market[TOKEN],undefined);
});

test("retirement list is metadata-only and cleanup defers while a derived history lease is live",async t=> {
  const f=fixture(t);await updateRetirements(f.env,[retire()],{now:NOW});
  f.sql.prepare(`INSERT INTO history_maintenance_jobs(job_day,cutoff_at,lease_until,created_at,updated_at)
    VALUES('day',?,?,?,?)`).run(iso(NOW),iso(NOW+HOUR),iso(NOW),iso(NOW));
  const result=await cleanupRetirements(f.env,{now:NOW});
  assert.equal(result.deferred_reason,"derived_history_busy");
  const page=await retirementPage(f.env);assert.equal(page.records.length,1);
  assert.equal(page.records[0].token_address,TOKEN);assert.equal(page.records[0].cohort,undefined);
});
