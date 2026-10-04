import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.js";

test("cutover freeze stops all cron work and mutations but keeps reads available", async () => {
  const tasks=[];
  const env={STORAGE_WRITES_FROZEN:"true",SCHEDULER_ENABLED:"auto",RADAR_INGEST_SECRET:"secret"};
  for (const cron of ["*/5 * * * *", "7 * * * *", "37 * * * *", "22,37,52 * * * *", "47 * * * *"]) {
    await worker.scheduled({cron},env,{waitUntil:task=>tasks.push(task)});
  }
  assert.equal(tasks.length,0);
  for (const path of ["/dispatch", "/deleted-token", "/api/ingest", "/api/runtime/history/flush", "/api/storage/maintenance"]) {
    const response=await worker.fetch(new Request(`https://worker.example${path}`,{method:"POST"}),env,{});
    assert.equal(response.status,503);
    assert.equal(response.headers.get("retry-after"),"120");
    assert.equal((await response.json()).error,"storage_cutover_writes_frozen");
  }
  const response=await worker.fetch(new Request("https://worker.example/health"),env,{});
  assert.equal(response.status,200);
  assert.equal((await response.json()).storage_writes_frozen,true);
  const checkpoint=await worker.fetch(new Request("https://worker.example/api/runtime/checkpoint?kind=invalid",{
    headers:{"x-radar-ingest-secret":"secret"},
  }),env,{});
  assert.equal(checkpoint.status,400);
  const read=await worker.fetch(new Request("https://worker.example/api/storage/archive/read",{
    method:"POST",headers:{"x-radar-ingest-secret":"secret"},body:"{}",
  }),env,{});
  assert.notEqual((await read.json()).error,"storage_cutover_writes_frozen");
});

test("legacy migration is authenticated and requires the operational write freeze", async () => {
  const request = authorized => new Request("https://worker.example/api/runtime/history/migrate-legacy", {
    method:"POST",headers:authorized ? {"x-radar-ingest-secret":"secret"} : {},body:"{}",
  });
  const env = {RADAR_INGEST_SECRET:"secret",STORAGE_WRITES_FROZEN:"true"};
  assert.equal((await worker.fetch(request(false),env,{})).status,401);
  const response = await worker.fetch(request(true),{...env,STORAGE_WRITES_FROZEN:"false"},{});
  assert.equal(response.status,409);
  assert.equal((await response.json()).error,"history_legacy_migration_requires_write_freeze");
});

test("maintenance-only cron cannot dispatch a scan or consume the live queue", async () => {
  const tasks=[];
  await worker.scheduled({cron:"47 * * * *"}, {SCHEDULER_ENABLED:"auto"}, {waitUntil:task=>tasks.push(task)});
  await Promise.all(tasks);
  assert.equal(tasks.length,1);
});

test("archive cron is isolated from frequent discovery and deep scans", async () => {
  assert.equal(shouldFlushScheduledHistory({cron:"*/5 * * * *"}, {}), false);
  assert.equal(shouldFlushScheduledHistory({cron:"7 * * * *"}, {}), false);
  assert.equal(shouldFlushScheduledHistory({cron:"37 * * * *"}, {}), true);
  assert.equal(shouldFlushScheduledHistory({cron:"37 * * * *"}, {HISTORY_FLUSH_CRON:"37 */6 * * *"}), false);
  const tasks=[];
  // No GitHub credentials: an accidental scan dispatch would reject.
  await worker.scheduled({cron:"37 * * * *"}, {SCHEDULER_ENABLED:"auto"}, {waitUntil:task=>tasks.push(task)});
  await Promise.all(tasks);
});

import {
  applyDeletedTokenUpdate,
  dashboardTokenDetail,
  claimDispatchBucket,
  corsHeaders,
  compactDashboardReport,
  ingestDashboardSnapshot,
  ingestSnapshotDetails,
  discoveryStateForTokens,
  discoveryDispatchGuard,
  shouldFlushScheduledHistory,
  dashboardTokenKeys,
  decodeCursor,
  encodeCursor,
  isCurrentDashboardSignal,
  requireCloudflareAccess,
  scanStatusPayload,
  schedulerBucket,
  schedulerEnabled,
  schedulerMode,
  schedulerKindForCron,
  githubActionsStatus,
  updateDeletedToken,
} from "../src/index.js";
import {
  ageBand,
  historyEventEffects,
  historyEventId,
  historyEventsFromPayload,
  mcapBand,
  normalizedOutcome,
  flushHistoryOutbox,
} from "../src/history.js";

