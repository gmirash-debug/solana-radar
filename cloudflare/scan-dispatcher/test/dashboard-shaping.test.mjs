import test from "node:test";
import assert from "node:assert/strict";
import {compactDashboardReport, dashboardRecordMatchesAgeWindow, isCurrentDashboardSignal} from "../src/dashboard-shaping.js";
import {isCurrentFilterPool, isCurrentFilterSignal} from "../../../dashboard/filter-scope.js";

const AT = "2026-10-08T00:00:00Z";

function fixture(config, ages = [null, 0.1, 24, 360, 2000]) {
  const rows = ages.map((age_hours, index) => ({token_address:`token-${index}`, age_hours}));
  return {config,
    alerts:rows.map(pool => ({pool, created_at:AT})),
    signal_theses:rows.map(row => ({...row, signal_at:AT, status:"unknown", source_tier:"candidate"})),
    summaries:rows.map(pool => ({pool})),
    active_pools:rows.map(pool => ({pool})),
    universe:rows};
}

test("GMGN all-age publication preserves observations with null age limits", () => {
  const report = fixture({discovery_source_mode:"gmgn_attention", age_min_hours:null, age_max_hours:null});
  const before = structuredClone(report);
  const published = compactDashboardReport(report);
  for (const field of ["alerts", "signal_theses", "summaries", "active_pools", "universe"]) {
    assert.equal(published[field].length, report[field].length, field);
  }
  assert.ok(published.signal_theses.every(row => row.status === "unknown" && row.source_tier === "candidate"));
  assert.deepEqual(report, before);
});

test("GMGN publication cannot reactivate numeric legacy age gates", () => {
  const report = fixture({discovery_source_mode:"gmgn_attention", age_min_hours:24, age_max_hours:360});
  assert.equal(compactDashboardReport(report).signal_theses.length, 5);
});

test("disabled non-GMGN age bounds are unset, not a zero-hour maximum", () => {
  for (const config of [{}, {age_min_hours:null, age_max_hours:null}]) {
    assert.equal(compactDashboardReport(fixture(config)).signal_theses.length, 5);
  }
  const maxOnly = fixture({age_min_hours:null, age_max_hours:360}, [0.1, 24, 360, 361]);
  assert.equal(compactDashboardReport(maxOnly).signal_theses.length, 3);
  const minOnly = fixture({age_min_hours:24, age_max_hours:null}, [0.1, 24, 2000]);
  assert.equal(compactDashboardReport(minOnly).signal_theses.length, 2);
});

test("explicit zero remains a real bound for legacy discovery", () => {
  const report = fixture({age_min_hours:null, age_max_hours:0}, [0, 0.1, 24]);
  const published = compactDashboardReport(report);
  assert.deepEqual(published.signal_theses.map(row => row.token_address), ["token-0"]);
});

test("GMGN all-age publication still enforces the original signal epoch", () => {
  const report = fixture({discovery_source_mode:"gmgn_attention", age_min_hours:null,
    age_max_hours:null, dashboard_signal_epoch:AT}, [2000]);
  report.signal_theses.push({token_address:"old", signal_at:"2026-08-12T00:00:00Z", age_hours:2000});
  report.alerts.push({pool:{token_address:"old", age_hours:2000}, created_at:"2026-08-12T00:00:00Z"});
  const published = compactDashboardReport(report);
  assert.deepEqual(published.signal_theses.map(row => row.token_address), ["token-0"]);
  assert.deepEqual(published.alerts.map(row => row.pool.token_address), ["token-0"]);
});

test("worker and browser agree on GMGN and nullable age scope", () => {
  const configs = [
    {}, {age_min_hours:null, age_max_hours:null}, {age_min_hours:24, age_max_hours:null},
    {age_min_hours:null, age_max_hours:360}, {age_min_hours:0.5, age_max_hours:360},
    {age_min_hours:null, age_max_hours:0},
    {discovery_source_mode:"gmgn_attention", age_min_hours:null, age_max_hours:null},
    {discovery_source_mode:"gmgn_attention", age_min_hours:24, age_max_hours:360},
  ];
  for (const config of configs) {
    const report = fixture(config);
    for (const alert of report.alerts) {
      assert.equal(dashboardRecordMatchesAgeWindow(alert, report), isCurrentFilterPool(alert, report));
      assert.equal(isCurrentDashboardSignal(alert, report), isCurrentFilterSignal(alert, report));
    }
  }
});
