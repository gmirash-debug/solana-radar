import assert from "node:assert/strict";
import test from "node:test";

import { resolveSignalEpisodes } from "../token-state.js";
import {
  DEFAULT_DASHBOARD_SIGNAL_EPOCH,
  dashboardSignalEpochMs,
  isCurrentFilterPool,
  isCurrentFilterSignal,
  marketWithCurrentFilterCatch,
} from "../filter-scope.js";

test("the current filter scope excludes historical alerts and theses", () => {
  const report = {};

  assert.equal(
    isCurrentFilterSignal({ signal_at: "2026-08-13T01:00:59Z" }, report),
    false,
  );
  assert.equal(
    isCurrentFilterSignal({ signal_at: DEFAULT_DASHBOARD_SIGNAL_EPOCH }, report),
    true,
  );
  assert.equal(
    isCurrentFilterSignal({ created_at: "2026-08-13T02:00:00Z" }, report),
    true,
  );
  assert.equal(
    isCurrentFilterSignal(
      {
        created_at: "2026-08-13T02:00:00Z",
        pool: {
          token_address: "old-monitor-alert",
          source: "signal_thesis_monitor",
        },
      },
      {
        summaries: [{
          pool: {
            token_address: "old-monitor-alert",
            market_source: "signal_thesis_monitor",
            first_signal_at: "2026-08-02T19:46:56Z",
          },
        }],
      },
    ),
    false,
  );
  assert.equal(isCurrentFilterSignal({}, report), false);
});

test("a report epoch overrides the static fallback", () => {
  const report = {
    config: { dashboard_signal_epoch: "2026-08-12T00:00:00Z" },
  };

  assert.equal(
    dashboardSignalEpochMs(report),
    Date.parse("2026-08-12T00:00:00Z"),
  );
  assert.equal(
    isCurrentFilterSignal({ created_at: "2026-08-12T00:00:00Z" }, report),
    true,
  );
});

test("the current filter scope excludes pools outside its age window", () => {
  const report = {
    config: {
      age_min_hours: 24,
      age_max_hours: 360,
    },
  };

  assert.equal(isCurrentFilterPool({ age_hours: 72 }, report), true);
  assert.equal(isCurrentFilterPool({ age_hours: 361 }, report), false);
  assert.equal(isCurrentFilterPool({}, report), false);
  assert.equal(
    isCurrentFilterSignal(
      { created_at: "2026-08-13T02:00:00Z", pool: { age_hours: 361 } },
      report,
    ),
    false,
  );
});

test("the thirty-minute filter includes young pools without resetting prior catches", () => {
  const report = { config: { age_min_hours: 0.5, age_max_hours: 360 } };
  for (const [age, included] of [[0.5 - 1 / 3600, false], [0.5, true], [23, true],
    [24, true], [360, true], [360 + 1 / 3600, false]]) {
    const signal = { created_at: "2026-10-03T02:00:00Z", pool: { age_hours: age } };
    assert.equal(isCurrentFilterPool(signal, report), included, `age ${age}`);
    assert.equal(isCurrentFilterSignal(signal, report), included, `signal age ${age}`);
  }
  assert.equal(isCurrentFilterPool({}, report), false);
  const existing = { first_signal_at: "2026-09-30T02:00:00Z", first_obs_mcap_usd: 20_000 };
  assert.deepEqual(marketWithCurrentFilterCatch(existing, report), existing);
});

test("the thirty-minute filter uses the actual pair timestamp over a stale reported age", () => {
  const report = { config: { age_min_hours: 0.5, age_max_hours: 360 } };
  const created = Date.now() - 31 * 60_000;
  assert.equal(isCurrentFilterPool({ pair_created_at: created, age_hours: 0.2 }, report), true);
  assert.equal(isCurrentFilterPool({ pool: { pair_created_at: created / 1000 } }, report), true);
});

test("a legacy catch cannot overwrite a new filter-era catch", () => {
  const market = {
    first_signal_at: "2026-07-31T10:00:00Z",
    first_obs_mcap_usd: 75_000,
    first_obs_price_usd: 0.000075,
    first_obs_mcap_at: "2026-07-31T10:00:00Z",
    first_obs_lane: "reactivation",
    caught_obs_mcap_usd: 75_000,
    latest_mcap_usd: 125_000,
    ath_mcap_usd: 500_000,
  };
  const scopedMarket = marketWithCurrentFilterCatch(market, {});
  const episodes = resolveSignalEpisodes({
    market: scopedMarket,
    alerts: [{
      lane: "reactivation",
      created_at: "2026-08-13T02:00:00Z",
      obs_mcap_usd: 120_000,
      obs_price_usd: 0.00012,
      action_tier: "watch",
    }],
  });

  assert.equal(scopedMarket.first_signal_at, undefined);
  assert.equal(scopedMarket.latest_mcap_usd, 125_000);
  assert.equal(scopedMarket.ath_mcap_usd, 500_000);
  assert.equal(episodes.originalCatch.at, "2026-08-13T02:00:00Z");
  assert.equal(episodes.originalCatch.mcapUsd, 120_000);
});