function recordingDb() {
  const writes = [];
  return {
    writes,
    prepare(sql) {
      const statement = {
        values: [],
        bind(...values) { statement.values = values; return statement; },
        async run() { writes.push({sql, values: statement.values}); return {success:true, meta:{changes:1}}; },
        async all() { return {results:[]}; },
        async first() { return null; },
      };
      return statement;
    },
    async batch(statements) { return Promise.all(statements.map(statement => statement.run())); },
  };
}

test("chunked publication removes wallet detail before D1 row limit and defers expensive writes", async () => {
  const db = recordingDb();
  const result = await ingestDashboardSnapshot({RADAR_DB:db}, {
    chunked:true,
    report:{generated_at:new Date().toISOString(), signal_theses:[{
      token_address:"token-a", signal_at:new Date().toISOString(), cohort_wallets:[{noise:"x".repeat(2_000_000)}],
    }]},
    deleted_tokens:{tokens:Array.from({length:100}, (_,i)=>`deleted-${i}`)},
  });
  assert.equal(result.ok, true);
  assert.equal(result.evidence_pending, true);
  assert.equal(db.writes.length, 3);
  const latest = JSON.parse(db.writes.find(row=>row.values[0] === "latest_report").values[1]);
  assert.equal(latest.signal_theses[0].cohort_wallets, undefined);
  assert.ok(JSON.stringify(latest).length < 1000);
});

test("detail batches reject oversize input before writing and preserve source-time conflict guards", async () => {
  const db = recordingDb();
  const generated_at = "2026-10-03T00:00:00Z";
  await assert.rejects(ingestSnapshotDetails({RADAR_DB:db}, {generated_at,
    detail_signal_theses:Array.from({length:26}, (_,i)=>({token_address:String(i)})),
  }), /25_rows/);
  assert.equal(db.writes.length, 0);
  await assert.rejects(ingestSnapshotDetails({RADAR_DB:db}, {}), /generated_at_required/);
  await ingestSnapshotDetails({RADAR_DB:db}, {generated_at,
    detail_current_alerts:[{pool:{token_address:"a"},created_at:generated_at}],
    detail_signal_theses:[{token_address:"a",signal_at:generated_at}],
  });
  assert.ok(db.writes.find(row=>row.sql.includes("INSERT INTO alerts")).sql.includes("WHERE excluded.updated_at >= alerts.updated_at"));
  assert.ok(db.writes.find(row=>row.sql.includes("INSERT INTO state_docs")).sql.includes("WHERE excluded.source_updated_at >= state_docs.source_updated_at"));
  assert.equal(db.writes.find(row=>row.sql.includes("INSERT INTO alerts")).values.at(-1), generated_at);
});

test("new evidence ingestion route requires the server ingest secret", async () => {
  const response = await worker.fetch(new Request("https://worker.example/api/ingest/details", {
    method:"POST",body:JSON.stringify({generated_at:"2026-10-03T00:00:00Z"}),
  }), {RADAR_INGEST_SECRET:"test-secret",RADAR_DB:recordingDb()}, {});
  assert.equal(response.status, 401);
});

test("native RPC ledger is a private server endpoint",async()=>{
  const response=await worker.fetch(new Request("https://worker.example/api/runtime/rpc-ledger"),{RADAR_INGEST_SECRET:"secret"},{});
  assert.equal(response.status,401);
});

test("storage endpoints require authorization and reject unbounded or malformed JSON", async () => {
  const env = {RADAR_INGEST_SECRET:"secret", HISTORY_ARCHIVE_MODE:"r2"};
  const call = (path, body, authorized=true) => worker.fetch(new Request(`https://worker.example${path}`, {
    method:"POST", headers:authorized ? {"x-radar-ingest-secret":"secret"} : {}, body,
  }),env,{});
  assert.equal((await call("/api/storage/archive", "{}", false)).status,401);
  assert.equal((await call("/api/storage/archive", "x".repeat(151*1024))).status,413);
  assert.equal((await call("/api/storage/archive/read", "x".repeat(4097))).status,413);
  assert.equal((await call("/api/storage/archive/read", "{" )).status,400);
});

