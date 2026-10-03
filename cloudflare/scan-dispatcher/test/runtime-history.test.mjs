import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import {createHash} from "node:crypto";
import test from "node:test";
import { historyEventId, ingestHistoryBatch, historyWallets, historyWalletDetail,
  historyEpisodeDetail, historyClusters, normalizedWallet, normalizedOutcome } from "../src/history.js";
import {existingPriorScores,historyOverview,hash,flushHistoryOutbox} from "../src/history.js";
import worker from "../src/index.js";
import {
  HistoryQueue, HISTORY_QUEUE_LIMITS, durableHistoryStatus,
  enqueueDurableHistory, flushDurableHistory, validateHistoryEvents,
} from "../src/runtime-history.js";

const NOW = Date.parse("2026-10-03T12:00:00Z");
const iso = time => new Date(time).toISOString();
const ledger = events => ({ history_ledger: { events } });

function event(id, { episode = id, at = NOW - 3600_000, type = "snapshot", wallets = [] } = {}) {
  return {
    event_id: id,
    episode: { episode_id: episode, token_address: `token:${episode}`, caught_at: iso(NOW - 4 * 3600_000),
      caught_mcap_usd: 80000, caught_liquidity_usd: 20000, token_age_days: 40 },
    event: { event_type: type, observed_at: iso(at), mcap_usd: 100000 },
    wallets,
    full_evidence: { source: "original", unrecognized_fields: [1, { retained: true }] },
  };
}

function outcome(id, options = {}) {
  const row = event(id, { ...options, type: "outcome_1h" });
  row.outcome = { entry_evidence_version:2,caught_at:row.episode.caught_at,caught_mcap_usd:80000,horizons: { "1h": {
    at: iso(NOW - 3 * 3600_000), target_at: iso(NOW - 3 * 3600_000),
    return_pct: 100, max_return_pct: 100, liquidity_usd: 20000, quality_status: "complete",
  } } };
  return row;
}

function storage(t) {
  const db = new DatabaseSync(":memory:");
  t.after(() => db.close());
  const result = { db, before: null, writes: 0 };
  result.sql = { exec(sql, ...values) {
    result.before?.(sql, values);
    const statement = db.prepare(sql);
    if (statement.columns().length) {
      const rows = statement.all(...values);
      return { toArray: () => rows };
    }
    result.writes += 1;
    const changes = Number(statement.run(...values).changes);
    // Model DO SQLite's index-inclusive rowsWritten, not SQLite changes(),
    // which reports only base rows. Due-index replacements cost two rows.
    let multiplier=1;
    if (/INSERT INTO history_queue_events|DELETE FROM history_queue_events/.test(sql)) multiplier=4;
    else if (/UPDATE history_queue_events SET status='delivered'/.test(sql)) multiplier=7;
    else if (/UPDATE history_queue_events SET lease_token/.test(sql)) multiplier=3;
    return { toArray: () => [], rowsWritten: changes*multiplier };
  } };
  result.transactionSync = callback => {
    db.exec("BEGIN");
    try {
      const value = callback();
      db.exec("COMMIT");
      return value;
    } catch (error) { db.exec("ROLLBACK"); throw error; }
  };
  return result;
}

// Execute the production history SQL, including numbered/reused D1 bindings.
function historyDb(t) {
  const db = new DatabaseSync(":memory:");
  t.after(() => db.close());
  for (const filename of ["0001_wallet_edge_history.sql", "0002_cluster_edge_evidence.sql", "0003_resumable_history.sql"]) {
    db.exec(readFileSync(new URL(`../migrations-history/${filename}`, import.meta.url), "utf8"));
  }
  const api = { db, before: null, calls: [] };
  async function execute(sql, values, method) {
    api.calls.push({ sql, values, method });
    assert.ok(values.length <= 100, `D1 bind limit: ${values.length}`);
    const override = await api.before?.({ sql, values, method });
    if (override !== undefined) return override;
    const statement = db.prepare(sql);
    const args = /\?\d+/.test(sql)
      ? [Object.fromEntries(values.map((value, index) => [index + 1, value]))] : values;
    if (method === "run") {
      const result = statement.run(...args);
      return { success: true, meta: { rows_written: Number(result.changes) } };
    }
    const rows = statement.all(...args);
    return method === "first" ? rows[0] ?? null : { success: true, results: rows };
  }
  api.prepare = sql => {
    const prepared = values => ({
      bind: (...bound) => prepared(bound),
      run: () => execute(sql, values, "run"),
      all: () => execute(sql, values, "all"),
      first: () => execute(sql, values, "first"),
    });
    return prepared([]);
  };
  api.batch = async statements => {
    db.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      db.exec("COMMIT");
      return results;
    } catch (error) { db.exec("ROLLBACK"); throw error; }
  };
  return api;
}

function fixture(t, overrides = {}) {
  let clock = NOW;
  const originalNow = Date.now;
  Date.now = () => clock;
  t.after(() => { Date.now = originalNow; });
  const store = storage(t);
  const db = historyDb(t);
  const env = { RADAR_HISTORY_DB: db, ...overrides };
  Object.defineProperty(env, "RADAR_DB", { get() { throw new Error("operational database accessed"); } });
  let queue = new HistoryQueue({ storage: store }, env);
  const requests = [];
  env.HISTORY_QUEUE = {
    idFromName(name) { assert.equal(name, "history-events-v1"); return name; },
    get() { return { fetch(request) { requests.push(request); return queue.fetch(request); } }; },
  };
  return { env, db, store, requests, get queue() { return queue; },
    advance(ms) { clock += ms; },
    restart() { queue = new HistoryQueue({ storage: store }, env); },
    rows() { return store.db.prepare("SELECT * FROM history_queue_events ORDER BY source_at,event_id").all(); },
    meta() { return store.db.prepare("SELECT * FROM history_queue_meta").get(); },
  };
}

function evidenceBucket() {
  const objects = new Map(), calls = [];
  const store = {objects,calls,before:null};
  const metadata = row => ({key:row.key,size:row.bytes.byteLength,customMetadata:structuredClone(row.metadata),
    checksums:{sha256:Uint8Array.from(Buffer.from(row.checksum,"hex")).buffer}});
  store.head = async key => {calls.push({method:"head",key}); await store.before?.("head",key);
    const row=objects.get(key);return row?metadata(row):null;};
  store.put = async (key,bytes,options) => {
    calls.push({method:"put",key});await store.before?.("put",key);
    assert.equal(options.onlyIf.get("If-None-Match"),"*");
    if(objects.has(key))return null;
    const checksum=createHash("sha256").update(bytes).digest("hex");
    assert.equal(checksum,options.sha256);
    const row={key,bytes:Uint8Array.from(bytes),metadata:structuredClone(options.customMetadata),checksum};
    objects.set(key,row);return metadata(row);
  };
  store.get = async key => {calls.push({method:"get",key});await store.before?.("get",key);
    const row=objects.get(key);return row?{...metadata(row),body:new Blob([row.bytes]).stream()}:null;};
  return store;
}

test("enqueue retains the entire event and never accesses RADAR_DB", async t => {
  const f = fixture(t);
  const original = event("full");
  original.large_observation = "x".repeat(60000);
  const result = await enqueueDurableHistory(f.env, ledger([original]));
  assert.equal(result.queued, 1);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json), original);
  assert.equal(result.pending_bytes, Buffer.byteLength(JSON.stringify(original)));
  assert.equal(f.db.calls.length, 0);
});

test("authenticated history envelopes are streamed and validated inside the durable queue", async t => {
  const f = fixture(t, {RADAR_INGEST_SECRET:"secret"});
  const original = event("streamed");
  original.large_observation = "x".repeat(60000);
  const request = new Request("https://worker/api/runtime/history", {method:"POST",
    headers:{"x-radar-ingest-secret":"secret"}, body:JSON.stringify(ledger([original]))});
  request.json = () => {throw new Error("edge must not parse history envelopes");};
  const response = await worker.fetch(request, f.env, {});
  assert.equal(response.status,200);
  assert.equal((await response.json()).queued,1);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json),original);
  const invalid = await worker.fetch(new Request("https://worker/api/runtime/history", {method:"POST",
    headers:{"x-radar-ingest-secret":"secret"}, body:JSON.stringify({history_ledger:[]})}), f.env, {});
  assert.equal(invalid.status,400);
  assert.equal((await invalid.json()).error,"history_ledger_invalid");
});

test("stable fallback historyEventId and duplicate first-payload semantics", async t => {
  const f = fixture(t);
  const row = event("unused");
  delete row.event_id;
  const changed = structuredClone(row);
  changed.full_evidence.source = "must not overwrite";
  const result = await enqueueDurableHistory(f.env, ledger([row, changed]));
  assert.equal(result.queued, 1);
  assert.equal(result.duplicates, 1);
  assert.equal(f.rows()[0].event_id, historyEventId(row));
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json), row);
});

test("25-event maximum rejects a whole overfull batch", async t => {
  const f = fixture(t);
  const rows = Array.from({ length: 26 }, (_, i) => event(`event-${i}`));
  await assert.rejects(enqueueDurableHistory(f.env, ledger(rows)), /history_batch_max_25/);
  assert.equal(f.rows().length, 0);
  assert.equal((await enqueueDurableHistory(f.env, ledger(rows.slice(0, 25)))).queued, 25);
});

test("per-event UTF-8 limit accepts the boundary and rejects without truncation", async t => {
  const row = event("utf8");
  row.unicode = "\u00e9".repeat(100);
  const bytes = Buffer.byteLength(JSON.stringify(row));
  const f = fixture(t, { HISTORY_QUEUE_MAX_EVENT_BYTES: bytes });
  await enqueueDurableHistory(f.env, ledger([row]));
  const larger = { ...row, event_id: "utf9", unicode: row.unicode + "x" };
  await assert.rejects(enqueueDurableHistory(f.env, ledger([event("small"), larger])), /history_event_oversize/);
  assert.equal(f.rows().length, 1);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json), row);
});

