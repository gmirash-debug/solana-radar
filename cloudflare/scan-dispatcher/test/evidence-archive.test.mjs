import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import test from "node:test";
import {gzipSync} from "node:zlib";
import {evidenceArchiveResponse} from "../src/evidence-archive.js";
import {readSqlRuntime} from "../src/runtime-sql.js";
import {guardR2Env} from "../src/r2-budget.js";
import worker from "../src/index.js";

const AT = "2026-10-04T12:00:00Z";
const hash = data => createHash("sha256").update(data).digest("hex");
const data = gzipSync(JSON.stringify({version:1, token_address:"token", pool_address:"pool",
  cohort_id:"cohort", signal_at:AT, evidence:{cohort:[{owner:"holder", balance:"42"}]}}));
const id = hash(data), key = `evidence/v1/sha256/${id.slice(0, 2)}/${id}.json.gz`;
const ref = {key, sha256:id, bytes:data.byteLength};
const endpoint = "https://worker/api/storage/evidence-archive";
const request = (method = "POST", body = data, extra = {}) => new Request(`${endpoint}?id=${id}`, {
  method, ...(method === "POST" ? {body} : {}), headers:{"x-radar-generated-at":AT, ...extra},
});

function fixture(t) {
  t.mock.method(globalThis, "fetch", () => { throw new Error("live network forbidden"); });
  const sql = new DatabaseSync(":memory:");
  t.after(() => sql.close());
  sql.exec(readFileSync(new URL("../migrations-storage/0002_runtime_sql.sql", import.meta.url), "utf8"));
  const state = {reads:0, inserts:0, pause:false, failRead:false, failAck:false, failInsert:false,
    uncertainInsert:false, failPut:false, headMismatch:false, getMismatch:false, beforePut:null};
  const db = {prepare(text) {
    let args = [];
    return {bind(...values) { args = values; return this; },
      async first() {
        state.reads++;
        if (state.failRead) throw new Error("private SQL error containing ingest-secret and SQL token");
        if (state.failAck && state.inserts) return null;
        return sql.prepare(text).get(...args) || null;
      },
      async run() {
        if (state.failInsert) throw new Error("private SQL write error containing ingest-secret");
        const result = sql.prepare(text).run(...args);
        state.inserts += Number(result.changes);
        if (state.uncertainInsert) throw new Error("SQL outcome unknown with ingest-secret");
        return {success:true, meta:{changes:Number(result.changes)}};
      }};
  }};
  const objects = new Map(), calls = [], reservations = [];
  const metadata = row => row ? {key:row.key, size:row.bytes.byteLength,
    customMetadata:row.meta, checksums:{sha256:Uint8Array.from(Buffer.from(hash(row.bytes), "hex")).buffer}} : null;
  const raw = {
    async head(objectKey) {
      calls.push("head");
      const result = metadata(objects.get(objectKey));
      return state.headMismatch && result ? {...result, size:result.size + 1} : result;
    },
    async put(objectKey, bytes, options) {
      calls.push("put");
      if (state.failPut) throw new Error("private R2 credentials in error");
      assert.equal(options.onlyIf.get("if-none-match"), "*");
      assert.equal(options.sha256, hash(bytes));
      assert.equal(options.storageClass, "Standard");
      await state.beforePut?.();
      if (objects.has(objectKey)) return null;
      objects.set(objectKey, {key:objectKey, bytes:Uint8Array.from(bytes), meta:options.customMetadata});
      return metadata(objects.get(objectKey));
    },
    async get(objectKey) {
      calls.push("get");
      const row = objects.get(objectKey);
      if (!row) return null;
      const result = {...metadata(row), body:new Blob([row.bytes]).stream()};
      return state.getMismatch ? {...result, customMetadata:{sha256:"0".repeat(64)}} : result;
    },
    list() { assert.fail("evidence must never list R2"); },
    delete() { assert.fail("evidence must never delete R2"); },
  };
  const env = {RADAR_DB:db, STORAGE_SQL_BACKEND:"turso", RUNTIME_STORAGE_BACKEND:"turso_sql",
    RADAR_ARCHIVE:raw, R2_BUDGET_GUARD:"enabled", R2_BUDGET:{idFromName:value => value,
      get:() => ({async fetch(budgetRequest) {
        const path = new URL(budgetRequest.url).pathname, body = await budgetRequest.json();
        reservations.push({path, ...body});
        if (state.pause) return Response.json({ok:true, allowed:false, error:"r2_monthly_budget_paused"});
        if (path === "/reserve") return Response.json({ok:true, allowed:true});
        assert.equal(path, "/reserve-batch");
        const counts = body.operations.reduce((value, op) => {
          value[["put", "list"].includes(op.kind) ? "class_a" : "class_b"]++;
          return value;
        }, {class_a:0, class_b:0});
        const now = Date.now();
        return Response.json({ok:true, allowed:true, month:new Date(now).toISOString().slice(0, 7),
          grant:{invocation_id:body.invocation_id, issued_at:now, expires_at:body.deadline,
            operations:body.operations.length, ...counts}});
      }})}};
  return {env, raw, sql, state, calls, reservations, objects,
    seedObject:() => objects.set(key, {key, bytes:Uint8Array.from(data), meta:{sha256:id}})};
}

