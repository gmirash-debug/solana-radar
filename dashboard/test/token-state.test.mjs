import test from "node:test";
import assert from "node:assert/strict";

import {
  compareTokensByCatchNewest,
  resolveAthContext,
  resolveCurrentMarket,
  resolveSignalEpisodes,
  resolveWorkflowStatus,
} from "../token-state.js";

test("workflow never substitutes an old holding thesis for a confirmed entry", () => {
  assert.equal(resolveWorkflowStatus({lifecycle: "holding", dataStatus: "current", currentTier: "watch"}), "candidate");
  assert.equal(resolveWorkflowStatus({lifecycle: "holding", dataStatus: "current", currentTier: "hot_reactivation", currentConfirmed: true}), "hot");
  assert.equal(resolveWorkflowStatus({lifecycle: "holding", dataStatus: "current", thesisConfirmed: true}), "active");
  assert.equal(resolveWorkflowStatus({lifecycle: "holding", dataStatus: "overdue", thesisConfirmed: true}), "recheck_due");
  assert.equal(resolveWorkflowStatus({lifecycle: "closed", dataStatus: "overdue"}), "inactive");
});

test("newer catch sorts ahead of an older catch", () => {
  const newest = { firstSignalAt: "2026-07-31T09:00:00Z" };
  const oldest = { firstSignalAt: "2026-07-30T09:00:00Z" };
  const missingCatch = {};

  assert.ok(compareTokensByCatchNewest(newest, oldest) < 0);
  assert.ok(compareTokensByCatchNewest(oldest, newest) > 0);
  assert.ok(compareTokensByCatchNewest(newest, missingCatch) < 0);
});

test("DATA-style stale market cannot create current PnL or ATH ratio", () => {
  const market = resolveCurrentMarket({
    pool: {
      latest_mcap_usd: 146_604,
      latest_price_usd: 0.000146604,
      latest_liquidity_usd: 38_000,
      latest_seen_at: "2026-07-27T17:09:15Z",
      market_snapshot_stale: true,
      market_snapshot_error: "dexscreener_not_found",
    },
    now: Date.parse("2026-07-31T00:30:00Z"),
  });
  assert.equal(market.isFresh, false);
  assert.equal(market.mcapUsd, null);
  assert.equal(market.priceUsd, null);
  assert.equal(market.lastVerifiedMcapUsd, 146_604);

  const ath = resolveAthContext({
    market: {
      ath_source: "solana_tracker",
      ath_status: "ready",
      ath_mcap_usd: 680_162,
      ath_mcap_at: "2026-06-19T16:35:00Z",
    },
    currentMarket: market,
  });
  assert.equal(ath.mcapUsd, 680_162);
  assert.equal(ath.ratio, null);
  assert.equal(ath.status, "legacy");
});

test("an active Reactivation episode does not overwrite original catch metadata", () => {
  const episodes = resolveSignalEpisodes({
    market: {
      first_signal_at: "2026-06-01T17:02:55Z",
      first_obs_mcap_usd: 158_686,
      first_obs_price_usd: 0.000158686,
      first_obs_lane: "cheap_sticky",
      first_obs_score: 83,
    },
    thesis: {
      signal_at: "2026-07-30T22:11:49Z",
      signal_mcap_usd: 45_317,
      signal_price_usd: 0.000045317,
      source_tier: "watch",
      source_score: 45,
      source_flow_sol: 14.26,
      source_wallets: 7,
    },
    alerts: [{
      lane: "reactivation",
      created_at: "2026-07-30T22:11:49Z",
      obs_mcap_usd: 45_317,
      action_tier: "watch",
      score: 45,
      pool: { price_usd: 0.000045317 },
    }],
  });
  assert.equal(episodes.originalCatch.at, "2026-06-01T17:02:55Z");
  assert.equal(episodes.originalCatch.mcapUsd, 158_686);
  assert.equal(episodes.originalCatch.lane, "cheap_sticky");
  assert.equal(episodes.activeEpisode.at, "2026-07-30T22:11:49Z");
  assert.equal(episodes.activeEpisode.mcapUsd, 45_317);
  assert.equal(episodes.displayCatch, episodes.activeEpisode);
});

test("fresh direct report market supersedes an older market cache", () => {
  const market = resolveCurrentMarket({
    pool: {
      _snapshot_source: "universe",
      _observed_at: "2026-07-31T00:00:00Z",
      mcap_usd: 36_700,
      price_usd: 0.0000367,
      liquidity_usd: 18_800,
      latest_seen_at: "2026-07-27T17:09:15Z",
      market_snapshot_stale: true,
    },
    now: Date.parse("2026-07-31T00:30:00Z"),
  });
  assert.equal(market.isFresh, true);
  assert.equal(market.mcapUsd, 36_700);
  assert.equal(market.priceUsd, 0.0000367);
});