test("request size is bounded in both helper and streaming DO receiver", async t => {
  const f = fixture(t);
  const rows = Array.from({ length: 12 }, (_, i) => ({ ...event(`big-${i}`), evidence: "x".repeat(95000) }));
  await assert.rejects(enqueueDurableHistory(f.env, ledger(rows)), /history_request_oversize/);
  const response = await f.queue.fetch(new Request("https://queue/enqueue", {
    method: "POST", body: JSON.stringify({ events: rows }),
  }));
  assert.equal(response.status, 413);
  assert.equal(f.rows().length, 0);
});

test("row and byte capacities reject atomically, including duplicate/new mixes", async t => {
  const row = event("one");
  const f = fixture(t, { HISTORY_QUEUE_MAX_PENDING_ROWS: 1,
    HISTORY_QUEUE_MAX_PENDING_BYTES: Buffer.byteLength(JSON.stringify(row)) });
  await enqueueDurableHistory(f.env, ledger([row]));
  await assert.rejects(enqueueDurableHistory(f.env, ledger([row, event("two")])), /history_queue_pending_capacity/);
  assert.equal(f.rows().length, 1);
  assert.equal(f.meta().pending_rows, 1);
  assert.equal((await enqueueDurableHistory(f.env, ledger([row]))).duplicates, 1);
});

test("pending byte budget independently bounds storage", async t => {
  const row = event("one");
  const f = fixture(t, { HISTORY_QUEUE_MAX_PENDING_BYTES: Buffer.byteLength(JSON.stringify(row)) - 1 });
  await assert.rejects(enqueueDurableHistory(f.env, ledger([row])), /history_queue_pending_capacity/);
  assert.equal(f.meta().pending_bytes, 0);
});

test("SQLite transactionSync rolls back rows and counters on a mid-batch failure", async t => {
  const f = fixture(t);
  f.store.before = (sql, values) => {
    if (/INSERT INTO history_queue_events/.test(sql) && values[0] === "second") throw new Error("disk write failed");
  };
  await assert.rejects(enqueueDurableHistory(f.env, ledger([event("first"), event("second")])), /disk write failed/);
  assert.equal(f.rows().length, 0);
  assert.equal(f.meta().pending_rows, 0);
  assert.equal(f.meta().do_writes, 0);
});

test("invalid source identity/time or non-JSON values reject before persistence", async t => {
  const f = fixture(t);
  const bad = [
    { ...event("bad"), episode: { ...event("bad").episode, episode_id: "" } },
    { ...event("bad"), event: { event_type: "signal", observed_at: "2026-10-03T10:00:00" } },
    event("early", { at: NOW - 5 * 3600_000 }),
    event("future", { at: NOW + 300001 }),
    { ...event("bad"), wallets: {} },
    { ...event("bad"), omitted: undefined },
    { ...event("bad"), infinite: Infinity },
  ];
  for (const row of bad) await assert.rejects(enqueueDurableHistory(f.env, ledger([event("valid"), row])));
  const cycle = event("cycle");
  cycle.self = cycle;
  await assert.rejects(enqueueDurableHistory(f.env, ledger([cycle])), /history_event_not_json/);
  assert.equal(f.rows().length, 0);
});

test("malformed ledgers fail closed rather than silently omitting events", async t => {
  const f = fixture(t);
  for (const payload of [{ history_ledger: null }, { history_ledger: [] }, ledger(null), ledger({})]) {
    await assert.rejects(enqueueDurableHistory(f.env, payload));
  }
  assert.equal((await enqueueDurableHistory(f.env, {})).queued, 0);
});

test("D1 outage preserves full events through restart and retries", async t => {
  const f = fixture(t);
  const rows = [event("older", { at: NOW - 2 * 3600_000 }), event("newer")];
  await enqueueDurableHistory(f.env, ledger(rows));
  f.db.before = () => { throw new Error("D1 unavailable"); };
  const failed = await flushDurableHistory(f.env);
  assert.equal(failed.delivered, 0);
  assert.equal(failed.failed, 2);
  assert.equal(failed.pending, 2);
  assert.deepEqual(f.rows().map(row => JSON.parse(row.payload_json)), rows);
  f.restart();
  assert.equal((await flushDurableHistory(f.env)).delivered, 0);
  f.db.before = null;
  f.advance(120001);
  assert.equal((await flushDurableHistory(f.env)).delivered, 2);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 2);
  assert.ok(f.rows().every(row => row.status === "delivered" && row.payload_json === null));
});

test("source-time ordering overrides IDs and enqueue order", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([
    event("a-new", { at: NOW - 1000 }), event("z-old", { at: NOW - 3000 }), event("m-middle", { at: NOW - 2000 }),
  ]));
  assert.equal((await flushDurableHistory(f.env)).delivered, 3);
  const ids = f.db.calls.filter(call => /INSERT OR IGNORE INTO signal_episode_events/.test(call.sql)).map(call => call.values[0]);
  assert.deepEqual(ids, ["z-old", "m-middle", "a-new"]);
  assert.equal(f.rows().find(row => row.event_id === "a-new").status, "delivered");
});

test("poison backoff does not block newer episodes but preserves same-episode order", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([
    event("poison", { episode: "blocked", at: NOW - 3000 }),
    event("child", { episode: "blocked", at: NOW - 2000 }), event("new", { at: NOW - 1000 }),
  ]));
  f.db.before = ({ sql, values }) => {
    if (/INSERT INTO signal_episodes/.test(sql) && values[0] === "blocked") throw new Error("poison");
  };
  const first = await flushDurableHistory(f.env);
  assert.equal(first.failed, 1);
  assert.equal(first.delivered, 1);
  await enqueueDurableHistory(f.env, ledger([event("fresh", { at: NOW - 500 })]));
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.rows().find(row => row.event_id === "child").attempts, 0);
  const retry = f.rows().find(row => row.event_id === "poison").next_attempt_at;
  await enqueueDurableHistory(f.env, ledger([event("poison", { episode: "blocked" })]));
  assert.equal(f.rows().find(row => row.event_id === "poison").next_attempt_at, retry);
  f.advance(120001);
  f.db.before = null;
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
});

test("derived read failure keeps its event pending without blocking an independent completed event", async t => {
  const f = fixture(t);
  const rows = [outcome("result"), event("companion")];
  await enqueueDurableHistory(f.env, ledger(rows));
  f.db.before = ({ sql }) => { if (/GROUP BY e.mcap_band/.test(sql)) throw new Error("baseline refresh failed"); };
  const failed = await flushDurableHistory(f.env);
  assert.equal(failed.delivered, 1);
  assert.equal(failed.failed, 1);
  assert.match(failed.error, /baseline refresh failed/);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 2);
  assert.ok(f.rows().find(row => row.event_id === "result").payload_json);
  f.db.before = null;
  f.advance(120001);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 2);
});

test("derived write failure prevents acknowledgement of the affected event", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([outcome("result"), event("other")]));
  f.db.before = ({ sql }) => {
    if (/INSERT INTO market_baselines/.test(sql)) return { success: false, error: "derived write rejected" };
  };
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered, 1);
  assert.equal(result.pending, 1);
  assert.match(result.error, /derived write rejected/);
});

test("wallet/cluster derived failure replays evidence without inflating strength", async t => {
  const f = fixture(t);
  const wallets = ["alice", "bob"].map(wallet_address => ({ wallet_address, common_funder: "shared", bought_tokens: 100 }));
  await enqueueDurableHistory(f.env, ledger([event("signal", { type: "signal", wallets })]));
  f.db.before = ({ sql }) => { if (/INSERT INTO wallet_clusters/.test(sql)) throw new Error("cluster refresh failed"); };
  assert.equal((await flushDurableHistory(f.env)).delivered, 0);
  assert.equal(f.db.db.prepare("SELECT evidence_count FROM wallet_cluster_edges").get().evidence_count, 1);
  f.advance(120001);
  f.db.before = null;
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.db.db.prepare("SELECT evidence_count FROM wallet_cluster_edges").get().evidence_count, 1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM wallet_cluster_edge_evidence").get().n, 1);
});

test("reusable ingestion refreshes baselines once per batch and rejects incomplete D1 responses", async t => {
  const f = fixture(t);
  const result = await ingestHistoryBatch(f.env, [outcome("a"), outcome("b")], { now: iso(NOW) });
  assert.equal(result.ingested.length, 2);
  assert.equal(f.db.calls.filter(call => /GROUP BY e.mcap_band/.test(call.sql)).length, 1);
  const batch = f.db.batch;
  f.db.batch = async () => [];
  const rejected = await ingestHistoryBatch(f.env, [event("signal", { type: "signal" })], { now: iso(NOW) });
  assert.equal(rejected.ingested.length, 0);
  assert.match(rejected.failed[0].error, /history_d1_batch_incomplete/);
  f.db.batch = batch;
});

test("concurrent flushes share one serialized inFlight delivery", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([event("once")]));
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  let entered;
  const started = new Promise(resolve => { entered = resolve; });
  f.db.before = async ({ sql }) => {
    if (/INSERT INTO signal_episodes/.test(sql)) { entered(); await wait; }
  };
  const first = flushDurableHistory(f.env);
  await started;
  const second = flushDurableHistory(f.env);
  release();
  assert.deepEqual(await first, await second);
  assert.equal(f.db.calls.filter(call => /INSERT INTO signal_episodes/.test(call.sql)).length, 1);
});