test("overlapping ingest rotation accepts old and next keys, never arbitrary or empty keys", async () => {
  const env={RADAR_INGEST_SECRET:"old",RADAR_INGEST_SECRET_NEXT:"next"};
  const call=key=>worker.fetch(new Request("https://worker.example/api/runtime/checkpoint?kind=invalid", {
    headers:{"x-radar-ingest-secret":key},
  }),env,{});
  assert.equal((await call("old")).status,400);
  assert.equal((await call("next")).status,400);
  assert.equal((await call("bad")).status,401);
  assert.equal((await call("")).status,401);
});

test("dashboard part metadata probe remains private with explicit method and backend boundaries", async () => {
  const env={RADAR_INGEST_SECRET:"secret"};
  const call=(authorized,method="POST")=>worker.fetch(new Request("https://worker.example/api/runtime/dashboard-parts",{
    method,headers:authorized ? {"x-radar-ingest-secret":"secret"} : {},
    ...(method==="POST" ? {body:JSON.stringify({ids:[]})} : {}),
  }),env,{});
  assert.equal((await call(false)).status,401);
  assert.equal((await call(true,"GET")).status,405);
  assert.equal((await call(true)).status,503);
});

test("identical state and thesis replays avoid unnecessary SQL row writes", async () => {
  const db = recordingDb(), generated_at="2026-10-03T00:00:00Z";
  await ingestSnapshotDetails({RADAR_DB:db}, {generated_at,
    detail_signal_theses:[{token_address:"a",signal_at:generated_at}],
  });
  const query = db.writes.find(row=>row.sql.includes("INSERT INTO state_docs")).sql;
  assert.ok(query.includes("state_docs.payload_json IS NOT excluded.payload_json"));
  assert.ok(query.includes("state_docs.source_updated_at IS NOT excluded.source_updated_at"));
});

function githubContent(data, sha) {
  return new Response(JSON.stringify({
    content: btoa(JSON.stringify(data)),
    sha,
  }), { status: 200 });
}

test("deleted-token mutation keeps both token and pool blacklist entries", () => {
  const result = applyDeletedTokenUpdate(
    { tokens: ["other-token"], pools: [], entries: {} },
    {
      action: "delete",
      token_address: "token-a",
      pool_address: "pool-a",
      symbol: "TEST",
    },
    "2026-07-30T12:00:00Z",
  );

  assert.equal(result.ok, true);
  assert.deepEqual(result.data.tokens, ["other-token", "token-a"]);
  assert.deepEqual(result.data.pools, ["pool-a"]);
  assert.equal(result.data.entries["token-a"].deleted_at, "2026-07-30T12:00:00Z");
});

test("I09: mint restore removes associated old pools, without restoring unrelated identities", () => {
  const source={tokens:["mint","other"],pools:["old","shared","other-pool"],entries:{
    mint:{token_address:"mint",pool_address:"old"},
    another:{token_address:"other",pool_address:"other-pool"},
    poolOnly:{token_address:null,pool_address:"shared"},
  }};
  for (const payload of [{token_address:"mint"},{token_address:"mint",pool_address:"new"}]) {
    const result=applyDeletedTokenUpdate(source,{...payload,action:"restore"},"2026-10-03T12:00:00Z");
    assert.deepEqual(result.data.tokens,["other"]);
    assert.deepEqual(result.data.pools,["other-pool","shared"]);
    assert.ok(result.data.entries.another);
    assert.ok(result.data.entries.poolOnly);
  }
});

test("I04: D1 details require both generation and frozen signal-window identity", async () => {
  const generation="2026-10-03T12:00:00Z";
  const summary={token_address:"mint",cohort_id:"cohort",signal_at:"2026-10-02T12:00:00Z",
    signal_window_start:"2026-10-02T11:00:00Z",signal_window_end:"2026-10-02T12:00:00Z",
    last_checked_at:generation,updated_at:generation};
  let detail={...summary,cohort_wallets:[{owner:"wallet"}]};
  let source="2026-10-02T12:00:00Z";
  const db={prepare(sql) {return {bind(){return this;},async all(){return {results:[]};},async first(){
    if (sql.includes("'latest_report'")) return {payload_json:JSON.stringify({generated_at:generation,signal_theses:[summary]}),source_updated_at:generation};
    if (sql.includes("state_docs")) return {payload_json:JSON.stringify(detail),source_updated_at:source};
    return null;
  }};}};
  const old=await dashboardTokenDetail({RADAR_DB:db},"mint");
  assert.equal(old.detail_status,"pending");
  assert.equal(old.thesis.cohort_wallets,undefined);
  source=generation; detail={...detail,signal_window_end:"different"};
  assert.equal((await dashboardTokenDetail({RADAR_DB:db},"mint")).detail_status,"pending");
  detail={...detail,signal_window_end:summary.signal_window_end};
  const matched=await dashboardTokenDetail({RADAR_DB:db},"mint");
  assert.equal(matched.detail_status,"ready");
  assert.equal(matched.thesis.cohort_wallets[0].owner,"wallet");
});

