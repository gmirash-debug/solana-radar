import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import test from "node:test";
import {backendEnv, createTursoDatabase, resolveStorageEnv, StorageSqlError} from "../src/storage-sql.js";

const credentials = {TURSO_DATABASE_URL:"libsql://radar-test.turso.io", TURSO_AUTH_TOKEN:"test.secret.token"};
const value = n => typeof n === "bigint" ? {type:"integer", value:String(n)}
  : n === null ? {type:"null"} : typeof n === "number" ? {type:"float", value:n}
  : typeof n === "string" ? {type:"text", value:n}
  : {type:"blob", base64:Buffer.from(n).toString("base64")};
const decode = n => n.type === "null" ? null : n.type === "integer" ? BigInt(n.value)
  : n.type === "blob" ? new Uint8Array(Buffer.from(n.base64, "base64")) : n.value;
const emptyResult = () => ({cols:[], rows:[], affected_row_count:0, last_insert_rowid:null});
const ok = (type, result) => ({type:"ok", response:{type, ...(result === undefined ? {} : {result})}});
const sqlError = (code = "SQLITE_ERROR", message = "SQL failure") => ({code, message});
const pipeline = result => ({baton:null, base_url:null, results:[ok("batch", result), ok("close")]});
const successfulSingle = result => pipeline({step_results:[emptyResult(), result], step_errors:[null, null]});
const rowResult = (names = ["n"], rows = [[{type:"integer", value:"1"}]]) => ({
  cols:names.map(name => ({name, decltype:null})), rows, affected_row_count:0, last_insert_rowid:null,
});
const staticDb = (body, options = {}) => createTursoDatabase(credentials, {fetch:async()=>Response.json(body), ...options});
const rejectsCode = (work, code) => assert.rejects(work, error => error instanceof StorageSqlError && error.code === code);

test("native fetch retains the global receiver in Worker runtimes", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async function () {
    assert.equal(this, globalThis, "native fetch must not receive the database instance");
    return Response.json(successfulSingle(rowResult()));
  };
  const db = createTursoDatabase(credentials);
  assert.equal(await db.prepare("SELECT 1 n").first("n"), 1);
});

// This implements the public Hrana conditions against SQLite, not the adapter's
// own condition logic, so the rollback tests exercise the transaction contract.
function fixture(t, {counters = false} = {}) {
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  const calls = [];
  const executed = [];
  let failSql = null;
  let mutate = null;
  const condition = (cond, results, errors) => !cond ? true : cond.type === "ok" ? results[cond.step] !== null
    : cond.type === "error" ? errors[cond.step] !== null : cond.type === "not" ? !condition(cond.cond, results, errors)
    : cond.type === "and" ? cond.conds.every(item => condition(item, results, errors))
    : cond.type === "or" ? cond.conds.some(item => condition(item, results, errors)) : assert.fail("unsupported Hrana condition");
  const fetchImpl = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push({url, options, body});
    assert.equal(body.baton, null);
    assert.equal(body.requests.length, 2);
    assert.equal(body.requests[1].type, "close");
    const results = [];
    const errors = [];
    const steps = body.requests[0].batch.steps;
    for (const step of steps) {
      if (!condition(step.condition, results, errors)) { results.push(null); errors.push(null); continue; }
      try {
        executed.push(step.stmt.sql);
        if (failSql?.(step.stmt.sql)) throw Object.assign(new Error("injected SQL error"), {errcode:1});
        const statement = sqlite.prepare(step.stmt.sql);
        statement.setReadBigInts(true);
        const columns = statement.columns();
        const records = statement.all(...(step.stmt.args || []).map(decode));
        const dml = /^\s*(INSERT|UPDATE|DELETE|REPLACE)\b/i.test(step.stmt.sql);
        const info = sqlite.prepare("SELECT changes() changed, last_insert_rowid() id").get();
        const changes = dml ? Number(info.changed) : 0;
        results.push({cols:columns.map(col => ({name:col.name, decltype:col.type})),
          rows:step.stmt.want_rows === false ? [] : records.map(row => columns.map(col => value(row[col.name]))),
          affected_row_count:changes, last_insert_rowid:dml ? String(info.id) : null,
          ...(counters ? {rows_written:changes * 3, rows_read:records.length + 10, query_duration_ms:1.25} : {})});
        errors.push(null);
      } catch (error) {
        results.push(null);
        errors.push(sqlError((error.errcode & 255) === 19 ? "SQLITE_CONSTRAINT" : "SQLITE_ERROR", error.message));
      }
    }
    const response = pipeline({step_results:results, step_errors:errors});
    if (mutate) mutate(response);
    // Closing a stream must roll back a transaction if COMMIT/ROLLBACK failed.
    try { sqlite.exec("ROLLBACK"); } catch { /* Autocommit stream. */ }
    return Response.json(response);
  };
  const db = createTursoDatabase(credentials, {fetchImpl});
  return {db, sqlite, calls, executed, set failSql(fn) {failSql = fn;}, set mutate(fn) {mutate = fn;}};
}

