import assert from "node:assert/strict";
import test from "node:test";

import { chooseDashboardPayload } from "../data-source.js";

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