test("I02: scheduler publishes fresh queue failure, counters and backlog age after a rejected flush", async () => {
  const writes=[];
  const env={SCHEDULER_ENABLED:"disabled",RADAR_HISTORY_DB:{prepare(){}},
    HISTORY_QUEUE:{idFromName:name=>name,get(){return {async fetch(request){
      if (new URL(request.url).pathname==="/flush") return Response.json({ok:false,error:"history_daily_write_budget"},{status:429});
      return Response.json({ok:true,pending:1146,pending_bytes:13584725,oldest_pending_age_seconds:86400,history_write_units:80000});
    }};}},
    RUNTIME_SNAPSHOTS:{idFromName:name=>name,get(){return {async fetch(request){writes.push(await request.json());return Response.json({ok:true});}};}},
  };
  const tasks=[];
  await worker.scheduled({cron:"*/5 * * * *"},env,{waitUntil:promise=>tasks.push(promise)});
  await Promise.all(tasks);
  assert.equal(writes.length,1);
  const status=writes[0].value;
  assert.equal(status.pending,1146);
  assert.equal(status.oldest_pending_age_seconds,86400);
  assert.equal(status.healthy,false);
  assert.equal(status.last_flush_error,"history_daily_write_budget");
  assert.ok(Date.parse(status.checked_at));
});

test("manual drain endpoint keeps ingest authentication and method boundaries", async () => {
  const env={RADAR_INGEST_SECRET:"secret"};
  const path="https://worker/api/runtime/history/flush";
  assert.equal((await worker.fetch(new Request(path,{method:"POST"}),env,{})).status,401);
  assert.equal((await worker.fetch(new Request(path,{headers:{"x-radar-ingest-secret":"secret"}}),env,{})).status,405);
});

test("deleted-token write retries a SHA conflict without losing concurrent deletion", async () => {
  const originalFetch = globalThis.fetch;
  let readCount = 0;
  let writeCount = 0;
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === "GET") {
      readCount += 1;
      return readCount === 1
        ? githubContent({ tokens: ["other-token"], pools: [], entries: {} }, "sha-1")
        : githubContent({ tokens: ["other-token", "concurrent-token"], pools: [], entries: {} }, "sha-2");
    }
    if (options.method === "PUT") {
      writeCount += 1;
      if (writeCount === 1) return new Response(JSON.stringify({ message: "sha conflict" }), { status: 409 });
      return new Response(JSON.stringify({ commit: { sha: "commit-2" } }), { status: 200 });
    }
    throw new Error(`Unexpected request: ${options.method}`);
  };
  try {
    const result = await updateDeletedToken(
      { GITHUB_TOKEN: "test-token" },
      { action: "delete", token_address: "token-a", pool_address: "pool-a" },
    );
    assert.equal(result.ok, true);
    assert.equal(result.commit_sha, "commit-2");
    assert.deepEqual(result.deleted_tokens.tokens, ["concurrent-token", "other-token", "token-a"]);
    assert.equal(readCount, 2);
    assert.equal(writeCount, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CORS accepts only the configured dashboard origin", () => {
  const env = { ALLOWED_ORIGIN: "https://gmirash-debug.github.io" };
  const accepted = corsHeaders(
    new Request("https://worker.example/deleted-token", { headers: { origin: env.ALLOWED_ORIGIN } }),
    env,
  );
  const rejected = corsHeaders(
    new Request("https://worker.example/deleted-token", { headers: { origin: "https://attacker.example" } }),
    env,
  );
  assert.equal(accepted["access-control-allow-origin"], env.ALLOWED_ORIGIN);
  assert.deepEqual(rejected, {});
});

test("delete access rejects unconfigured and unauthenticated requests", async () => {
  const request = new Request("https://worker.example/deleted-token", { method: "POST" });
  assert.deepEqual(
    await requireCloudflareAccess(request, {}),
    { ok: false, status: 503, error: "delete_access_not_configured" },
  );
  assert.deepEqual(
    await requireCloudflareAccess(request, {
      CLOUDFLARE_ACCESS_AUD: "audience",
      CLOUDFLARE_ACCESS_TEAM_DOMAIN: "team.cloudflareaccess.com",
    }),
    { ok: false, status: 401, error: "Cloudflare Access login required" },
  );
});