test("restart recovers expired durable leases without premature retries", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([event("leased")]));
  f.queue.claim(NOW);
  f.restart();
  f.advance(HISTORY_QUEUE_LIMITS.leaseMs - 1);
  assert.equal((await flushDurableHistory(f.env)).delivered, 0);
  f.advance(2);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.rows()[0].attempts, 2);
  assert.ok(f.meta().hist_writes >= HISTORY_QUEUE_LIMITS.flushHistoryWriteUnits);
});

test("an acknowledgement storage failure keeps payload replayable after lease expiry", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([event("ack-fail")]));
  f.store.before = sql => { if (/SET status='delivered'/.test(sql)) throw new Error("ack storage failed"); };
  await assert.rejects(flushDurableHistory(f.env), /ack storage failed/);
  assert.equal(f.rows()[0].status, "pending");
  assert.ok(f.rows()[0].payload_json);
  f.store.before = null;
  f.restart();
  f.advance(HISTORY_QUEUE_LIMITS.leaseMs + 1);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 1);
});

test("delivered dedupe persists on restart and never evicts unexpired receipts", async t => {
  const f = fixture(t, { HISTORY_QUEUE_MAX_RECEIPT_ROWS: 1 });
  await enqueueDurableHistory(f.env, ledger([event("receipt")]));
  await flushDurableHistory(f.env);
  f.restart();
  const writes = f.store.writes;
  assert.equal((await enqueueDurableHistory(f.env, ledger([event("receipt")]))).duplicates, 1);
  assert.equal(f.store.writes, writes);
  await assert.rejects(enqueueDurableHistory(f.env, ledger([event("new")])), /history_queue_receipt_capacity/);
  f.advance(HISTORY_QUEUE_LIMITS.receiptRetentionMs + 1);
  assert.equal((await enqueueDurableHistory(f.env, ledger([event("new")]))).queued, 1);
  assert.equal(f.rows().length, 1);
  assert.equal(f.rows()[0].event_id, "new");
});

test("daily DO write cap rolls back enqueue without truncation", async t => {
  const f = fixture(t, { HISTORY_QUEUE_DAILY_WRITE_UNITS: 1 });
  await assert.rejects(enqueueDurableHistory(f.env, ledger([event("budget")])), /history_queue_daily_write_budget/);
  assert.equal(f.rows().length, 0);
  assert.equal(f.meta().do_writes, 0);
});

test("daily history writes stop at the reserved budget and reset at UTC midnight", async t => {
  const f = fixture(t, { HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS: 16 });
  await enqueueDurableHistory(f.env, ledger([event("first"), event("second")]));
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered, 1);
  assert.equal(result.failed, 0);
  assert.equal(result.continued, 1);
  assert.equal(f.meta().hist_writes, 16);
  f.advance(120001);
  await assert.rejects(flushDurableHistory(f.env), /history_daily_write_budget/);
  f.advance(86400_000);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.meta().hist_writes, 16);
});

test("bounded write budget saves the next phase without poison backoff or payload loss", async t => {
  const f = fixture(t, { HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS: 16 });
  const row = event("signal", { type: "signal" });
  await enqueueDurableHistory(f.env, ledger([row]));
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered, 0);
  assert.equal(result.history_write_units, 16);
  assert.equal(f.rows()[0].last_error, null);
  assert.equal(result.continued,1);
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"outcomes");
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_outcomes").get().n, 0);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json), row);
});

test("status helper is read-only, uses GET, and reports effective lower limits", async t => {
  const f = fixture(t, { HISTORY_QUEUE_MAX_PENDING_ROWS: 10 });
  await enqueueDurableHistory(f.env, ledger([event("status")]));
  const writes = f.store.writes;
  const meta = { ...f.meta() };
  const result = await durableHistoryStatus(f.env);
  assert.equal(f.requests.at(-1).method, "GET");
  assert.equal(result.pending, 1);
  assert.equal(result.oldest_pending_at, iso(NOW - 3600_000));
  assert.equal(result.limits.pendingRows, 10);
  assert.equal(f.store.writes, writes);
  assert.deepEqual({ ...f.meta() }, meta);
  assert.equal(f.db.calls.length, 0);
  assert.deepEqual(await durableHistoryStatus({}), { enabled: false, error: "history_queue_not_configured" });
});

test("missing history DB and missing bindings fail closed while preserving backlog", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([event("pending")]));
  delete f.env.RADAR_HISTORY_DB;
  await assert.rejects(flushDurableHistory(f.env), /history_db_not_configured/);
  assert.equal(f.rows()[0].attempts, 0);
  await assert.rejects(enqueueDurableHistory({}, ledger([event("pending")])), /history_queue_not_configured/);
});

test("configuration can lower limits but cannot opt into larger paid-plan budgets", t => {
  const store = storage(t);
  assert.throws(() => new HistoryQueue({ storage: store }, { HISTORY_QUEUE_DAILY_WRITE_UNITS: 100001 }), /invalid_/);
  assert.throws(() => new HistoryQueue({ storage: store }, { HISTORY_QUEUE_MAX_PENDING_ROWS: 0 }), /invalid_/);
});

test("wire receiver rejects malformed JSON, unsupported methods, and invalid UTF-8", async t => {
  const f = fixture(t);
  for (const body of ["{", Uint8Array.of(255)]) {
    const response = await f.queue.fetch(new Request("https://queue/enqueue", { method: "POST", body }));
    assert.equal(response.status, 400);
  }
  assert.equal((await f.queue.fetch(new Request("https://queue/enqueue"))).status, 405);
  assert.equal((await f.queue.fetch(new Request("https://queue/missing", { method: "POST" }))).status, 404);
  assert.equal(f.rows().length, 0);
});

test("same source timestamp is deterministically ordered and pre-catch values are rejected", () => {
  const row = event("boundary", { at: NOW - 4 * 3600_000 });
  assert.equal(validateHistoryEvents([row], NOW).length, 1);
  row.event.observed_at = iso(NOW - 4 * 3600_000 - 1);
  assert.throws(() => validateHistoryEvents([row], NOW), /history_source_identity_or_time_invalid/);
});

async function drain(f, maximum = 300) {
  const totals={flushes:0,days:0,units:0};
  while (f.meta().pending_rows && totals.flushes<maximum) {
    try {
      const result=await flushDurableHistory(f.env);
      assert.equal(result.failed,0,result.error);
      assert.ok(result.history_queries<=HISTORY_QUEUE_LIMITS.flushQueries);
      assert.ok(result.history_requests<=HISTORY_QUEUE_LIMITS.flushRequests);
      assert.ok(result.history_write_units<=HISTORY_QUEUE_LIMITS.flushHistoryWriteUnits);
      totals.units+=result.history_write_units;
      totals.flushes++;
    } catch (error) {
      if (!/history_daily_write_budget/.test(error.message)) throw error;
      f.advance(86400_000); totals.days++;
    }
    f.advance(1001);
    f.restart();
  }
  assert.equal(f.meta().pending_rows,0,JSON.stringify(f.rows().map(row=>({id:row.event_id,progress:row.progress_json,error:row.last_error}))));
  return totals;
}

test("I01: accepted 60-wallet work progresses across restarts without replaying catch writes", async t => {
  const f=fixture(t);
  const wallets=Array.from({length:60},(_,i)=>({wallet_address:`wallet-${String(i).padStart(3,"0")}`,
    common_funder:"private-funder",bought_tokens:100,current_token_balance:100}));
  const raw=event("large",{type:"signal",wallets});
  await enqueueDurableHistory(f.env,ledger([raw]));
  const first=await flushDurableHistory(f.env);
  assert.equal(first.delivered,0);
  assert.equal(first.failed,0);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json),raw);
  const phase=JSON.parse(f.rows()[0].progress_json).phase;
  assert.equal(phase,"edges");
  const catchWrites=f.db.calls.filter(call=>/INSERT INTO signal_wallets/.test(call.sql)).length;
  const evidenceBefore=f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edge_evidence").get().n;
  f.advance(1001); f.restart();
  const totals=await drain(f);
  assert.ok(totals.flushes>1 && totals.flushes<60);
  assert.ok(evidenceBefore>0);
  assert.equal(f.db.calls.filter(call=>/INSERT INTO signal_wallets/.test(call.sql)).length,catchWrites);
  assert.equal(f.db.calls.filter(call=>/INSERT OR IGNORE INTO signal_episode_events/.test(call.sql)).length,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edge_evidence").get().n,1770);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges WHERE evidence_count<>1").get().n,0);
  assert.deepEqual((await historyClusters(f.env)).rows.map(row=>row.wallet_count),[60]);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM history_cluster_work").get().n,0);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  t.diagnostic(`60 wallets: ${totals.flushes+1} bounded flushes, ${totals.units+first.history_write_units} units, ${totals.days} UTC resets`);
});

test("I01: 25 lightweight events can finish in one bounded flush; retention is packed without graph rewrites", async t => {
  const f=fixture(t);
  await enqueueDurableHistory(f.env,ledger(Array.from({length:25},(_,i)=>event(`light-${i}`))));
  const result=await flushDurableHistory(f.env);
  assert.equal(result.delivered,20);
  assert.equal(result.continued,0);
  assert.equal(result.pending,5);
  assert.equal(result.history_queries,40);
  f.advance(1001); await drain(f);
  const wallets=Array.from({length:40},(_,i)=>({wallet_address:`held-${i}`,bought_tokens:100,current_token_balance:100,common_funder:"same"}));
  await enqueueDurableHistory(f.env,ledger([event("capture",{episode:"held",type:"signal",wallets})]));
  await drain(f);
  const before=f.db.calls.length;
  const checked=wallets.map(wallet=>({...wallet,current_token_balance:null,balance_retained_pct:null}));
  await enqueueDurableHistory(f.env,ledger([event("retention",{episode:"held",type:"retention_check",at:NOW, wallets:checked})]));
  const retained=await flushDurableHistory(f.env);
  assert.equal(retained.delivered,1);
  assert.equal(retained.history_write_units,24);
  assert.equal(f.db.calls.slice(before).filter(call=>/INSERT INTO signal_wallets|wallet_cluster_edge|wallet_clusters/.test(call.sql)).length,0);
  const detail=await historyEpisodeDetail(f.env,"held");
  const latest=detail.observations.filter(row=>row.observed_at===iso(NOW));
  assert.equal(latest.length,40);
  assert.ok(latest.every(row=>row.current_token_balance===null && row.balance_retained_pct===null));
  const wallet=await historyWalletDetail(f.env,"held-0");
  assert.equal(wallet.observations[0].current_token_balance,null);
});

