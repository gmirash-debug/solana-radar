import assert from "node:assert/strict";
import test from "node:test";

import {
  applyDeletedTokenUpdate,
  claimDispatchBucket,
  corsHeaders,
  requireCloudflareAccess,
  schedulerBucket,
  schedulerKindForCron,
  updateDeletedToken,
} from "../src/index.js";

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

  const entries = new Map();
  const env = {
    DISPATCH_BUCKETS: {
      get: async (key) => entries.get(key) || null,
      put: async (key, value) => entries.set(key, value),
    },
  };
  assert.deepEqual(await claimDispatchBucket(env, "discovery", "discovery:bucket"), { claimed: true, persistent: true });
  assert.deepEqual(await claimDispatchBucket(env, "discovery", "discovery:bucket"), { claimed: false, persistent: true });
});
