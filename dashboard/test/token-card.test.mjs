import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync, readdirSync} from "node:fs";
import vm from "node:vm";
import {JSDOM} from "jsdom";
import * as dataSource from "../data-source.js";
import * as coordination from "../coordinated-activity.js";
import * as terminology from "../terminology.js";
import * as decision from "../decision-view.js";
import * as staticDetail from "../static-detail.js";
import * as tokenState from "../token-state.js";
import * as filterScope from "../filter-scope.js";

const now = Date.parse("2026-10-03T12:00:00Z");
const checked = "2026-10-03T11:50:00Z";
const pool = {token_address:"mint", pool_address:"pool", symbol:"TEST", dex:"pumpswap",
  mcap_usd:100000, price_usd:0.02, liquidity_usd:10000, current_market_verified_at:checked};
const start = "2026-10-03T11:00:00Z", end = "2026-10-03T11:05:00Z";
const cohortWallet = patch => ({owner:"buyer", attributed_tokens:100, buy_sol:1,
  current_retained_tokens:40, current_balance:40, checked_at:checked, is_holder:true, ...patch});
const thesis = patch => ({...pool, cohort_id:"original", signal_at:end,
  signal_window_start:start, signal_window_end:end, last_checked_at:checked, updated_at:checked,
  next_check_at:"2026-10-03T12:50:00Z", status:"intact", source_tier:"watch", source_score:50,
  original_wallets:1, token_retention_pct:40, current_retained_supply_pct:0.4,
  cohort_wallets:[cohortWallet()], ...patch});
const event = patch => ({signature:"buy", token_recipient:"buyer", signer:"buyer",
  time:start, sol_amount:1, token_amount:100, price_native:0.0001, ...patch});
const alert = patch => ({pool, lane:"reactivation", created_at:end,
  window_start:start, window_end:end, action_tier:"watch", events:[event()], ...patch});

function fixture(t, {signalThesis = thesis(), alerts = [], overrides = {}} = {}) {
  const dom = new JSDOM(readFileSync(new URL("../index.html", import.meta.url), "utf8"), {url:"https://radar.test/"});
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({...dataSource, ...coordination, ...terminology, ...decision,
    ...staticDetail, ...tokenState, ...filterScope,
    decisionView:(token, config) => decision.decisionView(token, config, now),
    resolveCurrentMarket:args => tokenState.resolveCurrentMarket({...args, now}),
    document:dom.window.document, window:dom.window, localStorage:dom.window.localStorage,
    Date:FixedDate, console, URL, ...overrides});
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  // Execute the actual card functions and bindings without startup requests or polling.
  const body = source.slice(0, source.lastIndexOf("\nloadData().catch("))
    .replace(/^import\s[\s\S]*?;\n/gm, "");
  vm.runInContext(body, context);
  const api = vm.runInContext(`({state, buildTokenSignals, renderWalletRows, walletHeldLabel,
    renderReviewRow, renderThesisSummary, renderSupplyLinkageGroups, supplyPct,
    mergeTokenAlertDetails, ensureTokenDetail, applyDashboardPayload, detailLoadMessage,
    renderObservedPositionActivity})`, context);
  api.state.report = {generated_at:checked, alerts, signal_theses:signalThesis ? [signalThesis] : [], config:{}};
  api.state.history = [];
  t.after(() => dom.window.close());
  return {...api, dom, context};
}

test("local static previews hydrate matching published wallet details without enabling remote mode", async t => {
  const detail = {ok:true, token_key:"mint", report_source_updated_at:checked,
    thesis:thesis(), current_alerts:[], history:[], source:"published_scan"};
  const requests = [];
  const api = fixture(t, {signalThesis:thesis({cohort_wallets:undefined}), overrides:{
    AbortController, setTimeout, clearTimeout,
    fetch:async url => { requests.push(url); return {ok:true,json:async () => detail}; },
  }});
  api.state.dataSource = "static";
  api.state.tokenDetailManifest = {generation:checked,files:{mint:`data/token-details/${"a".repeat(64)}.json`}};
  await api.ensureTokenDetail("mint");
  assert.equal(api.state.publishedDashboard, false);
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"), true);
  assert.equal(api.buildTokenSignals()[0].wallets.length, 1);
  assert.deepEqual(requests, [`data/token-details/${"a".repeat(64)}.json`]);
});

test("thesis token attribution is not an observed buy count; absent entry cost is unknown", t => {
  const api = fixture(t, {signalThesis:thesis({cohort_wallets:[cohortWallet({buy_sol:undefined})]})});
  const token = api.buildTokenSignals()[0], wallet = token.wallets[0];
  assert.equal(wallet.buys, null);
  assert.equal(wallet.sol_in, null);
  assert.equal(wallet.sells, null);
  const table = JSDOM.fragment(api.renderWalletRows(token));
  const cells = table.querySelectorAll("tbody td");
  assert.equal(cells[2].textContent, "Unknown");
  assert.equal(cells[3].textContent, "Unknown");
});

