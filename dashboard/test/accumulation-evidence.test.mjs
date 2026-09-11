import test from "node:test";
import assert from "node:assert/strict";
import {renderAccumulationEvidence} from "../accumulation-evidence.js";

test("legacy snapshot explicitly shows unknown evidence", () => {
  const html = renderAccumulationEvidence({});
  assert.match(html, /Not verified/);
  assert.match(html, /Source-chain funding not checked/);
  assert.doesNotMatch(html, />0\.00%</);
});
test("pending trace never presents old percentages as current", () => {
  const html = renderAccumulationEvidence({position_flow:{status:"pending",supply_pct:{sold:42}}});
  assert.doesNotMatch(html, /42\.00%/);
  assert.match(html,/Check pending/);
});
test("transferred and sold remain distinct and all text escaped", () => {
  const html = renderAccumulationEvidence({position_flow:{status:"checked",checked_block:10,checked_at:new Date().toISOString(),
    supply_pct:{original:2,transferred:4,sold:1,unknown:1},recipients:[{address:'<script>bad</script>',depth:1}]}});
  assert.match(html,/At transfer recipients/);
  assert.match(html,/4\.00%/);
  assert.match(html,/Confirmed sold/);
  assert.match(html,/1\.00%/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/do not prove the same owner/);
});
test("old trace stays unverified even when the dashboard refreshed", () => {
  const html = renderAccumulationEvidence({position_flow:{status:"checked",checked_at:"2020-01-01T00:00:00Z",supply_pct:{sold:42}}});
  assert.doesNotMatch(html,/42\.00%/);
});
