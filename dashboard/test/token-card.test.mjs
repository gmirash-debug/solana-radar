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
import * as r2Budget from "../r2-budget-view.js";
import * as walletActivity from "../wallet-activity-view.js";

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
    ...staticDetail, ...tokenState, ...filterScope, ...r2Budget, ...walletActivity,
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
    renderObservedPositionActivity, renderWalletActivity, renderOverviewTab, renderStatus, renderFilters, filterMeta, renderEvidenceTab})`, context);
  api.state.report = {generated_at:checked, alerts, signal_theses:signalThesis ? [signalThesis] : [], config:{}};
  api.state.history = [];
  t.after(() => dom.window.close());
  return {...api, dom, context};
}

test("wallet activity exposes gross amounts, partial progress and signature-linked receipts", t => {
  const activity = {status:"backfilling",wallets_checked:1,wallets_total:4,pages_checked:3,
    wallet_status_counts:{retry:2},amounts_tokens:{sold:25,transferred:10,service:0,unclassified:0},
    amounts_supply_pct:{sold:2.5,transferred:1}};
  const api = fixture(t, {signalThesis:thesis({wallet_activity:activity,cohort_wallets:[cohortWallet({
    activity:{events:[{source_owner:"buyer",destination_owner:"recipient",signature:"sell-receipt",
      kind:"sold",tokens:25,timestamp:Date.parse(checked)/1000}]}})]})});
  const html = api.renderWalletActivity(api.buildTokenSignals()[0], true);
  assert.match(html,/1\/4 histories checked/);
  assert.match(html,/2\.50% of supply/);
  assert.match(html,/25 tokens/);
  assert.match(html,/Not established in checked subset/);
  assert.match(html,/2 wallet histories awaiting provider retry/);
  assert.match(html,/https:\/\/solscan\.io\/tx\/sell-receipt/);
  assert.match(html,/https:\/\/solscan\.io\/account\/recipient/);
  assert.match(html,/Not a breakdown of the original purchase/);
});

test("wallet activity does not turn a missing history into checked zero sales", t => {
  const api = fixture(t);
  const html = api.renderWalletActivity(api.buildTokenSignals()[0], true);
  assert.match(html,/History check queued/);
  assert.doesNotMatch(html,/None found|0\/0/);
});

test("partial pool observations cannot contradict decoded wallet sales in the main facts",t=>{
  const api=fixture(t,{signalThesis:thesis({wallet_activity:{status:"backfilling",wallets_checked:0,wallets_total:1,
    pages_checked:1,amounts_tokens:{sold:25},amounts_supply_pct:{sold:2.5}},
    outflow_evidence:{observed_sale_transactions:0,verified_sale_receipts:0,direct_transfer_transactions:1}})});
  const html=api.renderOverviewTab(api.buildTokenSignals()[0]);
  assert.doesNotMatch(html,/Observed sale trades|Verified sale receipts/);
  assert.match(html,/2\.50% of supply/);
  assert.match(html,/<details class="research-fold"><summary>Partial pool-window observations/);
  assert.match(html,/Pool-window transfers/);
  assert.match(html,/Missing events here do not contradict wallet-history receipts above/);
});

test("wallet preview holdings can wrap instead of being clipped on mobile",()=>{
  const css=readFileSync(new URL("../styles.css",import.meta.url),"utf8");
  const rule=css.match(/\.wallet-preview-row strong\s*\{([^}]+)\}/)[1];
  assert.match(rule,/min-width: 0/);
  assert.match(rule,/white-space: normal/);
  assert.match(css,/\.wallet-preview-row\s*\{\s*grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1fr\)/);
});

test("wallet evidence columns retain a readable width inside their scroll container",t=>{
  const api=fixture(t);
  assert.match(api.renderWalletRows(api.buildTokenSignals()[0]),/<table class="wallet-evidence-table">/);
  const css=readFileSync(new URL("../styles.css",import.meta.url),"utf8");
  assert.match(css,/\.wallet-evidence-table\s*\{\s*min-width: 760px;\s*table-layout: auto;/);
  assert.match(css,/\.table-wrap\s*\{[^}]*overflow-x: auto;/);
});

test("the radar and token criteria display the thirty-minute age minimum", t => {
  const api = fixture(t, {signalThesis:null});
  api.state.report.config = {age_min_hours:0.5, age_max_hours:360};
  api.renderFilters();
  assert.match(api.dom.window.document.querySelector(".radar-heading").textContent, /30m–15d/);
  assert.match(api.filterMeta("reactivation").criteria, /^30m-15d/);
});

test("GMGN radar exposes the configured cap range while keeping original tracked positions", t => {
  const api = fixture(t, {signalThesis:thesis({...pool, mcap_usd:900_000})});
  api.state.report.config = {discovery_source_mode:"gmgn_attention", age_min_hours:null,
    age_max_hours:null, mcap_min_usd:30_000, mcap_max_usd:500_000};
  api.state.report.stats = {gmgn_discovery:{tokens:100, mcap_filter:{eligible_tokens:25}}};
  api.renderFilters();
  assert.match(api.dom.window.document.querySelector(".radar-heading").textContent, /\$30k–\$500k mcap/);
  assert.match(api.filterMeta("reactivation").criteria, /\$30k–\$500k mcap/);
  assert.match(api.dom.window.document.querySelector("#metrics").textContent, /In cap range\s+25/);
  assert.equal(api.buildTokenSignals().length, 1);
});

test("GMGN discovery shows old non-pump tokens and escaped list membership with dated coverage", t => {
  const p = {...pool, dex:"raydium", age_hours:5000, gmgn_attention:{first_seen_at:start, last_seen_at:end,
    memberships:[{source:"trending", interval:"24h", rank:3},{source:"hot_searches", interval:"<script>", rank:4}]},
    candidate_analysis:{initial_hours:24, checked_at:checked, pending:true, scope:"standard_incremental",
      covered_ranges:[[Date.parse(start)/1000,Date.parse(end)/1000]]}};
  const api = fixture(t,{signalThesis:thesis({...p}),alerts:[alert({pool:p})]});
  api.state.report.config = {discovery_source_mode:"gmgn_attention",age_min_hours:null,age_max_hours:null};
  const tokens = api.buildTokenSignals();
  assert.equal(tokens.length,1);
  api.renderFilters();
  assert.match(api.dom.window.document.querySelector(".radar-heading").textContent,/GMGN Trending \+ Hot Searches/);
  const html = api.renderEvidenceTab(tokens[0],"","");
  assert.match(html,/Trending 24h #3/);
  assert.match(html,/Hot Searches &lt;script&gt; #4/);
  assert.match(html,/24h/);
  assert.match(html,/Incomplete \/ retry pending/);
  assert.ok(!html.includes("<script>"));
});

test("targeted checks retain a separate deep-scan date and storage-source label", t => {
  const api = fixture(t);
  api.state.report.scan_profile = "targeted";
  api.state.report.stats = {scanned_pools:6};
  api.state.dataSource = "remote";
  api.state.storageSource = "durable_snapshot";
  api.renderStatus();
  const doc = api.dom.window.document;
  assert.match(doc.querySelector("#subtitle").textContent, /^Last check /);
  assert.match(doc.querySelector("#statusRow").textContent, /targeted check: 6 pools/);
  assert.match(doc.querySelector("#statusRow").textContent, /deep scan not recorded yet/);
  assert.match(doc.querySelector("#statusRow").textContent, /durable snapshot/);
  assert.doesNotMatch(doc.querySelector("#statusRow").textContent, /live D1/);
  api.state.report.last_deep_scan_at = checked;
  api.renderStatus();
  assert.doesNotMatch(doc.querySelector("#statusRow").textContent, /deep scan not recorded yet/);
});

test("GMGN metrics separate candidates, selected pools, read heads and analysis", t => {
  const api = fixture(t);
  api.state.report.config = {discovery_source_mode:"gmgn_attention"};
  api.state.report.stats = {universe_pools:120,scanned_pools:4,gmgn_discovery:{tokens:277},
    scan_health:{selected_pools:40,head_sweep:{reactivation:{prepared:39}}}};
  api.renderFilters();
  const text = api.dom.window.document.querySelector("#metrics").textContent;
  assert.match(text,/GMGN candidates\s*277/);
  assert.match(text,/Selected pools\s*40/);
  assert.match(text,/History heads read\s*39/);
  assert.match(text,/Analyzed pools\s*4/);
});

test("a clean restart is not displayed as a successful fresh scan",t=>{
  const api=fixture(t,{signalThesis:null});
  api.state.scanStatus={status:"maintenance",running:false,last_success_at:null};
  api.state.report.scan_profile="storage_reset";
  api.renderStatus();
  assert.equal(api.dom.window.document.querySelector("#runScan").disabled,true);
  assert.match(api.dom.window.document.querySelector("#scannerSummary").textContent,/Scanner paused/);
  assert.match(api.dom.window.document.body.textContent,/Clean restart: first scan pending/);
  assert.equal(api.buildTokenSignals().length,0);
});

test("complete trading history remains distinct from pending wallet verification", t => {
  const p = {...pool,candidate_analysis:{initial_hours:6,checked_at:checked,pending:true,
    history_pending:false,evidence_pending:true,covered_ranges:[],scope:"probe"}};
  const api = fixture(t,{signalThesis:thesis({...p}),alerts:[alert({pool:p})]});
  const html = api.renderEvidenceTab(api.buildTokenSignals()[0],"","");
  assert.match(html,/History coverage<\/span><span>Checked/);
  assert.match(html,/Wallet verification/);
  assert.match(html,/Pending \/ parsed trading history retained/);
  assert.doesNotMatch(html,/Incomplete \/ retry pending/);
});

test("covered signal and fresh windows do not label an unrelated initial tail as incomplete", t => {
  const p = {...pool, candidate_analysis:{coverage_version:2, initial_hours:6,checked_at:checked,
    history_pending:true, initial_history_pending:true, pending_ranges:2,
    live_window_complete:true,signal_window_complete:true,live_window_start:start,live_window_end:end,
    covered_ranges:[[Date.parse(start)/1000,Date.parse(end)/1000]]}};
  const api = fixture(t,{signalThesis:thesis({...p}),alerts:[alert({pool:p})]});
  const html = api.renderEvidenceTab(api.buildTokenSignals()[0],"","");
  assert.match(html,/Fresh transaction window<\/span><span>Checked/);
  assert.match(html,/Original signal window<\/span><span>Checked/);
  assert.match(html,/Initial history<\/span><span>Still loading \/ 2 ranges pending/);
  assert.doesNotMatch(html,/History coverage<\/span><span>Incomplete/);
});

test("missing original coverage is not displayed as checked and interval rendering is bounded", t => {
  const p = {...pool,candidate_analysis:{coverage_version:2, live_window_complete:false,
    signal_window_complete:null, initial_history_pending:true, covered_ranges:Array.from({length:20},(_,i)=>[i+1,i+1])}};
  const api = fixture(t,{signalThesis:thesis({...p}),alerts:[alert({pool:p})]});
  const html = api.renderEvidenceTab(api.buildTokenSignals()[0],"","");
  assert.match(html,/Fresh transaction window<\/span><span>Incomplete/);
  assert.match(html,/Original signal window<\/span><span>Not established/);
  assert.match(html,/16 earlier ranges/);
});

test("metrics distinguish complete fresh windows from initial history still loading", t => {
  const api=fixture(t);
  api.state.report.config={discovery_source_mode:"gmgn_attention"};
  api.state.report.stats={scan_health:{live_fetch_pools:40,partial_live_windows:2,initial_history_pending_pools:30}};
  api.renderFilters();
  const text=api.dom.window.document.querySelector("#metrics").textContent;
  assert.match(text,/Fresh windows checked\s*38/);
  assert.match(text,/Initial history pending\s*30/);
});

test("failed RPC usage tracking is visible without claiming its quota was spent", t => {
  const api = fixture(t);
  api.state.report.stats = {rpc_providers:{helius:{status:"budget_ledger_unavailable",calls:{getHealth:1}}}};
  api.renderStatus();
  const text = api.dom.window.document.querySelector("#statusRow").textContent;
  assert.match(text,/Helius: usage tracking unavailable/);
  assert.doesNotMatch(text,/Helius blocked|Helius.*quota exhausted/);
});

test("saved current snapshots do not label a historical backlog as a cloud failure", t => {
  const api = fixture(t);
  api.state.scanStatus = {persistence:{status:"pending", pending:612,
    current_synced:true, durable_dashboard_synced:true, checkpoint:{ok:true}}};
  api.renderStatus();
  const row = api.dom.window.document.querySelector("#statusRow");
  assert.match(row.textContent, /Archive pending: 612/);
  assert.doesNotMatch(row.textContent, /Cloud save pending/);
  assert.match(row.innerHTML, /Current dashboard is saved/);
  assert.doesNotMatch(row.innerHTML, /Cloud storage unavailable/);
});

test("unconfirmed current publication keeps its cloud warning", t => {
  const api = fixture(t);
  api.state.scanStatus = {persistence:{status:"pending", pending:1,
    current_synced:true, durable_dashboard_synced:false, durable_dashboard_error:"upload deferred"}};
  api.renderStatus();
  const row = api.dom.window.document.querySelector("#statusRow");
  assert.match(row.textContent, /Cloud save pending: 1/);
  assert.match(row.innerHTML, /upload deferred/);
  assert.doesNotMatch(row.textContent, /Archive pending/);
});

test("a rejected private checkpoint remains a state warning even with a saved dashboard", t => {
  const api = fixture(t);
  api.state.scanStatus = {persistence:{status:"pending", pending:612,
    current_synced:true, durable_dashboard_synced:true, checkpoint:{ok:false,error:"checkpoint deferred"}}};
  api.renderStatus();
  const row = api.dom.window.document.querySelector("#statusRow");
  assert.match(row.textContent, /State save pending/);
  assert.match(row.innerHTML, /checkpoint deferred/);
  assert.doesNotMatch(row.textContent, /Archive pending/);
});

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
  assert.match(html, /src="radar-bootstrap\.js\?v=20261008-pool-history-v2"/);
  for (const file of readdirSync(new URL("../",import.meta.url)).filter(file => file.endsWith(".js"))) {
    const source = readFileSync(new URL(`../${file}`,import.meta.url),"utf8");
    for (const match of source.matchAll(/(?:from\s+|import\()"(\.\/[^"?]+\.js\?v=([^"]+))"/g)) {
      assert.equal(match[2],"20261008-pool-history-v2",`${file}: ${match[1]}`);
    }
  }
});