test("scheduler buckets distinguish five-minute discovery from hourly deep scans", async () => {
  const now = new Date("2026-07-31T12:07:41.000Z");
  assert.equal(schedulerKindForCron("*/5 * * * *"), "discovery");
  assert.equal(schedulerKindForCron("7 * * * *"), "deep_scan");
  assert.equal(schedulerBucket("discovery", now), "discovery:2026-07-31T12:05:00.000Z");
  assert.equal(schedulerBucket("deep_scan", now), "deep_scan:2026-07-31T12:00:00.000Z");

  const entries = new Set();
  const env = {
    DISPATCH_BUCKETS: {
      idFromName: (key) => key,
      get: (key) => ({
        fetch: async () => {
          const claimed = !entries.has(key);
          entries.add(key);
          return new Response(JSON.stringify({ claimed }));
        },
      }),
    },
  };
  assert.deepEqual(await claimDispatchBucket(env, "discovery", "discovery:bucket"), { claimed: true, persistent: true });
  assert.deepEqual(await claimDispatchBucket(env, "discovery", "discovery:bucket"), { claimed: false, persistent: true });
});

test("scheduler can be paused without removing its cron triggers", () => {
  assert.equal(schedulerEnabled({}), true);
  assert.equal(schedulerEnabled({ SCHEDULER_ENABLED: "true" }), true);
  assert.equal(schedulerEnabled({ SCHEDULER_ENABLED: "false" }), false);
  assert.equal(schedulerEnabled({ SCHEDULER_ENABLED: "off" }), false);
  assert.equal(schedulerMode({ SCHEDULER_ENABLED: "auto" }), "auto");
  assert.equal(schedulerMode({ SCHEDULER_ENABLED: "false" }), "disabled");
});

test("discovery never replaces a queued deep scan and ignores independent UI publication", async () => {
  const original = globalThis.fetch;
  try {
    for (const status of ["queued", "pending", "in_progress", "waiting", "requested"]) {
      globalThis.fetch = async () => new Response(JSON.stringify({workflow_runs:[
        {id:1,event:"push",status:"in_progress"}, {id:2,event:"workflow_dispatch",status},
      ]}));
      assert.deepEqual(await discoveryDispatchGuard({GITHUB_TOKEN:"test"}), {skipped:"deep_scan_has_priority",deep_run_id:2});
    }
    globalThis.fetch = async () => new Response(JSON.stringify({workflow_runs:[
      {id:1,event:"push",status:"in_progress"}, {id:2,event:"workflow_dispatch",status:"completed"},
    ]}));
    assert.deepEqual(await discoveryDispatchGuard({GITHUB_TOKEN:"test"}), {});
    globalThis.fetch = async () => new Response("quota", {status:429});
    assert.equal((await discoveryDispatchGuard({GITHUB_TOKEN:"test"})).skipped, "scan_queue_status_unavailable");
  } finally { globalThis.fetch = original; }
});

test("auto scheduler dispatches only when GitHub Actions is operational", () => {
  assert.equal(githubActionsStatus({ components: [{ name: "Actions", status: "operational" }] }), "operational");
  assert.equal(githubActionsStatus({ components: [{ name: "Actions", status: "major_outage" }] }), "major_outage");
  assert.equal(githubActionsStatus({ components: [{ name: "Pages", status: "operational" }] }), null);
});

test("D1 dashboard selects only token rows that can appear in the UI", () => {
  const keys = dashboardTokenKeys(
    {
      signal_theses: [{ token_address: "thesis-token" }],
      alerts: [{ pool: { token_address: "alert-token" } }],
      summaries: [{ pool: { token_address: "summary-token" } }],
    },
    [{ pool: { token_address: "history-token" } }],
  );
  assert.deepEqual(keys, ["thesis-token", "alert-token", "history-token", "summary-token"]);
});

