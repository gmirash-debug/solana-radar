import test from "node:test";
import assert from "node:assert/strict";
import {gmgnUrl, gmgnMarket, renderGmgnMarket, renderGmgnHolders} from "../gmgn-context.js";
import {resolveAthContext} from "../token-state.js";
const address = "0x" + "1".repeat(40), now = Date.parse("2026-09-10T00:00:00Z");
const token = () => ({token:address, gmgn:{info:{chain_id:4663, token:address, status:"ok", checked_at:new Date(now).toISOString(), price_usd:1, ath:{identity_version:2, token_address:address, highest_price:2}}}});
test("GMGN links are contract scoped, not pool scoped", () => {
  assert.equal(gmgnUrl(address), `https://gmgn.ai/robinhood/token/${address}`);
  assert.equal(gmgnUrl('javascript:alert(1)'), "");
});
test("drawdown is price based and only fresh", () => {
  assert.equal(gmgnMarket(token(),now).drawdown,50);
  assert.equal(gmgnMarket(token(),now+100*60000).drawdown,null);
  const t=token(); t.gmgn.info.status="stale";
  assert.equal(gmgnMarket(t,now).drawdown,null);
});
test("foreign network and token evidence rejected", () => {
  const t=token(); t.gmgn.info.chain_id=1;
  assert.equal(gmgnMarket(t,now),null);
});
test("missing ATH is unknown, not zero", () => {
  const t=token(); t.gmgn.info.ath.highest_price=null;
  assert.equal(gmgnMarket(t,now).high,null);
  assert.match(renderGmgnMarket(t,now), /Unknown/);
});
test("provider sample cannot masquerade as original buyers and escapes tags", () => {
  const t=token(); t.gmgn.holders={token:address, wallets:[{address,tags:['<script>'],transferred_in:1}],excluded:1};
  const html=renderGmgnHolders(t);
  assert.match(html,/not the signal cohort/);
  assert.match(html,/Transfer-in/);
  assert.doesNotMatch(html,/<script>/);
});
test("legacy GMGN ATH is hidden even in previously published snapshots", () => {
  const ctx=resolveAthContext({market:{ath_source:"gmgn",ath_mcap_usd:5000,ath_status:"ready"}});
  assert.equal(ctx.mcapUsd,null);
  assert.equal(ctx.ratio,null);
  assert.equal(ctx.status,"unverified");
});
test("matching contract verification admits only reported market-cap ATH", () => {
  const market={token_address:address,ath_token_address:address,ath_source:"gmgn",ath_identity_version:2,ath_mcap_basis:"matching_token_reported",ath_mcap_usd:5000};
  assert.equal(resolveAthContext({market}).mcapUsd,5000);
  market.ath_token_address="other";
  assert.equal(resolveAthContext({market}).mcapUsd,null);
});