test("I01/I07/I08: a 100-member component completes within unchanged quotas and D1's 100 binds", async t => {
  const f=fixture(t);
  const wallets=Array.from({length:100},(_,i)=>({wallet_address:`w${i}`,common_funder:"f"}));
  await existingPriorScores(f.db,wallets,iso(NOW));
  assert.deepEqual(f.db.calls.slice(-2).map(call=>call.values.length),[100,2]);
  await enqueueDurableHistory(f.env,ledger([event("hundred",{type:"signal",wallets})]));
  const totals=await drain(f);
  assert.equal((await historyClusters(f.env)).rows[0].wallet_count,100);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges").get().n,4950);
  t.diagnostic(`100 wallets: ${totals.flushes} flushes, ${totals.units} units, ${totals.days} UTC resets`);
});

test("I07: AB, BC, CD become one component; old identities are retired, not deleted", async t => {
  const f=fixture(t);
  for (const [id,a,b] of [["ab","a","b"],["bc","b","c"],["cd","c","d"]]) {
    await enqueueDurableHistory(f.env,ledger([event(id,{type:"signal",wallets:[a,b].map(wallet_address=>({wallet_address,common_funder:id}))})]));
    await drain(f);
  }
  assert.deepEqual((await historyClusters(f.env)).rows.map(row=>row.wallet_count),[4]);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_clusters WHERE active=0 AND retired_at IS NOT NULL").get().n,2);
  const members=f.db.db.prepare(`SELECT m.wallet_address FROM wallet_cluster_members m JOIN wallet_clusters c USING(cluster_id)
    WHERE c.active=1 ORDER BY m.wallet_address`).all();
  assert.deepEqual(members.map(row=>row.wallet_address),["a","b","c","d"]);
});

test("I07: known public infrastructure never creates edges or active ownership components", async t => {
  const f=fixture(t,{HISTORY_INFRASTRUCTURE_ADDRESSES:JSON.stringify(["public-router"])});
  const wallets=["a","b"].map(wallet_address=>({wallet_address,common_funder:"public-router",
    common_executor:"So11111111111111111111111111111111111111112"}));
  await enqueueDurableHistory(f.env,ledger([event("infra",{type:"signal",wallets})]));
  await drain(f);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges").get().n,0);
  assert.deepEqual((await historyClusters(f.env)).rows,[]);
});

test("I07: fragment retirement and scratch cleanup remain row-bounded at the minimum edge budget", async t => {
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:24});
  const insert=f.db.db.prepare(`INSERT INTO wallet_clusters
    (cluster_id,wallet_count,computed_through,updated_at,numeric_contract_version) VALUES (?,2,?,?,2)`);
  for (let i=0;i<30;i++) {
    const cluster=`obsolete-${i}`;
    insert.run(cluster,iso(NOW),iso(NOW));
    for (const wallet of ["a","b"]) f.db.db.prepare(`INSERT INTO wallet_cluster_members
      (cluster_id,wallet_address,first_seen_at,last_seen_at) VALUES (?,?,?,?)`).run(cluster,wallet,iso(NOW),iso(NOW));
  }
  await enqueueDurableHistory(f.env,ledger([event("retire-many",{type:"signal",
    wallets:["a","b"].map(wallet_address=>({wallet_address,common_funder:"private"}))})]));
  await drain(f,200);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_clusters WHERE active=1").get().n,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_clusters WHERE retired_at IS NOT NULL").get().n,30);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM history_cluster_work").get().n,0);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  const retireWrites=f.db.calls.filter(call=>/SET active=0,retired_at=/.test(call.sql));
  assert.equal(retireWrites.length,30);
  assert.ok(retireWrites.every(call=>/WHERE cluster_id=\?1 AND active=1/.test(call.sql)));
  const deletes=f.db.calls.filter(call=>/DELETE FROM history_cluster_work/.test(call.sql));
  assert.ok(deletes.length>=2);
  assert.ok(deletes.every(call=>/wallet_address=\?2/.test(call.sql)));
});

test("I05: unknown balances, retention, threshold times and drawdown remain null; real zeros survive", () => {
  const wallet=normalizedWallet({wallet_address:"unknown",bought_tokens:100,current_token_balance:null,
    balance_retained_pct:null,retained_pct_at_catch:null});
  assert.equal(wallet.observation.current_token_balance,null);
  assert.equal(wallet.observation.balance_retained_pct,null);
  assert.equal(wallet.observation.behavior_status,"unknown");
  assert.equal(wallet.retained_pct_at_catch,null);
  const exited=normalizedWallet({wallet_address:"exited",bought_tokens:100,current_token_balance:0,balance_retained_pct:0});
  assert.equal(exited.observation.current_token_balance,0);
  assert.equal(exited.observation.balance_retained_pct,0);
  const rows=normalizedOutcome({horizons:{"1h":{at:iso(NOW),max_return_pct:100,time_to_2x_minutes:null,
    time_to_5x_minutes:null,max_drawdown_pct:null}}},{caught_at:iso(NOW-3600_000)},iso(NOW));
  assert.equal(rows[0].time_to_2x_minutes,null);
  assert.equal(rows[0].time_to_5x_minutes,null);
  assert.equal(rows[0].max_drawdown_pct,null);
});

test("I05: legacy numeric results remain preserved but quarantined from trusted learning", async t => {
  const f=fixture(t);
  await enqueueDurableHistory(f.env,ledger([outcome("legacy")])); await drain(f);
  f.db.db.exec(`DELETE FROM signal_outcomes WHERE horizon_minutes=4320;
    UPDATE signal_outcomes SET horizon_minutes=4320,status='complete',max_return_pct=100,
    tradable_2x=1,numeric_contract_version=1,entry_verified=0 WHERE horizon_minutes=60;
    DELETE FROM signal_outcomes WHERE horizon_minutes<>4320;`);
  const overview=await historyOverview(f.env,"all");
  assert.equal(overview.resolved_72h,0);
  assert.equal(overview.legacy_unverified_72h,1);
  const detail=await historyEpisodeDetail(f.env,"legacy");
  assert.equal(detail.outcomes[0].max_return_pct,100);
  assert.equal(detail.outcomes[0].trusted,false);
  const projected=await historyWallets(f.env);
  assert.deepEqual(projected.rows,[]);
});

test("forward cluster repair retires legacy infrastructure fragments without deleting evidence", async t => {
  const f=fixture(t,{HISTORY_INFRASTRUCTURE_ADDRESSES:["router"]});
  const raw=event("old",{type:"signal",wallets:["a","b"].map(wallet_address=>({wallet_address}))});
  await enqueueDurableHistory(f.env,ledger([raw])); await drain(f);
  f.db.db.prepare(`INSERT INTO wallet_cluster_edges (edge_id,wallet_a,wallet_b,relation_type,first_seen_at,last_seen_at,
    weight,evidence_json,created_at,updated_at) VALUES ('legacy-edge','a','b','common_funder',?,?,1,?,?,?)`)
    .run(iso(NOW),iso(NOW),JSON.stringify({value:"router"}),iso(NOW),iso(NOW));
  f.db.db.prepare(`INSERT INTO wallet_clusters (cluster_id,computed_through,updated_at,wallet_count)
    VALUES ('legacy-cluster',?,?,2)`).run(iso(NOW),iso(NOW));
  for (const wallet of ["a","b"]) f.db.db.prepare(`INSERT INTO wallet_cluster_members
    (cluster_id,wallet_address,first_seen_at,last_seen_at) VALUES ('legacy-cluster',?,?,?)`).run(wallet,iso(NOW),iso(NOW));
  await enqueueDurableHistory(f.env,ledger([event("repair",{episode:"old",type:"cluster_repair",at:NOW})]));
  await drain(f);
  assert.equal(f.db.db.prepare("SELECT active FROM wallet_clusters WHERE cluster_id='legacy-cluster'").get().active,0);
  assert.equal(f.db.db.prepare("SELECT is_infrastructure FROM wallet_cluster_edges WHERE edge_id='legacy-edge'").get().is_infrastructure,1);
  assert.deepEqual((await historyClusters(f.env)).rows,[]);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
});

test("I06: full-tuple pagination returns every tied and nullable-lift wallet exactly once", async t => {
  const f=fixture(t);
  const rows=[["z",3,2],["a",2,3],["m",2,1],["n",null,4],["b",null,1]];
  for (const [address,lift,sample] of rows) f.db.db.prepare(`INSERT INTO wallet_scores
    (wallet_address,edge_score,lift_2x,eligible_episodes,computed_through,updated_at,numeric_contract_version)
    VALUES (?,50,?,?,?, ?,2)`).run(address,lift,sample,iso(NOW),iso(NOW));
  const found=[]; let cursor;
  do {
    const result=await historyWallets(f.env,{limit:1,cursor});
    found.push(...result.rows.map(row=>row.wallet_address)); cursor=result.next_cursor;
  } while (cursor);
  assert.deepEqual(found,["z","a","m","n","b"]);
  await assert.rejects(historyWallets(f.env,{cursor:btoa(JSON.stringify({score:50,wallet:"z"}))}),/obsolete/);
});