test("a receipt requires a verified guarded R2 head and an acknowledged Turso manifest", async t => {
  const f = fixture(t);
  const result = await (await evidenceArchiveResponse(f.env, request())).json();
  assert.deepEqual(result, {ok:true, accepted:true, id, archive_ref:ref});
  assert.deepEqual(f.calls, ["head", "put", "head"]);
  assert.equal(f.reservations.length, 1);
  assert.deepEqual(f.reservations[0].operations, [{kind:"head", key},
    {kind:"put", key, bytes:ref.bytes}, {kind:"head", key}]);
  const saved = await readSqlRuntime(f.env, `evidence-archive:${id}`);
  assert.equal(saved.updated_at, AT);
  assert.deepEqual(saved.value, {version:1, archive_ref:ref, source_generated_at:AT});
  assert.equal(f.sql.prepare("SELECT COUNT(*) n FROM runtime_sql_documents WHERE name LIKE 'outbox:%'").get().n, 0);
});

test("a verified dedupe is SQL-only even while the R2 guard is paused", async t => {
  const f = fixture(t);
  await evidenceArchiveResponse(f.env, request());
  const before = {calls:f.calls.length, reservations:f.reservations.length, inserts:f.state.inserts};
  f.state.pause = true;
  const retry = await (await evidenceArchiveResponse(f.env, request("POST", data,
    {"x-radar-generated-at":"2026-10-05T12:00:00Z"}))).json();
  assert.deepEqual(retry, {ok:true, accepted:true, id, archive_ref:ref});
  assert.deepEqual({calls:f.calls.length, reservations:f.reservations.length, inserts:f.state.inserts}, before);
  assert.equal((await readSqlRuntime(f.env, `evidence-archive:${id}`)).value.source_generated_at, AT);
});

test("a manifest-backed retry still enforces body identity and compressed capacity without R2", async t => {
  const f = fixture(t);
  await evidenceArchiveResponse(f.env, request());
  const before = f.calls.length;
  f.state.pause = true;
  assert.equal((await evidenceArchiveResponse(f.env, request("POST", gzipSync("wrong snapshot")))).status, 400);
  assert.equal((await evidenceArchiveResponse(f.env, request("POST", data,
    {"content-length":String(16 * 1024 * 1024 + 1)}))).status, 413);
  assert.equal((await evidenceArchiveResponse(f.env, request("POST", Buffer.alloc(16 * 1024 * 1024 + 1)))).status, 413);
  assert.equal(f.calls.length, before);
  assert.equal(f.state.inserts, 1);
});