test("public dashboard payload excludes per-wallet event detail", () => {
  const compact = compactDashboardReport({
    alerts: [{
      pool: { token_address: "token-a" },
      created_at: "2026-08-13T02:00:00Z",
      events: [{ signature: "private-event" }],
      common_funders: [{ source: "private-funder" }],
      wave: { net_buy_sol: 10, top_buyers: [{ owner: "wallet-a" }] },
      supply_integrity: {
        status: "watch",
        top_owners: [{ owner: "private-holder" }],
        linkage_groups: [{ members: ["private-wallet-a", "private-wallet-b"] }],
      },
    }],
    signal_theses: [{
      token_address: "token-a",
      signal_at: "2026-08-13T02:00:00Z",
      cohort_wallets: [{ owner: "wallet-a" }],
      source_score: 80,
      supply_integrity: {
        status: "watch",
        top_owners: [{ owner: "private-holder" }],
      },
      supply_integrity_history: [{ checked_at: "2026-08-13T02:00:00Z" }],
    }],
  });
  assert.equal(compact.alerts[0].events, undefined);
  assert.equal(compact.alerts[0].events_count, 1);
  assert.equal(compact.alerts[0].common_funders, undefined);
  assert.equal(compact.alerts[0].wave.top_buyers, undefined);
  assert.equal(compact.alerts[0].wave.top_buyers_count, 1);
  assert.equal(compact.alerts[0].supply_integrity.status, "watch");
  assert.equal(compact.alerts[0].supply_integrity.top_owners, undefined);
  assert.equal(compact.signal_theses[0].cohort_wallets, undefined);
  assert.equal(compact.signal_theses[0].supply_integrity.top_owners, undefined);
  assert.equal(compact.signal_theses[0].supply_integrity_history, undefined);
  assert.equal(compact.signal_theses[0].source_score, 80);
});

test("coordination summary strips frozen inputs and addresses without dropping risk metrics", () => {
  const evidence = {status:"pattern", metrics:{material_union_held_supply_pct:3},
    signals:[{code:"common_direct_funding", members:["private-wallet"], detail:{source:"private-funder", transfer_verified:true}}]};
  const input = {alerts:[{pool:{token_address:"token-a"}, created_at:"2026-10-01T12:00:00Z",
                         coordination_events:[{signature:"private-tx"}], coordinated_activity:evidence}],
                 signal_theses:[{token_address:"token-a", signal_at:"2026-10-01T12:00:00Z",
                                 coordination_inputs:{buys:[{owner:"private-wallet"}]}, coordinated_activity:evidence}]};
  const before = structuredClone(input);
  const compact = compactDashboardReport(input);
  assert.equal(compact.alerts[0].coordinated_activity.metrics.material_union_held_supply_pct, 3);
  assert.equal(compact.signal_theses[0].coordinated_activity.status, "pattern");
  assert.equal(compact.alerts[0].coordination_events, undefined);
  assert.equal(compact.signal_theses[0].coordination_inputs, undefined);
  for (const row of [compact.alerts[0], compact.signal_theses[0]]) {
    assert.equal(row.coordinated_activity.signals[0].members, undefined);
    assert.equal(row.coordinated_activity.signals[0].detail.source, undefined);
  }
  assert.deepEqual(input, before);
});

test("dashboard excludes records from the previous scanner filter era", () => {
  const report = {
    alerts: [
      { pool: { token_address: "old-alert" }, created_at: "2026-08-13T01:00:59Z" },
      { pool: { token_address: "new-alert" }, created_at: "2026-08-13T01:01:00Z" },
      {
        pool: {
          token_address: "old-monitor-alert",
          source: "signal_thesis_monitor",
        },
        created_at: "2026-08-13T02:00:00Z",
      },
    ],
    signal_theses: [
      { token_address: "old-thesis", signal_at: "2026-08-12T12:00:00Z" },
      { token_address: "new-thesis", signal_at: "2026-08-13T02:00:00Z" },
    ],
    summaries: [
      {
        pool: {
          token_address: "old-monitor-alert",
          market_source: "signal_thesis_monitor",
          first_signal_at: "2026-08-02T19:46:56Z",
        },
      },
      { pool: { token_address: "current-summary" } },
    ],
  };
  const compact = compactDashboardReport(report);

  assert.deepEqual(compact.alerts.map((item) => item.pool.token_address), ["new-alert"]);
  assert.deepEqual(compact.signal_theses.map((item) => item.token_address), ["new-thesis"]);
  assert.deepEqual(compact.summaries.map((item) => item.pool.token_address), ["current-summary"]);
  assert.equal(isCurrentDashboardSignal({}, report), false);
  assert.equal(
    isCurrentDashboardSignal(
      {
        pool: {
          token_address: "old-monitor-alert",
          source: "signal_thesis_monitor",
        },
        created_at: "2026-08-13T02:00:00Z",
      },
      report,
    ),
    false,
  );
  assert.equal(
    isCurrentDashboardSignal(
      { created_at: "2026-08-12T00:00:00Z" },
      { config: { dashboard_signal_epoch: "2026-08-12T00:00:00Z" } },
    ),
    true,
  );
});