test("backend is opt-in, synchronous, canonical and never silently falls back", async t => {
  assert.equal(resolveStorageEnv, backendEnv);
  const d1 = {prepare() {assert.fail("D1 must not be contacted");}};
  for (const flag of [undefined, null, "", "d1"]) {
    const env = {STORAGE_SQL_BACKEND:flag, RADAR_DB:d1};
    assert.equal(resolveStorageEnv(env), env);
  }
  const f = fixture(t);
  const env = resolveStorageEnv({...credentials, STORAGE_SQL_BACKEND:"turso", RADAR_DB:d1, RADAR_HISTORY_DB:d1,
    OTHER_BINDING:42}, {fetchImpl:async(...args) => {
      const request = JSON.parse(args[1].body);
      assert.equal(request.requests[0].type, "batch");
      return Response.json(successfulSingle(rowResult()));
    }});
  assert.equal(env.RADAR_DB, env.RADAR_HISTORY_DB);
  assert.equal(env.OTHER_BINDING, 42);
  assert.notEqual(env.RADAR_DB, d1);
  assert.equal(await env.RADAR_DB.prepare("SELECT 1 n").first("n"), 1);
  assert.equal(f.calls.length, 0);
  for (const missing of [{}, {TURSO_DATABASE_URL:credentials.TURSO_DATABASE_URL}, {TURSO_AUTH_TOKEN:"token"}]) {
    assert.throws(() => resolveStorageEnv({...missing, STORAGE_SQL_BACKEND:"turso", RADAR_DB:d1}),
      {code:"storage_sql_turso_credentials_missing"});
  }
  assert.throws(() => resolveStorageEnv({STORAGE_SQL_BACKEND:"turzo", RADAR_DB:d1}), {code:"storage_sql_backend_invalid"});
});