test("GET requires its manifest, is guarded and returns exactly the archived gzip", async t => {
  const f = fixture(t);
  f.seedObject();
  assert.equal((await evidenceArchiveResponse(f.env, request("GET"))).status, 404);
  assert.deepEqual(f.calls, []);
  await evidenceArchiveResponse(f.env, request());
  assert.deepEqual(f.calls, ["head"]);
  const reply = await evidenceArchiveResponse(guardR2Env(f.env), request("GET"));
  assert.equal(reply.headers.get("content-type"), "application/gzip");
  assert.equal(reply.headers.get("content-length"), String(data.byteLength));
  assert.equal(reply.headers.get("x-radar-sha256"), id);
  assert.equal(reply.headers.get("cache-control"), "private, no-store");
  assert.deepEqual(Buffer.from(await reply.arrayBuffer()), data);
  assert.equal(f.reservations.at(-1).kind, "get");
  f.state.pause = true;
  const before = f.calls.length;
  assert.equal((await evidenceArchiveResponse(f.env, request("GET"))).status, 503);
  assert.equal(f.calls.length, before);
});

test("missing or paused guard authority never permits native R2 I/O or publication", async t => {
  for (const missing of [false, true]) {
    const f = fixture(t);
    f.state.pause = true;
    if (missing) delete f.env.R2_BUDGET;
    const response = await evidenceArchiveResponse(f.env, request());
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {ok:false, error:"evidence_archive_unavailable"});
    assert.deepEqual(f.calls, []);
    assert.equal(f.state.inserts, 0);
  }
});

test("conditional PUT handles a competing writer without overwriting its object", async t => {
  const f = fixture(t);
  f.state.beforePut = () => f.seedObject();
  assert.equal((await evidenceArchiveResponse(f.env, request())).status, 200);
  assert.equal(f.objects.size, 1);
  assert.deepEqual(f.calls, ["head", "put", "head"]);
});

test("immutable manifest CAS cannot replace a concurrent mismatched receipt or acknowledge it", async t => {
  const f = fixture(t);
  const winner = {version:1, archive_ref:{...ref, bytes:ref.bytes + 1}, source_generated_at:AT};
  f.state.beforePut = () => {
    const payload = JSON.stringify(winner);
    f.sql.prepare(`INSERT INTO runtime_sql_documents
      (name,payload_json,payload_sha256,updated_at,source_ms,revision,bytes,touched_at)
      VALUES (?,?,?,?,?,0,?,?)`).run(`evidence-archive:${id}`, payload, hash(payload), AT,
        Date.parse(AT), Buffer.byteLength(payload), Date.now());
  };
  const result = await evidenceArchiveResponse(f.env, request());
  assert.equal(result.status, 503);
  assert.equal((await result.json()).error, "evidence_archive_manifest_not_acknowledged");
  assert.deepEqual((await readSqlRuntime(f.env, `evidence-archive:${id}`)).value, winner);
  assert.equal(f.state.inserts, 0);
  assert.deepEqual(Buffer.from(f.objects.get(key).bytes), data);
});

test("concurrent publications retain one immutable manifest and one content object", async t => {
  const f = fixture(t);
  const results = await Promise.all(Array.from({length:8}, (_, i) => evidenceArchiveResponse(f.env,
    request("POST", data, {"x-radar-generated-at":`2026-10-04T${12 + i}:00:00Z`}))));
  for (const result of results) assert.deepEqual(await result.json(), {ok:true, accepted:true, id, archive_ref:ref});
  assert.equal(f.state.inserts, 1);
  assert.equal(f.objects.size, 1);
  assert.match((await readSqlRuntime(f.env, `evidence-archive:${id}`)).value.source_generated_at,
    /^2026-10-04T1[2-9]:00:00Z$/);
});

test("no list, mutable progress method, malformed id or duplicate id can access storage", async t => {
  const f = fixture(t);
  for (const query of ["", "?id=short", `?id=${id.toUpperCase()}`, `?id=${id}%0A`,
    `?id=${id}&id=${id}`, "?id=../../private", `?key=${key}`, `?id=${id}0`]) {
    assert.equal((await evidenceArchiveResponse(f.env, new Request(endpoint + query))).status, 400);
  }
  for (const method of ["PATCH", "DELETE", "PUT", "HEAD"]) {
    assert.equal((await evidenceArchiveResponse(f.env, new Request(`${endpoint}?id=${id}`, {method}))).status, 405);
  }
  assert.equal(f.state.reads, 0);
  assert.deepEqual(f.calls, []);
});

