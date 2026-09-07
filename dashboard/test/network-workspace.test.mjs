import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {networkFromUrl, networkUrl} from "../radar-bootstrap.js";
import {reviewGroup, positionBounds, marketFresh, comparePositions, selectTokens} from "../robinhood-state.js";

const now = Date.parse("2026-09-07T12:00:00Z");
const payload = {status:"ok", generated_at:"2026-09-07T11:45:00Z"};
const token = {key:"4663:a", status:"observed", checked_at:payload.generated_at, wallets:[]};
const wallet = {bought_raw:"100", retained_lower_bound_raw:"20", retained_upper_bound_raw:"60"};
test("network switch uses the common entry and removes network-specific state", () => {
  assert.equal(networkFromUrl("https://example.com/radar/"),"solana");
  assert.equal(networkFromUrl("https://example.com/radar/?network=robinhood"),"robinhood");
  assert.equal(networkFromUrl("https://example.com/?network=evil"),"solana");
  assert.equal(networkUrl("https://example.com/radar/index.html?token=abc#detail","robinhood"),"https://example.com/radar/index.html?network=robinhood");
  assert.equal(networkUrl("https://example.com/radar/index.html?network=robinhood","solana"),"https://example.com/radar/index.html");
});
test("one shell and stylesheet serve both networks; old URL is just a redirect", () => {
  const html = readFileSync(new URL("../index.html",import.meta.url),"utf8");
  const old = readFileSync(new URL("../robinhood.html",import.meta.url),"utf8");
  assert.match(html,/radar-bootstrap\.js/);
  assert.match(html,/workspace\.css/);
  assert.match(old,/location\.replace/);
  assert.doesNotMatch(old,/robinhood\.css|rh-workspace/);
});
test("freshness and original cohort evidence gate the holding group", () => {
  assert.equal(reviewGroup(token,payload,now),"observed");
  assert.equal(reviewGroup({...token,status:"retained"},payload,now),"needs_data");
  const held = {...token,status:"retained",cohort_checks:2,cohort_created_at:payload.generated_at};
  assert.equal(reviewGroup(held,payload,now),"retained");
  assert.equal(reviewGroup(held,{...payload,status:"unavailable"},now),"needs_data");
  assert.equal(reviewGroup({...held,checked_at:"2026-09-07T09:00:00Z"},payload,now),"needs_data");
  assert.equal(reviewGroup({...token,status:"queued",security:{status:"risk"}},payload,now),"risk");
});
test("position percentages are weighted purchase bounds, not supply or wallet count", () => {
  assert.deepEqual(positionBounds({wallets:[wallet,{bought_raw:"300",retained_lower_bound_raw:"300",retained_upper_bound_raw:"300"}]}),{lower:80,upper:90});
  assert.deepEqual(positionBounds({wallets:[{...wallet,retained_lower_bound_raw:null}]}),{lower:null,upper:60});
  assert.deepEqual(positionBounds({wallets:[{...wallet,retained_lower_bound_raw:"0",retained_upper_bound_raw:"0"}]}),{lower:0,upper:0});
  for (const wallets of [[],[{...wallet,retained_upper_bound_raw:""}],[{...wallet,bought_raw:"0"}],[{...wallet,retained_lower_bound_raw:"70"}],[{...wallet,retained_upper_bound_raw:null}]]) assert.equal(positionBounds({wallets}),null);
});
test("market freshness is independent of page and wallet refresh", () => {
  assert.equal(marketFresh({...token,market_checked_at:payload.generated_at},now),true);
  assert.equal(marketFresh(token,now),false);
  assert.equal(marketFresh({...token,market_checked_at:payload.generated_at,market_stale:true},now),false);
  assert.equal(marketFresh({...token,market_checked_at:"2026-09-07T15:00:00Z"},now),false);
});
test("wallet search and deterministic retention sorting preserve unknown versus zero", () => {
  const a = {...token,key:"a",wallets:[{...wallet,address:"0x123"}],first_observed_at:"2026-09-06T12:00:00Z"};
  const b = {...token,key:"b",first_observed_at:payload.generated_at};
  assert.deepEqual(selectTokens([a,b]," 0x123 "),[a]);
  assert.ok(comparePositions(a,b,"retained") < 0);
  assert.ok(comparePositions(a,b,"caught") > 0);
  const zero = {...a,wallets:[{...wallet,retained_lower_bound_raw:"0",retained_upper_bound_raw:"0"}]};
  assert.ok(comparePositions(zero,b,"retained") < 0);
});