test("URL conversion and credentials stay in headers, private fields, and the configured origin", async()=> {
  for (const url of ["libsql://radar-test.turso.io", "https://radar-test.turso.io/", "https://radar-test.turso.io/v2/pipeline/"]) {
    let call;
    const db = createTursoDatabase({...credentials, TURSO_DATABASE_URL:url}, {fetch:async(endpoint, options) => {
      call = {endpoint, options};
      return Response.json(successfulSingle(rowResult()));
    }});
    assert.deepEqual(Object.keys(db), []);
    assert.equal(JSON.stringify(db), "{}");
    await db.prepare("SELECT 1 n").all();
    assert.equal(call.endpoint, "https://radar-test.turso.io/v2/pipeline");
    assert.equal(call.options.redirect, "error");
    assert.equal(call.options.headers.Authorization, "Bearer test.secret.token");
    assert.equal(call.options.body.includes(credentials.TURSO_AUTH_TOKEN), false);
  }
  for (const url of ["http://db.turso.io", "file:///private/db", "wss://db.turso.io", "not a url",
    "https://secret@db.turso.io", "https://user:pass@db.turso.io", "https://db.turso.io?token=secret",
    "libsql://db.turso.io?tls=0", "https://db.turso.io#secret", "https://db.turso.io/other-path", "https://db.turso.io/\npath"]) {
    assert.throws(() => createTursoDatabase({...credentials, TURSO_DATABASE_URL:url}), {code:"storage_sql_database_url_invalid"});
  }
  assert.throws(() => createTursoDatabase({...credentials, TURSO_AUTH_TOKEN:"a\nb"}), {code:"storage_sql_auth_token_invalid"});
  for (const timeoutMs of [0, -1, 60001, 1.5, "nope"]) {
    assert.throws(() => createTursoDatabase(credentials, {timeoutMs}), {code:"storage_sql_timeout_invalid"});
  }
});

test("typed parameters preserve NULL, floats, safe integers, large signed ints, text and blob views", async t => {
  const f = fixture(t);
  const bytes = new Uint8Array([99, 0, 255, 77]);
  const original = f.db.prepare("SELECT ?1 nil, ?2 float_value, ?3 safe, ?4 large, ?5 txt, ?6 bytes");
  const bound = original.bind(null, 1.125, Number.MAX_SAFE_INTEGER, 9223372036854775807n, "quoted'text", bytes.subarray(1, 3));
  const row = await bound.first();
  assert.equal(row.nil, null);
  assert.equal(row.float_value, 1.125);
  assert.equal(row.safe, Number.MAX_SAFE_INTEGER);
  assert.equal(row.large, "9223372036854775807");
  assert.equal(row.txt, "quoted'text");
  assert.deepEqual(new Uint8Array(row.bytes), new Uint8Array([0, 255]));
  const stmt = f.calls[0].body.requests[0].batch.steps[1].stmt;
  assert.deepEqual(stmt.args.map(item => item.type), ["null", "float", "integer", "integer", "text", "blob"]);
  for (const n of [undefined, true, false, {}, [], NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => f.db.prepare("SELECT ?1").bind(n), StorageSqlError);
  }
  for (const n of [9223372036854775808n, -9223372036854775809n]) {
    assert.throws(() => f.db.prepare("SELECT ?1").bind(n), {code:"storage_sql_integer_out_of_range"});
  }
  const unbound = await original.first();
  assert.equal(unbound.nil, null);
  assert.equal(f.calls[1].body.requests[0].batch.steps[1].stmt.args.length, 0, "bind does not mutate the original statement");
  // node:sqlite's text read truncates embedded NULs; validate wire preservation
  // independently rather than confusing that local driver with Hrana.
  const nulText = "quoted'\u0000text";
  const textDb = createTursoDatabase(credentials, {fetch:async(_, options) => {
    assert.equal(JSON.parse(options.body).requests[0].batch.steps[1].stmt.args[0].value, nulText);
    return Response.json(successfulSingle(rowResult(["txt"], [[value(nulText)]])));
  }});
  assert.equal(await textDb.prepare("SELECT ?1 txt").bind(nulText).first("txt"), nulText);
});