test("a source timestamp must be explicit, zoned and not in the future", async t => {
  const f = fixture(t);
  for (const at of ["", "tomorrow", "2026-10-04T12:00:00", "2099-01-01T00:00:00Z"]) {
    assert.equal((await evidenceArchiveResponse(f.env, request("POST", data, {"x-radar-generated-at":at}))).status, 400);
  }
  assert.equal(f.state.reads, 0);
  assert.deepEqual(f.calls, []);
});

test("compressed limits apply to headers and streaming bodies, and mismatched lengths fail", async t => {
  const f = fixture(t), maximum = 16 * 1024 * 1024;
  for (const [body, length, status] of [[data, String(maximum + 1), 413],
    [Buffer.alloc(maximum + 1), null, 413], [data, "garbage", 400], [data, "-1", 400],
    [data, "1", 400], [null, null, 400]]) {
    const headers = {"x-radar-generated-at":AT, ...(length === null ? {} : {"content-length":length})};
    assert.equal((await evidenceArchiveResponse(f.env, new Request(`${endpoint}?id=${id}`,
      {method:"POST", body, headers}))).status, status);
  }
  assert.deepEqual(f.calls, []);
  assert.equal(f.state.inserts, 0);
});

test("the POST content id must hash its gzip body before any R2 reservation", async t => {
  const f = fixture(t);
  for (const bytes of [gzipSync("different evidence"), Buffer.from("not gzip"), Buffer.alloc(0)]) {
    assert.equal((await evidenceArchiveResponse(f.env, request("POST", bytes))).status, 400);
  }
  const raw = Buffer.from("not gzip"), rawId = hash(raw);
  assert.equal((await evidenceArchiveResponse(f.env, new Request(`${endpoint}?id=${rawId}`,
    {method:"POST", body:raw, headers:{"x-radar-generated-at":AT}}))).status, 400);
  assert.equal(f.reservations.length, 0);
});

test("unverified heads and native write failures never acknowledge a manifest or delete an object", async t => {
  for (const failure of ["headMismatch", "failPut"]) {
    const f = fixture(t);
    f.state[failure] = true;
    assert.equal((await evidenceArchiveResponse(f.env, request())).status, 503);
    assert.equal(f.state.inserts, 0);
    if (failure === "headMismatch") assert.equal(f.objects.size, 1);
  }
  const f = fixture(t);
  f.seedObject();
  f.objects.get(key).meta = {sha256:"0".repeat(64)};
  assert.equal((await evidenceArchiveResponse(f.env, request())).status, 503);
  assert.deepEqual(f.calls, ["head"]);
  assert.equal(f.objects.size, 1);
  assert.equal(f.state.inserts, 0);
});

test("SQL write and acknowledgement failures retain the R2 original and sanitize private errors", async t => {
  for (const failure of ["failInsert", "failAck"]) {
    const f = fixture(t);
    f.state[failure] = true;
    const result = await evidenceArchiveResponse(f.env, request());
    assert.equal(result.status, 503);
    const body = await result.text();
    assert.equal(JSON.parse(body).ok, false);
    assert.doesNotMatch(body, /ingest-secret|credentials|private SQL/);
    assert.equal(f.objects.size, 1);
  }
});

test("retry after uncertain SQL commit is manifest-only; retry after failed SQL insert reuses its head", async t => {
  for (const failure of ["uncertainInsert", "failInsert"]) {
    const f = fixture(t);
    f.state[failure] = true;
    assert.equal((await evidenceArchiveResponse(f.env, request())).status, 503);
    const before = f.calls.length;
    f.state[failure] = false;
    assert.equal((await evidenceArchiveResponse(f.env, request())).status, 200);
    assert.deepEqual(f.calls.slice(before), failure === "uncertainInsert" ? [] : ["head"]);
    assert.equal(f.calls.filter(value => value === "put").length, 1);
  }
});