test("dashboard excludes out-of-window pools from all operational payloads", () => {
  const report = {
    config: {
      dashboard_signal_epoch: "2026-08-13T01:01:00Z",
      age_min_hours: 24,
      age_max_hours: 360,
    },
    alerts: [
      {
        created_at: "2026-08-13T02:00:00Z",
        pool: { token_address: "old-alert", age_hours: 361 },
      },
      {
        created_at: "2026-08-13T02:00:00Z",
        pool: { token_address: "current-alert", age_hours: 72 },
      },
    ],
    summaries: [
      { pool: { token_address: "old-summary", age_hours: 361 } },
      { pool: { token_address: "current-summary", age_hours: 72 } },
    ],
  };

  const compact = compactDashboardReport(report);

  assert.deepEqual(compact.alerts.map((item) => item.pool.token_address), ["current-alert"]);
  assert.deepEqual(compact.summaries.map((item) => item.pool.token_address), ["current-summary"]);
  assert.equal(isCurrentDashboardSignal(report.alerts[0], report), false);
});

test("dashboard shaping honors the thirty-minute minimum for alerts and summaries", () => {
  const ages = [0.5 - 1 / 3600, 0.5, 23, 24, 360, 360 + 1 / 3600];
  const report = {
    config: { age_min_hours: 0.5, age_max_hours: 360 },
    alerts: ages.map((age_hours, index) => ({
      created_at: "2026-10-03T02:00:00Z",
      pool: { token_address: `token-${index}`, age_hours },
    })),
    summaries: ages.map((age_hours, index) => ({
      pool: { token_address: `token-${index}`, age_hours },
    })),
  };
  const compact = compactDashboardReport(report);
  const expected = ["token-1", "token-2", "token-3", "token-4"];
  assert.deepEqual(compact.alerts.map(item => item.pool.token_address), expected);
  assert.deepEqual(compact.summaries.map(item => item.pool.token_address), expected);
  assert.equal(compact.config.age_min_hours, 0.5);
  assert.equal(isCurrentDashboardSignal({ created_at: "2026-10-03T02:00:00Z", pool: {} }, report), false);
});

test("D1 dashboard chunks market lookups below the SQLite parameter limit", async () => {
  const bindings = [];
  const db = {
    prepare: () => ({
      bind: (...keys) => ({
        all: async () => {
          bindings.push(keys);
          return { results: keys.map((token_key) => ({ token_key })) };
        },
      }),
    }),
  };
  const tokenKeys = Array.from({ length: 201 }, (_, index) => `token-${index}`);

  const rows = await discoveryStateForTokens(db, tokenKeys);

  assert.deepEqual(bindings.map((keys) => keys.length), [100, 100, 1]);
  assert.equal(rows.length, 201);
  assert.equal(rows[0].token_key, "token-0");
  assert.equal(rows.at(-1).token_key, "token-200");
});

test("D1 discovery cursor round-trips padded and unpadded base64url", () => {
  const source = { updatedAt: "2026-07-31T12:00:00Z", tokenKey: "token-key" };
  const cursor = encodeCursor(source);
  assert.deepEqual(decodeCursor(cursor), source);
});

test("scan status ingestion accepts scanner wrappers and raw recovery documents", () => {
  const status = { status: "failed", last_attempt_at: "2026-07-31T12:00:00Z" };
  assert.deepEqual(scanStatusPayload({ status }), status);
  assert.deepEqual(scanStatusPayload(status), status);
  assert.deepEqual(scanStatusPayload(null), {});
});

test("history dimensions preserve unknown inputs instead of classifying missing market facts as low range", () => {
  assert.equal(mcapBand(null), "unknown");
  assert.equal(ageBand(null), "unknown");
  assert.equal(mcapBand(24_999), "lt_25k");
  assert.equal(ageBand(15), "15d_30d");
});

