import assert from "node:assert/strict";
import test from "node:test";

import { chooseDashboardPayload } from "../data-source.js";

function payload(generatedAt) {
  return { report: { generated_at: generatedAt } };
}

test("newer static snapshot wins over stale Convex data", () => {
  const staticPayload = payload("2026-07-31T12:00:00Z");
  const convexPayload = payload("2026-07-31T11:00:00Z");
  const selected = chooseDashboardPayload({ staticPayload, convexPayload });
  assert.equal(selected.source, "static");
  assert.equal(selected.payload, staticPayload);
  assert.equal(selected.fallbackReason, "static_snapshot_is_newer_or_equal");
});

test("static snapshot wins an equal timestamp and Convex wins only when newer", () => {
  const staticPayload = payload("2026-07-31T12:00:00Z");
  assert.equal(
    chooseDashboardPayload({ staticPayload, convexPayload: payload("2026-07-31T12:00:00Z") }).source,
    "static",
  );
  assert.equal(
    chooseDashboardPayload({ staticPayload, convexPayload: payload("2026-07-31T12:01:00Z") }).source,
    "convex",
  );
});