test("wallet counts and entry basis cannot be borrowed from a different buy window", t => {
  const later = alert({window_start:"2026-10-03T11:30:00Z",window_end:"2026-10-03T11:35:00Z",
    created_at:"2026-10-03T11:35:00Z", events:[event({signature:"later",sol_amount:9,token_amount:900})]});
  const api = fixture(t, {alerts:[alert(),later]});
  const wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.buys, 1);
  assert.equal(wallet.sol_in, 1);
  assert.equal(wallet.tokens_bought, 100);
  assert.equal(wallet.retained_pct, 40);
});

test("buy events alone establish activity, never current holding or PnL", t => {
  const api = fixture(t, {signalThesis:null, alerts:[alert()]});
  const wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.buys, 1);
  assert.equal(wallet.retained_pct, null);
  assert.equal(wallet.pnl_sol, null);
  assert.equal(wallet.pnl_pct, null);
  assert.equal(wallet.realized_pnl_sol, null);
  assert.equal(api.walletHeldLabel(wallet), "not available");
});

test("wave balances require explicit verification; known counts and observed counts remain distinct", t => {
  const buyer = {owner:"buyer",token_bought:100,buy_sol:1,current_balance:80,retained_from_wave:80};
  const api = fixture(t, {signalThesis:null, alerts:[alert({wave:{top_buyers:[buyer]},events:[]})]});
  let wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.buys, null);
  assert.equal(wallet.retained_pct, null);
  api.state.report.alerts = [alert({wave:{balance_coverage_pct:100,top_buyers:[{...buyer,buy_count:0}]},events:[]})];
  wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.buys, 0);
  assert.equal(wallet.retained_pct, 80);
  api.state.report.alerts = [alert({wave:{balance_coverage_pct:100,top_buyers:[{...buyer,balance_verified:false}]}})];
  wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.buys, 1);
  assert.equal(wallet.retained_pct, null);
});

test("false numeric zero, absent attribution and invalid check times cannot produce a wallet balance", t => {
  const api = fixture(t);
  for (const value of [null,undefined,"","  ",false,true,[],{},NaN,-1]) {
    api.state.report.signal_theses = [thesis({cohort_wallets:[cohortWallet({current_retained_tokens:value})]})];
    const wallet = api.buildTokenSignals()[0].wallets[0];
    assert.equal(wallet.retained_pct, null, String(value));
    assert.equal(wallet.pnl_sol, null);
  }
  for (const patch of [{attributed_tokens:undefined}, {checked_at:"invalid"}, {checked_at:"2026-10-03T13:00:00Z"}]) {
    api.state.report.signal_theses = [thesis({cohort_wallets:[cohortWallet(patch)]})];
    assert.equal(api.buildTokenSignals()[0].wallets[0].retained_pct, null);
  }
});

test("measured zero stays zero even if is_holder is contradictory; no sale is inferred", t => {
  const api = fixture(t, {signalThesis:thesis({cohort_wallets:[cohortWallet({current_balance:0})]})});
  const wallet = api.buildTokenSignals()[0].wallets[0];
  assert.equal(wallet.retained_pct, 0);
  assert.equal(wallet.is_signal_holder, false);
  assert.equal(wallet.realized_pnl_sol, null);
  assert.equal(api.walletHeldLabel(wallet), "0% retained");
  assert.equal(wallet.pnl_basis, "tokens left tracked wallet");
});

test("overdue wallet balances retain their date and cannot show current open returns", t => {
  const api = fixture(t, {signalThesis:thesis({cohort_wallets:[cohortWallet({checked_at:"2026-10-03T09:00:00Z"})]}),alerts:[alert()]});
  const wallet = api.buildTokenSignals()[0].wallets[0];
  assert.match(api.walletHeldLabel(wallet), /at last check$/);
  assert.equal(wallet.retention_fresh, false);
  assert.equal(wallet.pnl_pct, null);
  assert.equal(wallet.pnl_sol, null);
});

test("list supply and position bounds distinguish tiny nonzero values from measured zero", t => {
  const api = fixture(t);
  const token = api.buildTokenSignals()[0];
  token.decision = {...token.decision,retained:0.02,supply:0.0002};
  const row = JSDOM.fragment(api.renderReviewRow(token));
  assert.equal(row.querySelector(".review-position strong").textContent, "<1%");
  assert.equal(row.querySelector(".review-position small").textContent, "<0.1% supply");
  token.decision = {...token.decision,retained:0,supply:0};
  assert.equal(JSDOM.fragment(api.renderReviewRow(token)).querySelector(".review-position strong").textContent, "0%");
  for (const value of ["", "  ", false, true, [], {}, -1, 101]) assert.equal(api.supplyPct(value), "-");
  assert.equal(api.supplyPct(0), "0.0%");
});