test("history outcome uses the frozen horizon result, not a later all-time peak", () => {
  const rows = normalizedOutcome({
    max_favorable: { return_pct: 900 },
    horizons: {
      "1h": {
        at: "2026-08-01T01:05:00Z",
        return_pct: 20,
        max_return_pct: 25,
        max_drawdown_pct: -5,
        time_to_1_5x_minutes: null,
      },
    },
  }, {
    caught_at: "2026-08-01T00:00:00Z",
    caught_liquidity_usd: 10_000,
  }, "2026-08-09T00:00:00Z");
  const hour = rows.find((row) => row.horizon_minutes === 60);
  const week = rows.find((row) => row.horizon_minutes === 10_080);
  assert.equal(hour.max_return_pct, 25);
  assert.equal(hour.max_drawdown_pct, -5);
  assert.equal(week.status, "pending");
});

test("history outbox accepts only explicit ledger events", () => {
  assert.deepEqual(historyEventsFromPayload({ history_ledger: { events: [{ episode: { token_address: "a" } }] } }).length, 1);
  assert.deepEqual(historyEventsFromPayload({ history: [{ episode: { token_address: "a" } }] }), []);
});

test("late outcome remains partial instead of becoming an eligible 24h result", () => {
  const rows = normalizedOutcome({horizons: {"24h": {
    at: "2026-08-04T00:00:00Z", target_at: "2026-08-02T00:00:00Z", return_pct: 100,
  }}}, {caught_at: "2026-08-01T00:00:00Z"}, "2026-08-04T00:00:00Z");
  assert.equal(rows.find(row => row.horizon_minutes === 1440).status, "partial");
});

test("outbox batches baseline refresh and acknowledges only after derived writes", async () => {
  for (const failDerived of [false, true]) {
    const queries = [];
    const pending = ["a", "b"].map(token => ({event_id: token, payload_json: JSON.stringify({
      episode: {token_address: token, caught_at: "2026-09-01T00:00:00Z"},
      event: {event_type: "outcome_24h", observed_at: "2026-09-02T00:00:00Z"},
      outcome: {},
    })}));
    let baselineReads = 0;
    const db = {
      prepare(sql) {
        const statement = {
          bind() { return statement; },
          async all() {
            queries.push(sql);
            if (sql.includes("SELECT event_id, payload_json")) return {results: pending};
            if (sql.includes("GROUP BY e.mcap_band")) {
              baselineReads += 1;
              if (failDerived) throw new Error("D1 quota");
            }
            return {results: []};
          },
          async first() { return {count: 0}; },
          async run() { queries.push(sql); return {success: true}; },
        };
        return statement;
      },
      async batch(statements) { return Promise.all(statements.map(statement => statement.run())); },
    };
    const run = flushHistoryOutbox({RADAR_DB: db, RADAR_HISTORY_DB: db});
    if (failDerived) await assert.rejects(run, /D1 quota/);
    else assert.equal((await run).delivered, 2);
    assert.equal(baselineReads, 1);
    assert.equal(queries.filter(sql => sql.includes("SET status = 'delivered'")).length, failDerived ? 0 : 2);
    assert.ok(queries.find(sql => sql.includes("SELECT event_id, payload_json")).includes("WHERE status = 'pending'"));
  }
});

test("history event ids sort by their observation time for deterministic initial backfill", () => {
  const earlier = historyEventId({
    episode: { episode_id: "episode-a" },
    event: { event_type: "signal", observed_at: "2026-08-01T00:00:00Z" },
  });
  const later = historyEventId({
    episode: { episode_id: "episode-b" },
    event: { event_type: "signal", observed_at: "2026-08-02T00:00:00Z" },
  });
  assert.match(earlier, /^history:1785542400:/);
  assert.ok(earlier < later);
});

test("only an original signal can add relationship evidence; outcomes alone refresh learned scores", () => {
  assert.deepEqual(historyEventEffects("signal"), {
    type: "signal",
    isSignalEvent: true,
    isOutcomeEvent: false,
    capturesPriorScore: true,
    updatesOutcomes: true,
    recordsClusterEdge: true,
    refreshesScores: false,
    refreshesClusters: true,
  });
  assert.deepEqual(historyEventEffects("retention_check"), {
    type: "retention_check",
    isSignalEvent: false,
    isOutcomeEvent: false,
    capturesPriorScore: false,
    updatesOutcomes: false,
    recordsClusterEdge: false,
    refreshesScores: false,
    refreshesClusters: false,
  });
  assert.deepEqual(historyEventEffects("outcome_72h"), {
    type: "outcome_72h",
    isSignalEvent: false,
    isOutcomeEvent: true,
    capturesPriorScore: false,
    updatesOutcomes: true,
    recordsClusterEdge: false,
    refreshesScores: true,
    refreshesClusters: true,
  });
});