test("I02: daily-cap rejection is persisted in queue health without losing payload or progress", async t => {
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:16});
  await enqueueDurableHistory(f.env,ledger([event("one"),event("two")]));
  await flushDurableHistory(f.env); f.advance(1001);
  await assert.rejects(flushDurableHistory(f.env),/history_daily_write_budget/);
  const health=await durableHistoryStatus(f.env);
  assert.equal(health.history_budget_exhausted,true);
  assert.match(health.last_flush_error,/history_daily_write_budget/);
  assert.ok(health.oldest_pending_age_seconds>0);
  assert.ok(f.rows().find(row=>row.status==="pending").payload_json);
});

test("I01/I07: a crash before DO progress commit recovers the same graph lock after lease expiry", async t => {
  const f=fixture(t);
  const wallets=Array.from({length:60},(_,i)=>({wallet_address:`w${i}`,common_funder:"f"}));
  await enqueueDurableHistory(f.env,ledger([event("crashed",{type:"signal",wallets})]));
  f.store.before=sql=>{
    if (/UPDATE history_queue_events SET progress_json|SET lease_token=NULL/.test(sql)) throw new Error("simulated process loss");
  };
  await assert.rejects(flushDurableHistory(f.env),/simulated process loss/);
  const job=`history:${hash("crashed")}`;
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,job);
  assert.equal(f.rows()[0].progress_json,null);
  assert.ok(f.rows()[0].payload_json);
  f.store.before=null; f.restart(); f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  await drain(f);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges WHERE evidence_count<>1").get().n,0);
});

test("I07: a waiting job cannot clear a pending owner's graph lock", async t => {
  const f=fixture(t);
  const wallets=Array.from({length:60},(_,i)=>({wallet_address:`w${i}`,common_funder:"f"}));
  await enqueueDurableHistory(f.env,ledger([event("owner",{type:"signal",wallets})]));
  await flushDurableHistory(f.env);
  const job=f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id;
  f.store.db.prepare("UPDATE history_queue_events SET next_attempt_at=? WHERE event_id='owner'").run(NOW+1000000);
  await enqueueDurableHistory(f.env,ledger([event("waiting",{at:NOW,type:"signal",
    wallets:["x","y"].map(wallet_address=>({wallet_address,common_funder:"g"}))})]));
  const wait=await flushDurableHistory(f.env);
  assert.equal(wait.delivered,0);
  assert.equal(wait.failed,0);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,job);
  f.advance(1000001); await drain(f);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
});

test("I01: minimally admitted budgets progress, including quarantined outcomes; impossible work is rejected before enqueue", async t => {
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:16});
  const signal=event("tiny",{type:"signal",wallets:[{wallet_address:"single"}]});
  await enqueueDurableHistory(f.env,ledger([signal]));
  const totals=await drain(f,100);
  assert.ok(totals.days>0);
  const unverified=outcome("quarantined"); delete unverified.outcome.entry_evidence_version;
  await enqueueDurableHistory(f.env,ledger([unverified])); await drain(f,100);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("impossible",{type:"signal",
    wallets:["a","b"].map(wallet_address=>({wallet_address,common_funder:"f"}))})])),/atomic_daily_allowance/);
  assert.equal(f.rows().some(row=>row.event_id==="impossible"),false);
});

test("legacy outbox handoff preserves pending payload until actual durable delivery receipt", async t => {
  const f=fixture(t);
  const raw=event("legacy-handoff");
  let delivered=0;
  const db={prepare(sql){let values=[];return {bind(...v){values=v;return this;},async all(){return {results:delivered?[]:[{
    event_id:raw.event_id,payload_json:JSON.stringify(raw),attempts:0}]};},async run(){assert.match(sql,/status='delivered'/);
    assert.equal(values[0],raw.event_id);delivered++;return {success:true};}};},async batch(statements){return Promise.all(statements.map(row=>row.run()));}};
  const env={RADAR_DB:db,RADAR_HISTORY_DB:f.db,HISTORY_QUEUE:f.env.HISTORY_QUEUE};
  const accepted=await flushHistoryOutbox(env);
  assert.equal(accepted.delivered,0);
  assert.equal(delivered,0);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json),raw);
  await drain(f);
  assert.equal((await flushHistoryOutbox(env)).delivered,1);
  assert.equal(delivered,1);
});

test("I01 steady state: two days of 100 forty-wallet cohorts/hour, outcomes and receipt expiry fit unchanged daily budgets", async t => {
  const f=fixture(t,{SCHEDULER_ENABLED:"disabled"});
  const start=NOW-12*3600_000;
  const caught=iso(start-3*86400_000);
  f.advance(-12*3600_000);
  // Exercise the mature receipt store, not just the empty first UTC day.
  // These are metadata-only delivered receipts; no pending payload is evicted.
  const seededReceipts=HISTORY_QUEUE_LIMITS.receiptRows-HISTORY_QUEUE_LIMITS.pendingRows;
  const expiredAt=start-HISTORY_QUEUE_LIMITS.receiptRetentionMs-1;
  f.store.db.exec("BEGIN");
  const receiptInsert=f.store.db.prepare(`INSERT INTO history_queue_events
    (event_id,episode_id,source_at,payload_bytes,status,next_attempt_at,delivered_at)
    VALUES (?,? ,?,0,'delivered',?,?)`);
  for (let i=0;i<seededReceipts-1;i++) receiptInsert.run(`expired-${i}`,`expired-${i}`,expiredAt,expiredAt,expiredAt);
  receiptInsert.run("protected-receipt","protected-receipt",start-3600_000,start,start);
  f.store.db.prepare("UPDATE history_queue_meta SET delivered_rows=?").run(seededReceipts);
  f.store.db.exec("COMMIT");
  assert.ok(HISTORY_QUEUE_LIMITS.receiptRows>=31*5000+2*HISTORY_QUEUE_LIMITS.pendingRows);
  const episodeInsert=f.db.db.prepare(`INSERT INTO signal_episodes (episode_id,token_address,lane,signal_family,
    caught_at,last_signal_at,mcap_band,liquidity_band,age_band,data_quality_status,created_at,updated_at)
    VALUES (?,?,'reactivation','reactivation_wave',?,?,'50k_100k','15k_50k','30d_90d','complete',?,?)`);
  const walletInsert=f.db.db.prepare(`INSERT INTO signal_wallets (episode_id,wallet_address,cohort_role,bought_tokens,
    evidence_status,created_at,updated_at,numeric_contract_version) VALUES (?,?,'at_catch',100,'complete',?,?,2)`);
  const cohorts=Array.from({length:100},(_,i)=>Array.from({length:40},(_,j)=>({wallet_address:`cohort-${i}-wallet-${j}`,
    bought_tokens:100,current_token_balance:100,balance_retained_pct:100,common_funder:j<2?`funder-${i}`:null})));
  for (let i=0;i<100;i++) {
    episodeInsert.run(`cohort-${i}`,`token:cohort-${i}`,caught,caught,caught,caught);
    for (const wallet of cohorts[i]) walletInsert.run(`cohort-${i}`,wallet.wallet_address,caught,caught);
    f.db.db.prepare(`INSERT INTO wallet_clusters (cluster_id,wallet_count,computed_through,updated_at,numeric_contract_version)
      VALUES (?,2,?,?,2)`).run(`warm-${i}`,caught,caught);
    for (const wallet of cohorts[i].slice(0,2)) f.db.db.prepare(`INSERT INTO wallet_cluster_members
      (cluster_id,wallet_address,first_seen_at,last_seen_at) VALUES (?,?,?,?)`).run(`warm-${i}`,wallet.wallet_address,caught,caught);
  }
  const checked=(id,i,at)=>{
    const raw=event(id,{episode:`cohort-${i}`,type:"retention_check",at,wallets:cohorts[i]});
    raw.episode.caught_at=caught;
    return raw;
  };
  const enqueue=async rows=>{for(let i=0;i<rows.length;i+=25) await enqueueDurableHistory(f.env,ledger(rows.slice(i,i+25)));};
  await enqueue(Array.from({length:96},(_,i)=>checked(`backlog-${i}`,i,start-3600_000)));
  let peak=96;
  for (let minute=0;minute<2880;minute+=5) {
    if (minute) f.advance(300000);
    if (minute%60===0) {
      const hour=minute/60;
      await enqueue(cohorts.map((_,i)=>checked(`hour-${hour}-${i}`,i,start+minute*60000)));
      const result=event(`outcome-${hour}`,{episode:`cohort-${hour%100}`,type:"outcome_72h",at:start+minute*60000});
      result.episode.caught_at=caught;
      result.outcome={entry_evidence_version:2,caught_at:caught,caught_mcap_usd:80000,horizons:{"72h":{
        at:iso(start),target_at:iso(start),return_pct:100,max_return_pct:100,liquidity_usd:20000,quality_status:"complete"}}};
      await enqueue([result]);
      if (hour%6===0) {
        const signal=event(`new-${hour}`,{type:"signal",at:start+minute*60000,
          wallets:Array.from({length:40},(_,i)=>({wallet_address:`new-${hour}-wallet-${i}`,bought_tokens:100}))});
        signal.episode.caught_at=iso(start+minute*60000); await enqueue([signal]);
      }
      peak=Math.max(peak,f.meta().pending_rows);
      f.restart();
    }
    const tasks=[];
    await worker.scheduled({cron:"*/5 * * * *"},f.env,{waitUntil:promise=>tasks.push(promise)});
    await Promise.all(tasks);
    assert.ok(f.meta().hist_writes<=80000);
    assert.ok(f.meta().do_writes<=50000);
    if (minute%1440===1435) {
      const pending=f.store.db.prepare("SELECT event_id,last_error,progress_json FROM history_queue_events WHERE status='pending'").all();
      assert.deepEqual(pending,[]);
      assert.equal((await enqueueDurableHistory(f.env,ledger([event("protected-receipt")]))).duplicates,1);
      t.diagnostic(`UTC day ${Math.floor(minute/1440)+1}: delivered workload=${96+Math.floor((minute+5)/1440)*2428}, pending=0, peak=${peak}, history units=${f.meta().hist_writes}, DO units=${f.meta().do_writes}, receipts=${f.meta().delivered_rows}`);
    }
  }
  assert.equal(f.meta().pending_rows,0);
  assert.equal(f.meta().delivered_rows,HISTORY_QUEUE_LIMITS.receiptRows);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_observation_bundles").get().n,4896);
  assert.equal(f.store.db.prepare("SELECT COUNT(*) n FROM history_queue_events WHERE event_id LIKE 'expired-%'").get().n,
    HISTORY_QUEUE_LIMITS.receiptRows-4952-1);
  assert.ok(f.requests.filter(request=>new URL(request.url).pathname==="/flush").length<=1152);
});