test("all/run metadata reports measured counters or explicitly labelled conservative fallbacks", async t => {
  const f = fixture(t, {counters:true});
  await f.db.exec("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT UNIQUE)");
  const inserted = await f.db.prepare("INSERT INTO items(value) VALUES (?1)").bind("one").run();
  assert.equal(inserted.success, true);
  assert.deepEqual(inserted.results, []);
  assert.deepEqual(inserted.meta, {changes:1, rows_written:3, rows_read:10, duration:1.25, last_row_id:1});
  const duplicate = await f.db.prepare("INSERT OR IGNORE INTO items(value) VALUES (?1)").bind("one").run();
  assert.equal(duplicate.meta.changes, 0);
  assert.equal(duplicate.meta.rows_written, 0);
  const queried = await f.db.prepare("SELECT value FROM items").all();
  assert.deepEqual(queried.results, [{value:"one"}]);
  assert.equal(queried.meta.rows_read, 11);
  const fallback = await staticDb(successfulSingle({...rowResult(), affected_row_count:2})).prepare("SELECT 1 n").all();
  assert.equal(fallback.meta.rows_written, 2);
  assert.equal(fallback.meta.rows_written_estimated, true);
  assert.equal(fallback.meta.rows_read_estimated, true);
  assert.ok(fallback.meta.duration >= 0);
});

test("first supports a named column, no rows and SQL NULL; raw preserves column order and duplicate names", async t => {
  const f = fixture(t);
  assert.deepEqual(await f.db.prepare("SELECT 0 a, NULL b").first(), {a:0, b:null});
  assert.equal(await f.db.prepare("SELECT 0 a, NULL b").first("a"), 0);
  assert.equal(await f.db.prepare("SELECT NULL b").first("b"), null);
  assert.equal(await f.db.prepare("SELECT 1 n WHERE 0").first(), null);
  await rejectsCode(() => f.db.prepare("SELECT 1 n").first("absent"), "storage_sql_first_column_missing");
  await rejectsCode(() => f.db.prepare("SELECT 1 n").first(1), "storage_sql_first_column_invalid");
  const duplicate = rowResult(["same", "same", "__proto__"], [[value(1n), value(2n), value("safe")]]);
  const db = staticDb(successfulSingle(duplicate));
  assert.deepEqual(await db.prepare("SELECT 1").raw(), [[1, 2, "safe"]]);
  assert.deepEqual(await db.prepare("SELECT 1").raw({columnNames:true}), [["same", "same", "__proto__"], [1, 2, "safe"]]);
  const row = await db.prepare("SELECT 1").first();
  assert.equal(row.same, 2);
  assert.equal(Object.getPrototypeOf(row), Object.prototype);
  assert.equal(Object.hasOwn(row, "__proto__"), true);
  await rejectsCode(() => db.prepare("SELECT 1").raw({columnNames:"yes"}), "storage_sql_raw_options_invalid");
});

test("batch commits all statements, preserves returned results and rolls back on a middle statement failure", async t => {
  const f = fixture(t);
  await f.db.exec("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT UNIQUE)");
  const result = await f.db.batch([
    f.db.prepare("INSERT INTO items(value) VALUES (?1)").bind("one"),
    f.db.prepare("INSERT INTO items(value) VALUES (?1) RETURNING id, value").bind("two"),
    f.db.prepare("SELECT COUNT(*) n FROM items"),
  ]);
  assert.equal(result.length, 3);
  assert.equal(result[0].meta.changes, 1);
  assert.deepEqual(result[1].results, [{id:2, value:"two"}]);
  assert.deepEqual(result[2].results, [{n:2}]);
  const calls = f.calls.length;
  const statementCount = f.executed.length;
  await assert.rejects(() => f.db.batch([
    f.db.prepare("INSERT INTO items(value) VALUES ('three')"),
    f.db.prepare("INSERT INTO items(value) VALUES ('one')"),
    f.db.prepare("INSERT INTO items(value) VALUES ('four')"),
  ]), error => error.code === "storage_sql_statement_failed" && error.sqlCode === "SQLITE_CONSTRAINT");
  assert.equal(f.calls.length, calls + 1, "no retry of an unsuccessful transaction");
  const executed = f.executed.slice(statementCount);
  assert.ok(executed.includes("ROLLBACK"));
  assert.ok(!executed.includes("COMMIT"));
  assert.ok(!executed.some(sql => sql.includes("'four'")));
  assert.deepEqual(f.sqlite.prepare("SELECT value FROM items ORDER BY id").all().map(row => row.value), ["one", "two"]);
});

