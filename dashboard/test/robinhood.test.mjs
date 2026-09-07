import test from "node:test";
import assert from "node:assert/strict";
import {validSnapshot, isFresh, selectTokens, formatSupplyPercent} from "../robinhood-state.js";
test("small positive holdings never look like a zero balance", () => {
  assert.equal(formatSupplyPercent(0.00002), "<0.01%");
  assert.equal(formatSupplyPercent(0), "0.00%");
  assert.equal(formatSupplyPercent(null), "Unknown");
});
const token = {token:`0x${"1".repeat(40)}`, pool:`0x${"2".repeat(40)}`, symbol:"Test", status:"observed", wallets:[]};
token.key = `4663:${token.token}`;
test("network isolation and valid addresses", () => {
  assert.equal(validSnapshot({chain_id:4663,tokens:[token]}), true);
  assert.equal(validSnapshot({chain_id:1,tokens:[token]}), false);
  assert.equal(validSnapshot({chain_id:4663,tokens:[{...token,key:token.token}]}), false);
  assert.equal(validSnapshot({chain_id:4663,tokens:[{...token,pool:'javascript:alert(1)'}]}), false);
});
test("failed, future and stale data never fresh", () => {
  const now = Date.parse("2026-09-07T12:00:00Z");
  assert.equal(isFresh({status:"ok",generated_at:"2026-09-07T11:30:00Z"},now),true);
  for(const payload of [{status:"unavailable",generated_at:"2026-09-07T11:30:00Z"},{status:"ok",generated_at:"2026-09-07T13:00:00Z"},{status:"ok",generated_at:"2026-09-07T09:00:00Z"}]) assert.equal(isFresh(payload,now),false);
});
test("newest first, search and status filters", () => {
  const a = {...token,first_observed_at:"2026-09-06T00:00:00Z"};
  const b = {...token,key:"new",symbol:"New",status:"buy_wave",first_observed_at:"2026-09-07T00:00:00Z"};
  assert.deepEqual(selectTokens([a,b]),[b,a]);
  assert.deepEqual(selectTokens([a,b],"new","buy_wave"),[b]);
  assert.deepEqual(selectTokens([a,b],"","queued"),[]);
});
