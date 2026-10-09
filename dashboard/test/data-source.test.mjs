import assert from "node:assert/strict";
import test from "node:test";

import { chooseDashboardPayload, mergeRetirementMarkers, applyRetirementFences, retirementRecordCurrent } from "../data-source.js";

function payload(generatedAt) {
  return { report: { generated_at: generatedAt } };
}

test("newer static snapshot wins over stale remote data", () => {
  const staticPayload = payload("2026-07-31T12:00:00Z");
  const remotePayload = payload("2026-07-31T11:00:00Z");
  const selected = chooseDashboardPayload({ staticPayload, remotePayload });
  assert.equal(selected.source, "static");
  assert.equal(selected.payload, staticPayload);
  assert.equal(selected.fallbackReason, "remote_snapshot_stale");
});

test("remote data wins equal timestamps and newer snapshots", () => {
  const staticPayload = payload("2026-07-31T12:00:00Z");
  assert.equal(
    chooseDashboardPayload({ staticPayload, remotePayload: payload("2026-07-31T12:00:00Z") }).source,
    "remote",
  );
  assert.equal(
    chooseDashboardPayload({ staticPayload, remotePayload: payload("2026-07-31T12:01:00Z") }).source,
    "remote",
  );
});

test("remote retirement fences older static tokens even when the static report is newer", () => {
  const old={token_address:"token",signal_at:"2026-10-08T01:00:00Z"};
  const staticPayload={report:{generated_at:"2026-10-09T12:00:00Z",signal_theses:[old],
    universe:[{token_address:"token"}],active_pools:[{pool:{token_address:"token"}}]},market:{token:{mcap:19000}}};
  const remotePayload={report:{generated_at:"2026-10-09T11:00:00Z"},token_retirements:{token:{retired_at:"2026-10-09T10:00:00Z"}}};
  const selected=chooseDashboardPayload({staticPayload,remotePayload});
  assert.equal(selected.source,"static");assert.equal(selected.payload.report.signal_theses.length,0);
  assert.equal(selected.payload.market.token,undefined);
  assert.equal(selected.payload.report.universe.length,0);
  assert.equal(selected.payload.report.active_pools.length,0);
  assert.equal(staticPayload.report.signal_theses.length,1);
});

test("a saved recapture cannot override a newer retirement, or bring back an old cohort", () => {
  const marker={retired_at:"2026-10-09T10:00:00Z",reactivated_at:"2026-10-09T11:00:00Z"};
  const second={retired_at:"2026-10-10T12:00:00Z"};
  const markers=mergeRetirementMarkers({token:marker},{token:second},{token:marker});
  assert.equal(markers.token,second);
  assert.equal(retirementRecordCurrent({token_address:"token",signal_at:"2026-10-09T11:00:00Z"},markers),false);
  const active=applyRetirementFences({report:{signal_theses:[
    {token_address:"token",signal_at:"2026-10-09T01:00:00Z"},
    {token_address:"token",signal_at:"2026-10-09T10:30:00Z"}]}},{token:marker});
  assert.equal(active.report.signal_theses.length,1);
  assert.equal(active.report.signal_theses[0].signal_at,"2026-10-09T10:30:00Z");
});