test("missing holder count is not displayed as zero wallets holding", t => {
  const api = fixture(t);
  const token = api.buildTokenSignals()[0];
  assert.match(api.renderThesisSummary(token), /holder count unverified/);
  token.signalThesis.holders_remaining = 0;
  assert.match(api.renderThesisSummary(token), /0\/1 tracked wallets holding at last check/);
});

test("supporting-only or unspecified wallet links never receive a verified transfer badge", t => {
  const api = fixture(t);
  const group = {family:"common_funder",key:"source",wallets:2};
  for (const patch of [{}, {supporting_only:false}, {supporting_only:true,transfer_verified:true}]) {
    assert.doesNotMatch(api.renderSupplyLinkageGroups({evidence_version:2,linkage_groups:[{...group,...patch}]}), /verified transfer link/);
  }
  assert.match(api.renderSupplyLinkageGroups({evidence_version:2,linkage_groups:[{...group,supporting_only:false,transfer_verified:true}]}), /verified transfer link/);
});

test("unrelated detail alerts cannot add wallets or replace another token's alert", t => {
  const api = fixture(t);
  const existing = alert(), foreign = alert({pool:{...pool,token_address:"other"},events:[event({token_recipient:"other-wallet"})]});
  const rows = api.mergeTokenAlertDetails([existing],"mint",[foreign]);
  assert.equal(rows.length,1);
  assert.equal(rows[0],existing);
});

test("same-cohort cached wallet observations survive a newer summary without overwriting it", t => {
  const original = thesis(), api = fixture(t, {signalThesis:original});
  vm.runInContext("render = () => {};", api.context);
  const detail = {token_key:"mint",thesis:original,report_source_updated_at:checked,source:"published_scan"};
  api.state.tokenDetailCache.set("mint",detail);
  const summary = {...original,last_checked_at:"2026-10-03T11:55:00Z",updated_at:"2026-10-03T11:55:00Z",
    token_retention_pct:80,cohort_wallets:[]};
  api.applyDashboardPayload({report:{generated_at:"2026-10-03T11:55:00Z",alerts:[],signal_theses:[summary],config:{}}},"static");
  const token = api.buildTokenSignals()[0];
  assert.equal(token.signalThesis.token_retention_pct,80);
  assert.equal(token.wallets[0].retained_pct,40);
  assert.equal(token.wallets[0].retention_fresh,false);
  assert.match(api.walletHeldLabel(token.wallets[0]), /at last check$/);
  assert.match(api.detailLoadMessage("mint"), /earlier published check/);
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"),false);
  api.applyDashboardPayload({report:{generated_at:"2026-10-03T11:56:00Z",alerts:[],
    signal_theses:[{...summary,cohort_id:"replacement"}],config:{}}},"static");
  assert.equal(api.state.tokenDetailCache.has("mint"),false);
  assert.equal(api.buildTokenSignals()[0].wallets.length,0);
});

test("a snapshot changing after detail acceptance still prevents application", async t => {
  let api;
  api = fixture(t, {overrides:{loadTokenDetail:async args => {
    const detail = {token_key:"mint",report_source_updated_at:checked,thesis:thesis()};
    assert.equal(args.accepts(detail),true);
    api.state.report.generated_at = "2026-10-03T11:55:00Z";
    return detail;
  }}});
  api.state.publishedDashboard = true;
  api.state.tokenDetailManifest = {generation:checked};
  await api.ensureTokenDetail("mint");
  assert.equal(api.state.tokenDetailCache.has("mint"),false);
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"),false);
});

test("partial movement note needs actual observations and never renders raw amounts or confirms sales", t => {
  const api = fixture(t);
  const token = api.buildTokenSignals()[0];
  const activity = {status:"partial",scope:"supplied_pool_transactions_strictly_after_signal",
    wallet_history_complete:false,affects_original_cohort_retention:false,
    owners:{buyer:{direct_transfer_raw:"123456789123456789"}}, observations:[]};
  token.signalThesis.observed_position_activity = activity;
  assert.equal(api.renderObservedPositionActivity(token), "");
  activity.observations = [{direct_transfer_raw:"123456789123456789"}];
  const html = api.renderObservedPositionActivity(token);
  assert.match(html, /1 observation \/ partial wallet history \/ not confirmed sales/);
  assert.doesNotMatch(html, /123456789123456789/);
  assert.equal(token.signalThesis.token_retention_pct,40);
});

test("HTML entrypoint and every dashboard JS import use the same evidence cache tag", () => {
  const html = readFileSync(new URL("../index.html",import.meta.url),"utf8");
  assert.match(html, /src="radar-bootstrap\.js\?v=20261003-evidence-6"/);
  for (const file of readdirSync(new URL("../",import.meta.url)).filter(file => file.endsWith(".js"))) {
    const source = readFileSync(new URL(`../${file}`,import.meta.url),"utf8");
    for (const match of source.matchAll(/(?:from\s+|import\()"(\.\/[^"?]+\.js\?v=([^"]+))"/g)) {
      assert.equal(match[2],"20261003-evidence-6",`${file}: ${match[1]}`);
    }
  }
});