test("BEGIN, foreign key setup and COMMIT failures cannot apply a partial transaction", async t => {
  const f = fixture(t);
  await f.db.exec("CREATE TABLE items(value TEXT)");
  for (const target of ["PRAGMA foreign_keys = ON", "BEGIN IMMEDIATE", "COMMIT"]) {
    const offset = f.executed.length;
    f.failSql = sql => sql === target;
    await rejectsCode(() => f.db.batch([f.db.prepare("INSERT INTO items VALUES ('one')")]), "storage_sql_statement_failed");
    assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM items").get().n, 0);
    const executed = f.executed.slice(offset);
    assert.equal(executed.includes("ROLLBACK"), target === "COMMIT");
  }
  f.failSql = sql => sql === "COMMIT" || sql === "ROLLBACK";
  await rejectsCode(() => f.db.batch([f.db.prepare("INSERT INTO items VALUES ('one')")]), "storage_sql_statement_failed");
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM items").get().n, 0, "stream close also rolls back");
});

test("foreign keys are enabled on every stream, not only on the migration stream", async t => {
  const f = fixture(t);
  await f.db.exec("PRAGMA foreign_keys = ON; CREATE TABLE parents(id INTEGER PRIMARY KEY); CREATE TABLE children(parent_id INTEGER REFERENCES parents(id));");
  f.sqlite.exec("PRAGMA foreign_keys=OFF");
  await rejectsCode(() => f.db.prepare("INSERT INTO children VALUES (99)").run(), "storage_sql_statement_failed");
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM children").get().n, 0);
  assert.equal((await f.db.prepare("PRAGMA foreign_keys").first()).foreign_keys, 1);
  await rejectsCode(() => f.db.exec("PRAGMA foreign_keys = OFF; CREATE TABLE ignored(n);"), "storage_sql_exec_pragma_unsupported");
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE name='ignored'").get().n, 0);
});

