import assert from "node:assert/strict";
import test from "node:test";

import {
  applyDeletedTokenUpdate,
  claimDispatchBucket,
  corsHeaders,
  compactDashboardReport,
  discoveryStateForTokens,
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
} from "../src/history.js";

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