test("corrupt manifests cannot authorize either a receipt or a read at another path", async t => {
  const f = fixture(t);
  await evidenceArchiveResponse(f.env, request());
  for (const changes of [{key:`outbox/v1/sha256/${id.slice(0, 2)}/${id}.json.gz`},
    {key:key.replace(id.slice(0, 2), "00")}, {sha256:id + "\n"}, {bytes:0}, {bytes:16 * 1024 * 1024 + 1}]) {
    f.sql.prepare("UPDATE runtime_sql_documents SET payload_json=? WHERE name=?").run(
      JSON.stringify({version:1, archive_ref:{...ref, ...changes}, source_generated_at:AT}), `evidence-archive:${id}`);
    const before = f.calls.length;
    for (const method of ["GET", "POST"]) assert.equal((await evidenceArchiveResponse(f.env, request(method))).status, 503);
    assert.equal(f.calls.length, before);
  }
});

test("GET validates object size, digest metadata, key and optional provider checksum", async t => {
  const f = fixture(t);
  await evidenceArchiveResponse(f.env, request());
  const get = f.raw.get;
  for (const change of [{size:ref.bytes + 1}, {customMetadata:{sha256:"0".repeat(64)}},
    {key:"private/other"}, {checksums:{sha256:new Uint8Array(32).buffer}}, {body:null}]) {
    f.raw.get = async objectKey => ({...await get(objectKey), ...change});
    assert.equal((await evidenceArchiveResponse(f.env, request("GET"))).status, 503);
  }
  f.raw.get = async () => null;
  assert.equal((await evidenceArchiveResponse(f.env, request("GET"))).status, 503);
  assert.equal(f.objects.size, 1);
});

const routeEnv = () => ({STORAGE_EPOCH:"test-generation", RADAR_INGEST_SECRET:"ingest-secret",
  RADAR_INGEST_SECRET_NEXT:"next-secret", RUNTIME_STORAGE_BACKEND:"turso_sql",
  STORAGE_SQL_BACKEND:"turso", TURSO_DATABASE_URL:"https://database.example.invalid",
  TURSO_AUTH_TOKEN:"private-sql-token", RADAR_ARCHIVE:{}});

test("the index route authenticates both methods and fences reads as well as writes", async t => {
  t.mock.method(globalThis, "fetch", () => { assert.fail("route fences must precede SQL and R2"); });
  const env = routeEnv();
  for (const method of ["GET", "POST"]) {
    const routeRequest = headers => new Request(endpoint + "?id=invalid", {method, headers});
    assert.equal((await worker.fetch(routeRequest({"x-radar-storage-epoch":env.STORAGE_EPOCH}), env)).status, 401);
    for (const epoch of ["", "stale"]) {
      assert.equal((await worker.fetch(routeRequest({"x-radar-ingest-secret":"ingest-secret",
        "x-radar-storage-epoch":epoch}), env)).status, 409);
    }
    for (const secret of ["ingest-secret", "next-secret"]) {
      assert.equal((await worker.fetch(routeRequest({"x-radar-ingest-secret":secret,
        "x-radar-storage-epoch":env.STORAGE_EPOCH}), env)).status, 400);
    }
  }
  assert.equal((await worker.fetch(request(), {...env, STORAGE_WRITES_FROZEN:"true"})).status, 503);
});

test("index storage failures never expose SQL errors, credentials or response details", async t => {
  t.mock.method(globalThis, "fetch", () => { throw new Error("private-sql-token ingest-secret detailed error"); });
  const env = routeEnv();
  const result = await worker.fetch(request("GET", null, {"x-radar-ingest-secret":"ingest-secret",
    "x-radar-storage-epoch":env.STORAGE_EPOCH}), env);
  assert.equal(result.status, 503);
  assert.deepEqual(await result.json(), {ok:false, error:"evidence_archive_unavailable"});
});