test("exec applies actual operational and history migrations and compatible JSON/numbered parameter SQL", async t => {
  const f = fixture(t);
  for (const file of ["migrations/0001_radar_data.sql", "migrations/0002_history_outbox.sql",
    "migrations-history/0001_wallet_edge_history.sql", "migrations-history/0002_cluster_edge_evidence.sql",
    "migrations-history/0003_resumable_history.sql"]) {
    const result = await f.db.exec(readFileSync(new URL(`../${file}`, import.meta.url), "utf8"));
    assert.ok(result.count > 0);
  }
  assert.equal(await f.db.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE type='table'").first("n"), 20);
  assert.equal(await f.db.prepare("SELECT json_extract(?1, '$.balance') n").bind('{"balance":12.5}').first("n"), 12.5);
  assert.deepEqual(await f.db.prepare("SELECT value FROM json_each(?1) ORDER BY value").bind('[3,1,2]').raw(), [[1], [2], [3]]);
  assert.equal(await f.db.prepare("WITH vals AS (SELECT ?2 n) SELECT n + ?1 total FROM vals").bind(1, 9).first("total"), 10);
  assert.equal(await f.db.prepare("SELECT job_id FROM history_cluster_lock WHERE id=1").first("job_id"), null);
});

test("production resumable history pipeline accepts the adapter and replay preserves the original cohort", async t => {
  const {resumeHistoryEvent} = await import("../src/history-progress.js");
  const f = fixture(t);
  for (const file of ["0001_wallet_edge_history.sql", "0002_cluster_edge_evidence.sql", "0003_resumable_history.sql"]) {
    await f.db.exec(readFileSync(new URL(`../migrations-history/${file}`, import.meta.url), "utf8"));
  }
  const now = "2026-10-03T12:00:00Z";
  const raw = {event_id:"adapter-original-signal", episode:{episode_id:"adapter-episode", token_address:"token:adapter",
    caught_at:"2026-10-03T08:00:00Z", caught_mcap_usd:80000, caught_liquidity_usd:20000, token_age_days:40},
    event:{event_type:"signal", observed_at:"2026-10-03T08:00:00Z", mcap_usd:80000},
    wallets:["alice", "bob"].map(wallet_address => ({wallet_address, bought_tokens:100, buy_sol:2,
      current_token_balance:100, common_funder:"private-funder"}))};
  const state = {};
  await resumeHistoryEvent(f.db, raw, state, {now, remaining:()=>10000});
  assert.equal(state.phase, "done");
  assert.equal(await f.db.prepare("SELECT COUNT(*) n FROM signal_wallets").first("n"), 2);
  const observation = {...raw, event_id:"adapter-retention", event:{event_type:"retention_check", observed_at:now},
    wallets:raw.wallets.map(row => ({...row, current_token_balance:20}))};
  await resumeHistoryEvent(f.db, observation, {}, {now, remaining:()=>10000});
  await resumeHistoryEvent(f.db, observation, {}, {now, remaining:()=>10000});
  assert.deepEqual(await f.db.prepare("SELECT bought_tokens, retained_pct_at_catch FROM signal_wallets").raw(), [[100, 100], [100, 100]]);
  assert.equal(await f.db.prepare("SELECT COUNT(*) n FROM wallet_observation_bundles").first("n"), 1);
  const bundle = await f.db.prepare("SELECT observations_json FROM wallet_observation_bundles").first("observations_json");
  assert.deepEqual(JSON.parse(bundle).map(row => row.current_token_balance), [20, 20]);
});

test("migration SQL splitter handles strings, all quote styles, comments and trigger CASE blocks", async t => {
  const f = fixture(t);
  const script = `-- comment ; quote '
    CREATE TABLE [semi;table]("name;value" TEXT, \`other;field\` INTEGER);
    /* block ; \" quote */ INSERT INTO [semi;table] VALUES ('it''s;a;value', 1);
    CREATE TABLE log(value TEXT);
    CREATE TRIGGER echo AFTER INSERT ON [semi;table] BEGIN
      INSERT INTO log VALUES (CASE WHEN NEW.\`other;field\` > 1 THEN 'high;value' ELSE 'low;value' END);
      INSERT INTO log VALUES ('second;value');
    END;
    INSERT INTO [semi;table] VALUES ('last;value', 2); -- trailing comment ;`;
  const result = await f.db.exec(script);
  assert.equal(result.count, 5);
  assert.deepEqual(await f.db.prepare("SELECT value FROM log").raw(), [["high;value"], ["second;value"]]);
  assert.equal((await f.db.prepare("SELECT \"name;value\" n FROM [semi;table] LIMIT 1").first()).n, "it's;a;value");
  const before = f.calls.length;
  assert.deepEqual(await f.db.exec("-- comments only;\n /* ; */"), {count:0, duration:0});
  assert.equal(f.calls.length, before);
  const declaration = await f.db.exec("PRAGMA foreign_keys=ON;");
  assert.equal(declaration.count, 1);
});

test("invalid SQL, unsupported controls and oversized batches fail before sending any request", async t => {
  const f = fixture(t);
  for (const sql of ["", "-- just a comment", "SELECT 1; SELECT 2", "SELECT 'unclosed", "SELECT \"unclosed",
    "SELECT [unclosed", "SELECT 1 /* unterminated", "SELECT 1\0", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT x",
    "RELEASE x", "ATTACH DATABASE 'file' AS x", "DETACH x"]) {
    assert.throws(() => f.db.prepare(sql), StorageSqlError);
  }
  assert.throws(() => f.db.prepare("CREATE TRIGGER x AFTER INSERT ON y BEGIN SELECT 1;"), {code:"storage_sql_unterminated_trigger"});
  await rejectsCode(() => f.db.batch(Array.from({length:101}, () => f.db.prepare("SELECT 1"))), "storage_sql_batch_invalid");
  await rejectsCode(() => f.db.batch([{}]), "storage_sql_batch_statement_invalid");
  await rejectsCode(() => f.db.batch([staticDb(successfulSingle(rowResult())).prepare("SELECT 1")]), "storage_sql_batch_database_mismatch");
  await rejectsCode(() => f.db.batch([f.db.prepare("PRAGMA user_version")]), "storage_sql_batch_statement_unsupported");
  await rejectsCode(() => f.db.exec("BEGIN; CREATE TABLE y(n); COMMIT;"), "storage_sql_transaction_control_unsupported");
  assert.deepEqual(await f.db.batch([]), []);
  assert.equal(f.calls.length, 0);
});

test("statement and transport errors never echo credentials, endpoint, SQL or bound values", async()=> {
  const secretMessage = `fetch https://secret-user:password@db.turso.io/path?token=${credentials.TURSO_AUTH_TOKEN} SQL private_value`;
  const errors = [
    async()=> {throw new Error(secretMessage);},
    async()=> new Response(secretMessage, {status:401}),
    async()=> Response.json({baton:null, results:[{type:"error", error:sqlError("SQLITE_ERROR", secretMessage)}, ok("close")]}),
    async()=> Response.json(pipeline({step_results:[emptyResult(), null], step_errors:[null, sqlError("SQLITE_ERROR", secretMessage)]})),
    async()=> Response.json(pipeline({step_results:[emptyResult(), null], step_errors:[null, sqlError(secretMessage, secretMessage)]})),
  ];
  for (const fetch of errors) {
    const db = createTursoDatabase(credentials, {fetch});
    await assert.rejects(() => db.prepare("SELECT ?1 private_value").bind("private_value").run(), error => {
      const serialized = `${String(error)} ${JSON.stringify(error)}`;
      for (const secret of [credentials.TURSO_AUTH_TOKEN, "password", "private_value", "db.turso.io"]) assert.ok(!serialized.includes(secret));
      assert.equal(error.cause, undefined);
      return error instanceof StorageSqlError;
    });
  }
});

test("HTTP auth, rate-limit and transient failures are classified, but writes are never automatically retried", async()=> {
  for (const status of [400, 401, 403, 408, 429, 500, 503, 302]) {
    let calls = 0;
    const db = createTursoDatabase(credentials, {fetch:async()=> {calls++; return new Response("body secret", {status});}});
    await assert.rejects(() => db.prepare("INSERT INTO t VALUES (1)").run(), error => {
      assert.equal(error.code, "storage_sql_http_error");
      assert.equal(error.status, status);
      assert.equal(error.retryable, status === 408 || status === 429 || status >= 500);
      assert.equal(error.outcomeUnknown, ![400, 401, 403].includes(status));
      return true;
    });
    assert.equal(calls, 1);
  }
  let calls = 0;
  const db = createTursoDatabase(credentials, {fetch:async()=> {calls++; throw new Error("socket reset");}});
  await rejectsCode(() => db.prepare("INSERT INTO t VALUES (1)").run(), "storage_sql_network_error");
  assert.equal(calls, 1);
});

test("timeout bounds fetch and body reads and aborts without retrying non-idempotent writes", async()=> {
  for (const hang of ["fetch", "body"]) {
    let calls = 0;
    let signal;
    const db = createTursoDatabase(credentials, {timeoutMs:10, fetch:async(_, options)=> {
      calls++;
      signal = options.signal;
      if (hang === "fetch") return new Promise(() => {});
      return new Response(new ReadableStream({start() {}, cancel() {}}));
    }});
    const start = performance.now();
    await assert.rejects(() => db.prepare("INSERT INTO t VALUES (1)").run(), error => {
      assert.equal(error.code, "storage_sql_timeout");
      assert.equal(error.outcomeUnknown, true);
      return true;
    });
    assert.ok(performance.now() - start < 1000);
    assert.equal(signal.aborted, true);
    assert.equal(calls, 1);
  }
});

test("strict malformed pipeline, statement values and metadata validation never reports success", async()=> {
  const valid = successfulSingle(rowResult());
  const variants = [null, {}, {results:[]}, {...valid, baton:"open-stream-secret"}, {...valid, results:valid.results.slice(0, 1)},
    {...valid, results:[{type:"ok", response:{type:"execute", result:rowResult()}}, ok("close")]},
    {...valid, results:[ok("batch", {step_results:[], step_errors:[]}), ok("close")]},
    {...valid, results:[ok("batch", {step_results:[emptyResult(), null], step_errors:[null, null]}), ok("close")]},
  ];
  for (const payload of variants) await rejectsCode(() => staticDb(payload).prepare("SELECT 1").all(), "storage_sql_invalid_response");
  for (const bad of [{cols:null}, {rows:null}, {cols:[{name:1}]}, {rows:[[]]}, {rows:[[{}]]},
    {rows:[[{type:"integer", value:"9007199254740993oops"}]]}, {rows:[[{type:"integer", value:"9223372036854775808"}]]},
    {rows:[[{type:"integer", value:1}]]}, {rows:[[{type:"float", value:"1"}]]}, {rows:[[{type:"text", value:1}]]},
    {rows:[[{type:"blob", base64:"##"}]]}, {rows:[[{type:"unknown", value:"secret"}]]},
    {affected_row_count:-1}, {affected_row_count:"1"}, {affected_row_count:9007199254740992},
    {rows_written:-1}, {rows_read:1.5}, {query_duration_ms:-1}, {last_insert_rowid:"oops"}]) {
    await rejectsCode(() => staticDb(successfulSingle({...rowResult(), ...bad})).prepare("SELECT 1").all(), "storage_sql_invalid_response");
  }
  const malformed = createTursoDatabase(credentials, {fetch:async()=>new Response("not-json")});
  await rejectsCode(() => malformed.prepare("SELECT 1").all(), "storage_sql_invalid_response");
  const invalidUtf8 = createTursoDatabase(credentials, {fetch:async()=>new Response(new Uint8Array([0xff, 0xfe]))});
  await rejectsCode(() => invalidUtf8.prepare("SELECT 1").all(), "storage_sql_invalid_response");
});

test("incomplete or contradictory batch results fail closed even if a server claims the transaction committed", async t => {
  const f = fixture(t);
  await f.db.exec("CREATE TABLE t(n)");
  for (const mutation of [
    response => response.results[0].response.result.step_results.pop(),
    response => response.results[0].response.result.step_results[2] = null,
    response => response.results[0].response.result.step_errors[2] = sqlError(),
    response => response.results[0].response.result.step_results[4] = emptyResult(),
  ]) {
    f.mutate = mutation;
    await assert.rejects(() => f.db.batch([f.db.prepare("INSERT INTO t VALUES (1)")]), StorageSqlError);
  }
});

test("request and response byte limits are bounded before unbounded allocations or repeated writes", async()=> {
  let calls = 0;
  const db = createTursoDatabase(credentials, {fetch:async()=> {calls++; return Response.json(successfulSingle(rowResult()));}});
  await rejectsCode(() => db.prepare("SELECT ?1").bind("x".repeat(16 * 1024 * 1024)).all(), "storage_sql_request_too_large");
  assert.equal(calls, 0);
  for (const response of [
    () => new Response("{}", {headers:{"Content-Length":String(16 * 1024 * 1024 + 1)}}),
    () => new Response("x".repeat(16 * 1024 * 1024 + 1)),
  ]) {
    const reader = createTursoDatabase(credentials, {fetch:async()=>response()});
    await rejectsCode(() => reader.prepare("SELECT 1").all(), "storage_sql_response_too_large");
  }
});