test("R2 mode persists only tiny references after upload and confirmation; SQL sees verified original evidence", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  const raw=event("r2"),before=structuredClone(raw);
  raw.large_evidence="x".repeat(60000);before.large_evidence=raw.large_evidence;
  const result=await enqueueDurableHistory(f.env,ledger([raw]));
  assert.equal(result.queued,1);
  const queued=f.rows()[0],envelope=JSON.parse(queued.payload_json);
  assert.equal(envelope.archive_queue_version,1); assert.equal(queued.archive_version,1);
  assert.ok(queued.payload_bytes<1500);
  assert.equal(envelope.episode,undefined); assert.equal(envelope.wallets,undefined);
  assert.equal(envelope.archive_ref.event_id,"r2");
  assert.deepEqual(archive.calls.map(call=>call.method),["head","put","head"]);
  assert.equal(f.db.calls.length,0);
  assert.deepEqual(raw,before);
  const flushed=await flushDurableHistory(f.env);
  assert.equal(flushed.delivered,1);assert.equal(flushed.history_queries,2);
  const stored=f.db.db.prepare("SELECT payload_json FROM signal_episode_events WHERE event_id='r2'").get();
  const sql=JSON.parse(stored.payload_json);
  assert.equal(sql.archive_ref.key,envelope.archive_ref.key);
  const status=await durableHistoryStatus(f.env);
  assert.equal(status.archive_write_requests,1); assert.equal(status.archive_read_requests,3);
  assert.equal(status.pending_bytes,0);
});

test("R2 unavailable cannot acknowledge ingress; failed confirmation retries preserve caller identity", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  const raw=event("outbox");
  archive.before=method=>{if(method==="put")throw new Error("R2 unavailable");};
  const request=()=>new Request("https://queue/ingest",{method:"POST",body:JSON.stringify(ledger([raw]))});
  assert.equal((await f.queue.fetch(request())).status,503);
  assert.equal(f.rows().length,0); assert.equal(f.meta().pending_rows,0); assert.equal(f.db.calls.length,0);
  let heads=0;
  archive.before=method=>{if(method==="head"&&++heads===2)throw new Error("confirmation lost");};
  await assert.rejects(enqueueDurableHistory(f.env,ledger([raw])),/history_archive_unavailable/);
  assert.equal(f.rows().length,0); assert.equal(archive.objects.size,1);
  archive.before=null;
  assert.equal((await enqueueDurableHistory(f.env,ledger([raw]))).queued,1);
  const calls=archive.calls.length;
  archive.before=()=>{throw new Error("R2 now unavailable");};
  assert.equal((await enqueueDurableHistory(f.env,ledger([{...raw,full_evidence:{changed:true}}]))).duplicates,1);
  assert.equal(archive.calls.length,calls);
  assert.equal(JSON.parse(f.rows()[0].payload_json).archive_ref.event_id,raw.event_id);
});

test("corrupt or mismatched archive refs never execute SQL and remain pending through restart", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger([event("corrupt")]));
  const row=f.rows()[0],ref=JSON.parse(row.payload_json).archive_ref,object=archive.objects.get(ref.key);
  object.bytes[10]^=1;
  const failed=await flushDurableHistory(f.env);
  assert.equal(failed.failed,1); assert.equal(failed.delivered,0); assert.equal(failed.history_write_units,0);
  assert.equal(f.db.calls.length,0); assert.equal(f.rows()[0].status,"pending");
  assert.equal(f.rows()[0].payload_json,row.payload_json);
  f.restart();f.advance(120001);object.bytes[10]^=1;
  object.metadata.event_id="foreign";
  assert.equal((await flushDurableHistory(f.env)).failed,1);
  assert.equal(f.db.calls.length,0);
  object.metadata.event_id=ref.event_id;f.advance(240001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
});

test("R2 read outage after acceptance preserves reference, progress and backoff", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger([event("read-outage")]));
  const payload=f.rows()[0].payload_json;
  archive.before=method=>{if(method==="get")throw new Error("offline");};
  assert.equal((await flushDurableHistory(f.env)).failed,1);
  assert.equal(f.db.calls.length,0);assert.equal(f.rows()[0].payload_json,payload);
  const retry=f.rows()[0].next_attempt_at,progress=f.rows()[0].progress_json;
  f.restart();
  await enqueueDurableHistory(f.env,ledger([event("read-outage")]));
  assert.equal(f.rows()[0].next_attempt_at,retry);assert.equal(f.rows()[0].progress_json,progress);
  archive.before=null;f.advance(120001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
});

test("legacy spill is incremental and preserves pending identity, progress, timers and receipts", async t => {
  const archive=evidenceBucket(),f=fixture(t,{RADAR_ARCHIVE:archive});
  const rows=Array.from({length:26},(_,i)=>({...event(`legacy-${String(i).padStart(2,"0")}`),evidence:"x".repeat(30000)}));
  await enqueueDurableHistory(f.env,ledger(rows.slice(0,25)));await enqueueDurableHistory(f.env,ledger(rows.slice(25)));
  f.store.db.prepare("UPDATE history_queue_events SET next_attempt_at=?,attempts=3,progress_json=?").run(NOW+3600000,JSON.stringify({version:1,phase:"event"}));
  const before=f.rows(),pendingBytes=f.meta().pending_bytes;
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();
  const flush=await flushDurableHistory(f.env);
  assert.equal(flush.spilled,5);assert.equal(flush.delivered,0);assert.equal(flush.pending,26);
  assert.ok(f.meta().pending_bytes<pendingBytes);
  const after=f.rows();
  for(let i=0;i<26;i++){
    for(const key of ["event_id","episode_id","source_at","next_attempt_at","attempts","progress_json","status","delivered_at"])assert.equal(after[i][key],before[i][key]);
    assert.equal(after[i].archive_version,i<5?1:0);
  }
  assert.equal(f.db.calls.length,0);
  const second=await flushDurableHistory(f.env);
  assert.equal(second.spilled,5);assert.equal(f.rows().filter(row=>row.archive_version===0).length,16);
  for (let i=0;i<4;i++) await flushDurableHistory(f.env);
  assert.equal(f.rows().filter(row=>row.archive_version===0).length,0);
  assert.ok(f.meta().pending_bytes<pendingBytes/10);
  assert.equal(f.meta().pending_rows,26);
});

test("legacy R2 upload or spill commit failure leaves original raw event and counters intact", async t => {
  const archive=evidenceBucket(),f=fixture(t,{RADAR_ARCHIVE:archive});
  const raw={...event("legacy-safe"),evidence:"x".repeat(50000)};
  await enqueueDurableHistory(f.env,ledger([raw]));
  const before=f.rows()[0],bytes=f.meta().pending_bytes;
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();
  archive.before=()=>{throw new Error("archive offline");};
  await assert.rejects(flushDurableHistory(f.env),/history_archive_unavailable/);
  assert.deepEqual(f.rows()[0],before);assert.equal(f.meta().pending_bytes,bytes);assert.equal(f.db.calls.length,0);
  archive.before=null;
  f.store.before=sql=>{if(/SET payload_json=\?,payload_bytes=\?,archive_version=1/.test(sql))throw new Error("spill commit failed");};
  await assert.rejects(flushDurableHistory(f.env),/spill commit failed/);
  assert.deepEqual(f.rows()[0],before);assert.equal(f.meta().pending_bytes,bytes);assert.equal(f.db.calls.length,0);
  f.store.before=null;
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.rows()[0].event_id,before.event_id);
});

test("references survive SQL outage, lease replay, failed acknowledgements and feature rollback", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger([event("replay")]));
  const payload=f.rows()[0].payload_json;
  f.db.before=()=>{throw new Error("SQL down");};
  assert.equal((await flushDurableHistory(f.env)).failed,1);
  f.db.before=null;f.advance(120001);
  f.store.before=sql=>{if(/SET status='delivered'/.test(sql))throw new Error("ack failed");};
  await assert.rejects(flushDurableHistory(f.env),/ack failed/);
  assert.equal(f.rows()[0].payload_json,payload);assert.equal(f.rows()[0].status,"pending");
  delete f.env.HISTORY_ARCHIVE_MODE;f.store.before=null;f.restart();f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
  const calls=archive.calls.length;
  assert.equal((await enqueueDurableHistory(f.env,ledger([event("replay")]))).duplicates,1);
  assert.equal(archive.calls.length,calls);
});

