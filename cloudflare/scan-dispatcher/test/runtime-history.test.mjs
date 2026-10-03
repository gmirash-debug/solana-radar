import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { historyEventId, ingestHistoryBatch } from "../src/history.js";
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
  row.outcome = { horizons: { "1h": {
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
    return { toArray: () => [], rowsWritten: changes };
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
  for (const filename of ["0001_wallet_edge_history.sql", "0002_cluster_edge_evidence.sql"]) {
    db.exec(readFileSync(new URL(`../migrations-history/${filename}`, import.meta.url), "utf8"));
  }
  const api = { db, before: null, calls: [] };
  async function execute(sql, values, method) {
    api.calls.push({ sql, values, method });
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
  assert.equal((await flushDurableHistory(f.env)).delivered, 2);
  const ids = f.db.calls.filter(call => /INSERT OR IGNORE INTO signal_episode_events/.test(call.sql)).map(call => call.values[0]);
  assert.deepEqual(ids, ["z-old", "m-middle"]);
  assert.equal(f.rows().find(row => row.event_id === "a-new").status, "pending");
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

test("derived read failure acknowledges none even after successful historical writes", async t => {
  const f = fixture(t);
  const rows = [outcome("result"), event("companion")];
  await enqueueDurableHistory(f.env, ledger(rows));
  f.db.before = ({ sql }) => { if (/GROUP BY e.mcap_band/.test(sql)) throw new Error("baseline refresh failed"); };
  const failed = await flushDurableHistory(f.env);
  assert.equal(failed.delivered, 0);
  assert.equal(failed.failed, 2);
  assert.match(failed.error, /baseline refresh failed/);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 2);
  assert.ok(f.rows().every(row => row.status === "pending" && row.payload_json));
  f.db.before = null;
  f.advance(120001);
  assert.equal((await flushDurableHistory(f.env)).delivered, 2);
  assert.equal(f.db.db.prepare("SELECT COUNT(*) AS n FROM signal_episode_events").get().n, 2);
});

test("derived write failure also prevents all delivery acknowledgements", async t => {
  const f = fixture(t);
  await enqueueDurableHistory(f.env, ledger([outcome("result"), event("other")]));
  f.db.before = ({ sql }) => {
    if (/INSERT INTO market_baselines/.test(sql)) return { success: false, error: "derived write rejected" };
  };
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered, 0);
  assert.equal(result.pending, 2);
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
  assert.equal(result.failed, 1);
  assert.equal(f.meta().hist_writes, 16);
  f.advance(120001);
  await assert.rejects(flushDurableHistory(f.env), /history_daily_write_budget/);
  f.advance(86400_000);
  assert.equal((await flushDurableHistory(f.env)).delivered, 1);
  assert.equal(f.meta().hist_writes, 16);
});

test("batch write budget is reserved before D1 batch writes and keeps poison payload", async t => {
  const f = fixture(t, { HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS: 16 });
  const row = event("signal", { type: "signal" });
  await enqueueDurableHistory(f.env, ledger([row]));
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered, 0);
  assert.equal(result.history_write_units, 16);
  assert.match(f.rows()[0].last_error, /history_flush_write_budget/);
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