test("archive queue protects unknown pending schemas; ref/queue identity mismatch fails before SQL", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger([event("protected")]));
  const row=f.rows()[0],envelope=JSON.parse(row.payload_json);
  envelope.archive_queue_version=2;
  f.store.db.prepare("UPDATE history_queue_events SET payload_json=?").run(JSON.stringify(envelope));
  await assert.rejects(flushDurableHistory(f.env),/history_archive_queue_version_invalid/);
  assert.equal(f.rows()[0].status,"pending");assert.equal(f.rows()[0].attempts,0);assert.equal(f.db.calls.length,0);
  envelope.archive_queue_version=1;
  f.store.db.prepare("UPDATE history_queue_events SET payload_json=?,episode_id='foreign'").run(JSON.stringify(envelope));
  assert.equal((await flushDurableHistory(f.env)).failed,1);assert.equal(f.db.calls.length,0);
  assert.equal(f.rows()[0].status,"pending");
});

test("archive budgets bound requests and compressed bytes without acknowledgements or raw loss", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive,HISTORY_ARCHIVE_DAILY_WRITES:1});
  await enqueueDurableHistory(f.env,ledger([event("budget-1")]));
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("budget-2")])),/history_archive_daily_budget/);
  assert.equal(f.rows().length,1);assert.equal(f.meta().archive_writes,1);
  f.advance(86400000);
  assert.equal((await enqueueDurableHistory(f.env,ledger([event("budget-2")]))).queued,1);
  f.advance(86400000);f.env.HISTORY_ARCHIVE_DAILY_WRITE_BYTES=1;f.restart();
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("byte-budget")])),/history_archive_daily_budget/);
  assert.equal(f.rows().length,2);assert.equal(archive.objects.size,2);
  assert.equal(f.db.calls.length,0);
});

test("R2 requests retain whole-batch ingress limits and invalid batches perform no archive operations", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await assert.rejects(enqueueDurableHistory(f.env,ledger(Array.from({length:26},(_,i)=>event(`many-${i}`)))),/history_batch_max_25/);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("valid"),{...event("invalid"),wallets:{}}])),/history_wallets_must_be_array/);
  const large=Array.from({length:12},(_,i)=>({...event(`big-${i}`),evidence:"x".repeat(95000)}));
  const response=await f.queue.fetch(new Request("https://queue/enqueue",{method:"POST",body:JSON.stringify({events:large})}));
  assert.equal(response.status,413);assert.equal(archive.calls.length,0);assert.equal(f.rows().length,0);
});

test("Turso cutover uses a separate estimate day while leaving DO cap, leases and cursors unchanged", async t => {
  const f=fixture(t);
  await enqueueDurableHistory(f.env,ledger([event("switch")]));
  f.store.db.prepare("UPDATE history_queue_events SET progress_json=?").run(JSON.stringify({version:1,phase:"event"}));
  f.store.db.prepare("UPDATE history_queue_meta SET hist_day=?,hist_writes=80000").run(iso(NOW).slice(0,10));
  const row=f.rows()[0],doWrites=f.meta().do_writes;
  f.env.STORAGE_SQL_BACKEND="turso";f.env.TURSO_DATABASE_URL="libsql://unit.turso.io";f.env.TURSO_AUTH_TOKEN="unit-test";
  f.restart();
  assert.equal(f.queue.historyBudget,180000);
  assert.equal(f.queue.meta(NOW).hist_day,"turso:2026-10-03");
  assert.equal(f.queue.meta(NOW).hist_writes,0);assert.equal(f.queue.meta(NOW).do_writes,doWrites);
  const claim=f.queue.claim(NOW);
  assert.equal(claim.day,"turso:2026-10-03");assert.equal(claim.doDay,"2026-10-03");
  assert.equal(claim.rows.length,1);assert.equal(claim.rows[0].progress_json,row.progress_json);
  assert.ok(f.queue.env.RADAR_HISTORY_DB!==f.db);
  assert.equal(f.meta().hist_writes,40000);
  f.queue.finish(claim,{ingested:[],failed:[],deferred:["switch"],progress:new Map()},0,null,NOW);
  assert.equal(f.meta().hist_writes,0);
  assert.ok(f.meta().do_writes<=50000);
});

test("D1 cannot opt into Turso estimates; only Turso permits at most one million daily estimate units", t => {
  const store=storage(t),env={STORAGE_SQL_BACKEND:"turso",TURSO_DATABASE_URL:"libsql://unit.turso.io",TURSO_AUTH_TOKEN:"unit-test"};
  assert.throws(()=>new HistoryQueue({storage:store},{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000}),/invalid_/);
  assert.throws(()=>new HistoryQueue({storage:store},{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:480000}),/invalid_/);
  assert.equal(new HistoryQueue({storage:store},{...env,HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000}).historyBudget,180000);
  assert.equal(new HistoryQueue({storage:store},{...env,HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:1000000}).historyBudget,1000000);
  assert.throws(()=>new HistoryQueue({storage:store},{...env,HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:1000001}),/invalid_/);
});

test("archived cluster work resumes without replaying the captured cohort", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive,HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:24});
  const wallets=["a","b","c"].map(wallet_address=>({wallet_address,common_funder:"cluster",bought_tokens:100}));
  await enqueueDurableHistory(f.env,ledger([event("archived-cluster",{type:"signal",wallets})]));
  const first=await flushDurableHistory(f.env);
  assert.equal(first.delivered,0);assert.equal(first.continued,1);assert.ok(f.rows()[0].progress_json);
  const payload=f.rows()[0].payload_json;
  for(let i=0;i<100&&f.meta().pending_rows;i++){
    f.advance(86400000);f.restart();await flushDurableHistory(f.env);
    if(f.meta().pending_rows)assert.equal(f.rows()[0].payload_json,payload);
  }
  assert.equal(f.meta().pending_rows,0);
  assert.equal(f.db.calls.filter(call=>/INSERT OR IGNORE INTO signal_episode_events/.test(call.sql)).length,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_wallets").get().n,3);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edge_evidence").get().n,3);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
});

test("expired raw leases require archive verification before SQL after cutover", async t => {
  const archive=evidenceBucket(),f=fixture(t,{RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger([event("old-lease")]));
  const claim=f.queue.claim(NOW),raw=f.rows()[0].payload_json;
  assert.ok(claim.rows[0].lease_token);
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  archive.before=method=>{if(method==="get")throw new Error("cannot verify");};
  const failed=await flushDurableHistory(f.env);
  assert.equal(failed.failed,1);assert.equal(f.db.calls.length,0);assert.equal(f.rows()[0].payload_json,raw);
  archive.before=null;f.advance(240001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.meta().pending_bytes,0);
});

test("archive PUT allowance exhaustion does not block previously archived reference delivery", async t => {
  const archive=evidenceBucket(),f=fixture(t,{RADAR_ARCHIVE:archive,HISTORY_ARCHIVE_DAILY_WRITES:1});
  await enqueueDurableHistory(f.env,ledger([event("legacy"),event("already-archived")]));
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();
  // Spill one row, then hit the PUT cap on the next; accepted archived work can still finish via GET.
  const result=await flushDurableHistory(f.env);
  assert.equal(result.delivered,1);assert.equal(result.failed,1);assert.equal(result.pending,1);
  assert.equal(f.rows().find(row=>row.status==="pending").archive_version,0);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
});

test("known full row capacity fails before creating billable orphan archive objects", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive,HISTORY_QUEUE_MAX_PENDING_ROWS:1});
  await enqueueDurableHistory(f.env,ledger([event("full")]));
  const calls=archive.calls.length;
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("new-full")])),/history_queue_pending_capacity/);
  assert.equal(archive.calls.length,calls);assert.equal(archive.objects.size,1);
});

test("concurrent archived duplicate enqueues retain first accepted evidence without repeated PUT", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  let release,entered;
  const gate=new Promise(resolve=>{release=resolve;}),started=new Promise(resolve=>{entered=resolve;});
  archive.before=async method=>{if(method==="put"){entered();await gate;}};
  const raw=event("concurrent"),changed={...raw,full_evidence:{source:"second"}};
  const first=enqueueDurableHistory(f.env,ledger([raw]));await started;
  const second=enqueueDurableHistory(f.env,ledger([changed]));release();
  assert.equal((await first).queued,1);assert.equal((await second).duplicates,1);
  assert.equal(archive.calls.filter(call=>call.method==="put").length,1);
  archive.before=null;await flushDurableHistory(f.env);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
});

test("archive success followed by batch queue failure never acknowledges; retry reuses both immutable objects", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  f.store.before=(sql,values)=>{if(/INSERT INTO history_queue_events/.test(sql)&&values[0]==="second")throw new Error("queue failed");};
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("first"),event("second")])),/queue failed/);
  assert.equal(f.rows().length,0);assert.equal(f.meta().pending_rows,0);assert.equal(f.meta().pending_bytes,0);
  assert.equal(archive.objects.size,2);assert.equal(f.db.calls.length,0);
  f.store.before=null;
  assert.equal((await enqueueDurableHistory(f.env,ledger([event("first"),event("second")]))).queued,2);
  assert.equal(archive.calls.filter(call=>call.method==="put").length,2);
  assert.equal((await flushDurableHistory(f.env)).delivered,2);
});

test("daily derived-mode estimates batch lightweight inline and archived work within the same 40-query cap", async t => {
  const f=fixture(t,{HISTORY_DERIVED_MODE:"daily",RADAR_ARCHIVE:evidenceBucket()});
  const rows=Array.from({length:10},(_,i)=>outcome(`daily-${i}`));
  await enqueueDurableHistory(f.env,ledger(rows));
  const inline=f.queue.claim(NOW);
  assert.equal(inline.rows.length,5);
  f.queue.finish(inline,{ingested:[],failed:[],deferred:inline.rows.map(row=>row.event_id),progress:new Map()},0,null,NOW);
  f.store.db.prepare("UPDATE history_queue_events SET progress_json=?,next_attempt_at=?").run(JSON.stringify({phase:"derived_dirty"}),NOW);
  assert.equal(f.queue.claim(NOW).rows.length,10);
  // Reuse the same fixture after clearing expired leases, then safely migrate the raw payloads to references.
  f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  f.store.db.prepare("UPDATE history_queue_events SET lease_token=NULL,progress_json=NULL,next_attempt_at=?").run(NOW);
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();await f.queue.spillLegacyRows();
  const archived=f.queue.claim(NOW+HISTORY_QUEUE_LIMITS.leaseMs+1);
  assert.equal(archived.rows.length,5);
  assert.equal(JSON.parse(archived.rows[0].payload_json).work.initial_queries,8);
  f.queue.finish(archived,{ingested:[],failed:[],deferred:archived.rows.map(row=>row.event_id),progress:new Map()},0,null);
  f.store.db.prepare("UPDATE history_queue_events SET progress_json=?,next_attempt_at=?").run(JSON.stringify({phase:"derived_dirty"}),NOW);
  assert.equal(f.queue.claim(NOW+HISTORY_QUEUE_LIMITS.leaseMs+1).rows.length,10);
});

test("25-event R2 ingress keeps its atomic client wire within the internal-service budget", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  const rows=Array.from({length:25},(_,i)=>event(`ingress-${String(i).padStart(2,"0")}`));
  f.store.before=(sql,values)=>{if(/INSERT INTO history_queue_events/.test(sql)&&values[0]===rows[12].event_id)throw new Error("atomic commit failed");};
  await assert.rejects(enqueueDurableHistory(f.env,ledger(rows)),/atomic commit failed/);
  assert.equal(archive.calls.length,75);assert.equal(archive.objects.size,25);
  assert.equal(f.rows().length,0);assert.equal(f.meta().pending_rows,0);assert.equal(f.meta().pending_bytes,0);
  assert.equal(f.db.calls.length,0);assert.equal(f.requests.length,1);
  f.store.before=null;
  const before=archive.calls.length,retry=await enqueueDurableHistory(f.env,ledger(rows));
  assert.equal(retry.queued,25);assert.equal(archive.calls.length-before,25);
  assert.ok(archive.calls.slice(before).every(call=>call.method==="head"));
  assert.equal(f.requests.length,2);assert.equal(f.rows().filter(row=>row.archive_version===1).length,25);
  const replay=archive.calls.length;
  assert.equal((await enqueueDurableHistory(f.env,ledger(rows))).duplicates,25);
  assert.equal(archive.calls.length,replay);
});

test("archived lightweight flush planning shares the cap between hydration and SQL", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive});
  await enqueueDurableHistory(f.env,ledger(Array.from({length:25},(_,i)=>event(`bounded-${String(i).padStart(2,"0")}`))));
  const before=archive.calls.length,queries=f.db.calls.length,result=await flushDurableHistory(f.env);
  assert.equal(result.delivered,15);assert.equal(result.pending,10);assert.equal(result.continued,0);
  assert.equal(result.history_requests,45);assert.equal(result.archive_requests,15);assert.equal(result.history_queries,30);
  assert.equal(result.external_history_requests,0);
  assert.equal(archive.calls.length-before+f.db.calls.length-queries,result.history_requests);
  await drain(f);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,25);
});

test("legacy spill and resumable graph processing never exceed one shared 45-request invocation", async t => {
  const archive=evidenceBucket(),f=fixture(t,{RADAR_ARCHIVE:archive});
  const wallets=Array.from({length:60},(_,i)=>({wallet_address:`budget-${i}`,common_funder:"private-funder",bought_tokens:100}));
  const raw=event("00-heavy",{type:"signal",wallets});
  const legacy=Array.from({length:5},(_,i)=>event(`light-${i}`));
  await enqueueDurableHistory(f.env,ledger([raw,...legacy]));
  f.store.db.prepare("UPDATE history_queue_events SET next_attempt_at=? WHERE event_id<>'00-heavy'").run(NOW+3600000);
  f.env.HISTORY_ARCHIVE_MODE="r2";f.restart();
  const before=archive.calls.length,first=await flushDurableHistory(f.env);
  assert.equal(first.spilled,5);assert.equal(first.archive_requests,16);assert.equal(first.history_queries,29);
  assert.equal(first.history_requests,45);assert.equal(archive.calls.length-before,16);
  assert.equal(first.continued,1);assert.equal(first.failed,0);assert.equal(first.error,null);
  const captured=f.rows().find(row=>row.event_id===raw.event_id).payload_json;
  assert.ok(f.rows().find(row=>row.event_id===raw.event_id).progress_json);
  for (let i=0;i<80&&f.rows().find(row=>row.event_id===raw.event_id).status==="pending";i++) {
    f.advance(1001);f.restart();
    const calls=archive.calls.length,result=await flushDurableHistory(f.env);
    assert.ok(result.history_requests<=45);assert.ok(result.history_queries<=40);
    assert.equal(result.archive_requests,archive.calls.length-calls);assert.equal(result.failed,0);
    if (f.rows().find(row=>row.event_id===raw.event_id).status==="pending") {
      assert.equal(f.rows().find(row=>row.event_id===raw.event_id).payload_json,captured);
    }
  }
  assert.equal(f.rows().find(row=>row.event_id===raw.event_id).status,"delivered");
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_episode_events WHERE event_id='00-heavy'").get().n,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_wallets").get().n,60);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edge_evidence").get().n,1770);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  assert.ok(f.rows().filter(row=>row.event_id!==raw.event_id).every(row=>row.status==="pending"&&row.next_attempt_at===NOW+3600000));
});

test("Turso SQL requests are classified external and retain the 40-query cap alongside R2", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive,
    STORAGE_SQL_BACKEND:"turso",TURSO_DATABASE_URL:"libsql://unit.turso.io",TURSO_AUTH_TOKEN:"unit-test"});
  // Execute the identical checked SQL interface locally; real provider timing requires a live tail smoke.
  f.queue.env.RADAR_HISTORY_DB=f.db;
  await enqueueDurableHistory(f.env,ledger(Array.from({length:25},(_,i)=>event(`external-${i}`))));
  const result=await flushDurableHistory(f.env);
  assert.equal(result.external_history_requests,result.history_queries);assert.equal(result.external_history_requests,30);
  assert.equal(result.archive_requests,15);assert.equal(result.history_requests,45);
  assert.equal(result.failed,0);assert.equal(result.delivered,15);
});

test("flush query override is only lowerable and status reports its active value", async t => {
  const f=fixture(t,{HISTORY_QUEUE_FLUSH_QUERIES:"24"});
  assert.equal(f.queue.queryLimit,24);
  assert.equal((await durableHistoryStatus(f.env)).limits.flushQueries,24);
  assert.equal(f.queue.historyBudget,80000);
  assert.equal(new HistoryQueue({storage:f.store},{}).queryLimit,40);
  for (const value of [0,41,-1,1.5,"invalid"]) {
    assert.throws(()=>new HistoryQueue({storage:f.store},{HISTORY_QUEUE_FLUSH_QUERIES:value}),/invalid_HISTORY_QUEUE_FLUSH_QUERIES/);
  }
});

test("Turso 24-query flush resumes archived daily-mode graph work across ticks and restarts", async t => {
  const archive=evidenceBucket(),f=fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:archive,
    STORAGE_SQL_BACKEND:"turso",TURSO_DATABASE_URL:"libsql://unit.turso.io",TURSO_AUTH_TOKEN:"unit-test",
    HISTORY_QUEUE_FLUSH_QUERIES:24,HISTORY_DERIVED_MODE:"daily"});
  f.db.db.exec(readFileSync(new URL("../migrations-storage/0001_daily_learning.sql",import.meta.url),"utf8"));
  let roundTrips=0;
  const wrap=statement=>({statement,
    bind:(...values)=>wrap(statement.bind(...values)),
    run:()=>{roundTrips++;return statement.run();},
    all:()=>{roundTrips++;return statement.all();},
    first:(...args)=>{roundTrips++;return statement.first(...args);},
  });
  const counted={prepare:sql=>wrap(f.db.prepare(sql)),
    batch:statements=>{roundTrips++;return f.db.batch(statements.map(item=>item.statement));}};
  f.queue.env.RADAR_HISTORY_DB=counted;
  const wallets=Array.from({length:60},(_,i)=>({wallet_address:`limited-${i}`,common_funder:"private-funder",bought_tokens:100}));
  await enqueueDurableHistory(f.env,ledger([event("limited",{type:"signal",wallets})]));
  const payload=f.rows()[0].payload_json;
  assert.equal(JSON.parse(payload).work.initial_queries,40);
  let ticks=0;
  while (f.meta().pending_rows&&ticks<80) {
    const before=roundTrips,result=await flushDurableHistory(f.env);
    assert.ok(roundTrips-before<=24);
    assert.equal(result.history_queries,roundTrips-before);
    assert.equal(result.external_history_requests,roundTrips-before);
    assert.ok(result.history_requests<=45);assert.equal(result.failed,0,result.error);assert.equal(result.error,null);
    if (!ticks) { assert.equal(result.history_queries,24);assert.equal(result.continued,1); }
    ticks++;
    if (f.meta().pending_rows) {
      assert.equal(f.rows()[0].payload_json,payload);assert.ok(f.rows()[0].progress_json);
      f.advance(1001);f.restart();f.queue.env.RADAR_HISTORY_DB=counted;
      assert.equal(f.queue.queryLimit,24);
    }
  }
  assert.ok(ticks>1);assert.equal(f.meta().pending_rows,0);assert.equal(f.rows()[0].status,"delivered");
  assert.equal((await durableHistoryStatus(f.env)).limits.flushQueries,24);
  assert.equal(f.db.calls.filter(call=>/INSERT OR IGNORE INTO signal_episode_events/.test(call.sql)).length,1);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM signal_wallets").get().n,60);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) n FROM wallet_cluster_edge_evidence").get().n,1770);
  assert.equal(f.db.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
});
