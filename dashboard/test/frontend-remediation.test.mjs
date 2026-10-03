import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {JSDOM} from "jsdom";
import * as dataSource from "../data-source.js";
import * as coordination from "../coordinated-activity.js";
import * as terminology from "../terminology.js";
import * as decision from "../decision-view.js";
import * as staticDetail from "../static-detail.js";
import * as tokenState from "../token-state.js";
import * as scope from "../filter-scope.js";
import * as evaluation from "../evaluation-summary.js";
import * as robinhood from "../robinhood-state.js";
import * as accumulation from "../accumulation-evidence.js";
import * as gmgn from "../gmgn-context.js";

const now = Date.parse("2026-10-03T12:00:00Z"), checked = "2026-10-03T11:50:00Z";
const start = "2026-10-03T11:00:00Z", end = "2026-10-03T11:05:00Z";
const pool = patch => ({token_address:"mint", pool_address:"pool", symbol:"TEST", name:"Test token",
  dex:"pumpswap", age_hours:1, mcap_usd:100000, price_usd:0.02, liquidity_usd:10000,
  market_snapshot_at:Date.parse(checked) / 1000, current_market_verified_at:checked, ...patch});
const wallet = patch => ({owner:"buyer", attributed_tokens:100, buy_sol:1, current_retained_tokens:40,
  current_balance:40, checked_at:checked, is_holder:true, ...patch});
const thesis = patch => ({...pool(), cohort_id:"original", signal_at:end, signal_window_start:start,
  signal_window_end:end, last_checked_at:checked, updated_at:checked, next_check_at:"2026-10-03T12:50:00Z",
  status:"intact", source_tier:"watch", source_score:50, original_wallets:1, token_retention_pct:40,
  current_retained_supply_pct:0.4, cohort_wallets:[wallet()], ...patch});
const alert = patch => ({pool:pool(), lane:"reactivation", created_at:end, window_start:start, window_end:end,
  action_tier:"watch", events:[{signature:"buy", token_recipient:"buyer", signer:"buyer", time:start,
    sol_amount:1, token_amount:100, price_native:0.0001}], ...patch});
const payload = generated => ({ok:true, report:{generated_at:generated, alerts:[], signal_theses:[], config:{}},
  history:[], market:{}, deleted_tokens:{}});
const response = (body, ok = true) => ({ok, status:ok ? 200 : 503, json:async () => body});
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const tick = () => new Promise(done => setTimeout(done, 0));

function harness(t, {fetch = async () => { throw new Error("Unmocked network request"); },
  module = "app", url = "https://radar.test/", mobile = false, setup = () => {}, storage = {}} = {}) {
  const dom = new JSDOM(readFileSync(new URL("../index.html", import.meta.url), "utf8"), {url, pretendToBeVisual:true});
  const clock = {now};
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  Object.entries(storage).forEach(([key, value]) => dom.window.localStorage.setItem(key, value));
  dom.window.HTMLElement.prototype.scrollTo = () => {};
  dom.window.HTMLElement.prototype.scrollIntoView = () => {};
  dom.window.scrollTo = () => {};
  setup(dom.window);
  const context = vm.createContext({...dataSource, ...coordination, ...terminology, ...decision,
    ...staticDetail, ...tokenState, ...scope, ...evaluation, ...robinhood, ...accumulation, ...gmgn,
    decisionView:(token, config) => decision.decisionView(token, config, clock.now),
    resolveCurrentMarket:args => tokenState.resolveCurrentMarket({...args, now:clock.now}),
    isFresh:p => robinhood.isFresh(p, clock.now), walletFresh:row => robinhood.walletFresh(row, clock.now),
    marketFresh:row => robinhood.marketFresh(row, clock.now), reviewGroup:(row, p) => robinhood.reviewGroup(row, p, clock.now),
    document:dom.window.document, window:dom.window, localStorage:dom.window.localStorage,
    navigator:dom.window.navigator, Date:FixedDate, console:{log(){}, warn(){}, error(){}}, URL,
    AbortController, AbortSignal, Response, ReadableStream, setTimeout, clearTimeout, fetch,
    matchMedia:query => ({matches:query.includes("max-width") ? mobile : !mobile})});
  const source = readFileSync(new URL(`../${module}.js`, import.meta.url), "utf8");
  const body = (module === "app" ? source.slice(0, source.lastIndexOf("\nloadData().catch(")) : source)
    .replace(/^import\s[\s\S]*?;\n/gm, "");
  vm.runInContext(body, context);
  const names = module === "app"
    ? "state, buildTokenSignals, applyDashboardPayload, loadData, loadStaticData, fetchWithTimeout, ensureTokenDetail, detailLoadMessage, marketPhase, render, renderStatus, renderIntelligence, ensureIntelligence, renderNarratives, renderNarrativeTokenRows, applyDeletedTokenList, isTokenHidden, alertMatches, bindTokenHideActions, syncLocalDeletedTokens, openRadarToken, renderWalletRows, localMutation, runScan, persistTokenDeletion"
    : "state, load, render";
  const api = vm.runInContext(`({${names}})`, context);
  if (module === "app") {
    api.state.report = {generated_at:checked, alerts:[alert()], signal_theses:[thesis()], config:{}};
    api.state.workflow = "all";
    api.state.lane = "all";
    decision.REVIEW_QUEUES.forEach(q => api.state.expandedQueues.add(q.id));
  }
  t.after(() => dom.window.close());
  return {...api, dom, doc:dom.window.document, context, clock,
    noRender:() => vm.runInContext("render = () => {};", context)};
}

test("U01: report generations do not refresh stale registry quotes in any direct list", () => {
  for (const source of ["universe", "active", "summary"]) {
    const p = pool({source:"registry", _snapshot_source:source, _observed_at:checked,
      market_snapshot_at:Date.parse("2026-10-01T11:00:00Z") / 1000, market_snapshot_stale:true});
    const result = tokenState.resolveCurrentMarket({pool:p, now});
    assert.equal(result.isFresh, false);
    assert.equal(result.observedAt, "2026-10-01T11:00:00.000Z");
    assert.equal(result.source, "registry");
    assert.equal(result.priceUsd, null);
  }
});

test("U01: own quote provenance survives merging an older failed market cache", t => {
  const api = harness(t);
  api.state.report.universe = [pool({source:"dexscreener"})];
  api.state.market = {mint:{latest_seen_at:"2026-10-01T11:00:00Z", market_snapshot_stale:true,
    current_market_verified_at:"2026-10-01T11:00:00Z"}};
  const current = api.buildTokenSignals()[0].currentMarket;
  assert.equal(current.isFresh, true);
  assert.equal(current.observedAt, "2026-10-03T11:50:00.000Z");
  assert.equal(current.source, "dexscreener");
});

test("U01: missing registry clocks, expired own quotes and future quotes remain unverified", () => {
  for (const patch of [{source:"registry", market_snapshot_at:0, current_market_verified_at:null},
    {market_snapshot_at:Date.parse("2026-10-01T11:00:00Z") / 1000},
    {market_snapshot_at:Date.parse("2026-10-03T13:00:00Z") / 1000}]) {
    assert.equal(tokenState.resolveCurrentMarket({pool:pool({...patch, _snapshot_source:"universe", _observed_at:checked}), now}).isFresh, false);
  }
});

test("U02: failed remote refresh/cooldown retains the newer accepted snapshot and details", async t => {
  let requests = 0;
  const api = harness(t, {url:"https://audit.github.io/", fetch:async url => {
    if (url.startsWith("https://mock.invalid/")) { requests++; throw new Error("mock Worker failure"); }
    return response(payload("2026-10-03T10:00:00Z"));
  }});
  api.noRender();
  api.dom.window.SOLANA_RADAR_DATA_API_URL = "https://mock.invalid";
  api.applyDashboardPayload(payload(checked), "remote");
  api.state.tokenDetailLoadedKeys.add("mint");
  await api.loadData();
  await api.loadData();
  assert.equal(api.state.report.generated_at, checked);
  assert.equal(api.state.dataSource, "remote");
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"), true);
  assert.equal(requests, 1);
  assert.match(api.doc.querySelector("#statusRow").textContent, /previous snapshot retained/);
});

test("U02: late concurrent responses cannot override the latest request, even with a higher timestamp", async t => {
  const first = deferred(), second = deferred();
  let requests = 0;
  const api = harness(t, {fetch:async () => response(await (++requests === 1 ? first.promise : second.promise))});
  api.noRender();
  const olderRequest = api.loadData(), latestRequest = api.loadData();
  second.resolve(payload("2026-10-03T11:55:00Z"));
  await latestRequest;
  first.resolve(payload("2026-10-03T11:59:00Z"));
  await olderRequest;
  assert.equal(api.state.report.generated_at, "2026-10-03T11:55:00Z");
});

test("U02: initial fallback still displays before remote completes and equal static cannot strip remote data", async t => {
  const remote = deferred();
  const api = harness(t, {url:"https://audit.github.io/", fetch:async url =>
    url.startsWith("https://mock.invalid/") ? response(await remote.promise) : response(payload(checked))});
  api.noRender(); api.state.report = null;
  api.dom.window.SOLANA_RADAR_DATA_API_URL = "https://mock.invalid";
  const task = api.loadData();
  await tick();
  assert.equal(api.state.dataSource, "static");
  remote.resolve(payload(checked));
  await task;
  assert.equal(api.state.dataSource, "remote");
  assert.equal(api.applyDashboardPayload(payload(checked), "static"), false);
});

test("U03: stalled detail JSON body reaches error/retry and aborts the entire request", async t => {
  let stream, signal;
  const api = harness(t, {setup:window => { window.setTimeout = fn => setTimeout(fn, 5); window.clearTimeout = clearTimeout; },
    fetch:async (_url, options) => {
      signal = options.signal;
      return new Response(new ReadableStream({start(controller) { stream = controller; }}));
    }});
  api.state.dataSource = "static";
  api.state.selectedTokenKey = null;
  api.state.tokenDetailManifest = {generation:checked, files:{mint:`data/token-details/${"a".repeat(64)}.json`}};
  try {
    await api.ensureTokenDetail("mint");
    assert.equal(api.state.tokenDetailLoadingKeys.has("mint"), false);
    assert.equal(api.state.tokenDetailLoadedKeys.has("mint"), false);
    assert.equal(signal.aborted, true);
    assert.match(api.state.tokenDetailErrors.get("mint"), /timed out/);
    assert.match(api.detailLoadMessage("mint"), /Retry/);
  } finally { stream.error(new Error("mock body cleanup")); }
});

test("U03: stalled headers also have a finite deadline", async t => {
  const api = harness(t, {fetch:async () => new Promise(() => {})});
  await assert.rejects(api.fetchWithTimeout("mock:headers", {}, 5), /timed out/);
});

test("U04: missing, blank, boolean and invalid ATH ratios cannot create a green range", t => {
  const api = harness(t);
  for (const ratio of [null, undefined, "", " ", false, true, [], {}, NaN, Infinity, -1]) {
    assert.equal(api.marketPhase({athCurrentRatio:ratio}), null, String(ratio));
  }
  assert.equal(api.marketPhase({athCurrentRatio:0.1}).label, "Low range");
  assert.equal(api.marketPhase({athCurrentRatio:0.9}).label, "Near ATH");
});

test("U05: historical buy FX cannot produce SOL valuation or open PnL without a fresh native quote", t => {
  const api = harness(t);
  api.state.report.alerts = [alert({pool:pool({price_usd:0.01}), events:[{signature:"buy", token_recipient:"buyer",
    signer:"buyer", time:start, sol_amount:1, token_amount:10000, price_native:0.0001}]})];
  api.state.report.universe = [pool({price_usd:0.02})];
  api.state.report.signal_theses = [thesis({cohort_wallets:[wallet({attributed_tokens:10000, current_retained_tokens:10000,
    current_balance:10000, buy_sol:1})]})];
  const token = api.buildTokenSignals()[0], w = token.wallets[0];
  assert.equal(w.retention_fresh, true);
  assert.equal(w.avg_entry_native, 0.0001);
  assert.equal(w.current_value_sol, null);
  assert.equal(w.pnl_pct, null);
  assert.equal(w.pnl_sol, null);
  assert.equal(token.medianWalletPnl, null);
  const cells = JSDOM.fragment(api.renderWalletRows(token)).querySelectorAll("tbody td");
  assert.equal(cells[5].textContent, "-");
  assert.equal(cells[6].textContent, "-");
});

test("U06: out-of-scope historical Learning links stay in Learning and explain unavailable identity", t => {
  const api = harness(t, {mobile:true});
  api.state.report.config = {age_min_hours:0.5, age_max_hours:360};
  api.state.report.alerts.push(alert({pool:pool({token_address:"out-of-scope-mint", age_hours:744}),
    created_at:"2026-09-02T11:05:00Z", window_start:"2026-09-02T11:00:00Z", window_end:"2026-09-02T11:05:00Z"}));
  api.state.tab = "intelligence";
  api.state.selectedTokenKey = null;
  api.state.intelligence = {status:"ready", overview:{}, wallets:[], clusters:[],
    episodes:[{token_address:"out-of-scope-mint", symbol:"OLD", caught_at:"2026-09-02T11:05:00Z"}]};
  api.renderIntelligence();
  api.doc.querySelector(".inline-token-action").click();
  assert.equal(api.state.tab, "intelligence");
  assert.equal(api.state.selectedTokenKey, null);
  assert.equal(api.doc.querySelector(".token-detail"), null);
  assert.match(api.doc.querySelector("#appNotice").textContent, /details are unavailable/);
});

test("U06: in-scope Learning links expand a collapsed queue and open exactly the requested mint", t => {
  const api = harness(t, {mobile:true});
  api.state.expandedQueues.clear();
  api.state.tab = "intelligence";
  api.state.intelligence = {status:"ready", overview:{}, wallets:[], clusters:[], episodes:[{token_address:"mint"}]};
  api.renderIntelligence();
  api.doc.querySelector(".inline-token-action").click();
  assert.equal(api.state.selectedTokenKey, "mint");
  assert.equal(api.state.tab, "filters");
  assert.equal(api.state.detailTab, "wallets");
  assert.equal(api.state.mobileDetailOpen, true);
  assert.equal(api.doc.querySelector(".review-row.is-selected").dataset.tokenKey, "mint");
});

test("U06: a closed in-scope Learning token opens Closed instead of substituting an open position", t => {
  const api = harness(t);
  api.state.report.signal_theses = [thesis({status:"invalidated"})];
  api.state.report.alerts.push(alert({pool:pool({token_address:"other", pool_address:"other-pool", symbol:"OTHER"})}));
  api.state.tab = "intelligence";
  api.state.intelligence = {status:"ready", overview:{}, wallets:[], clusters:[], episodes:[{token_address:"mint"}]};
  api.renderIntelligence();
  api.doc.querySelector(".inline-token-action").click();
  assert.equal(api.state.selectedTokenKey, "mint");
  assert.equal(api.state.reviewQueue, "inactive");
  assert.equal(api.doc.querySelector(".review-row.is-selected").dataset.tokenKey, "mint");
  assert.match(api.doc.querySelector(".token-detail h2").textContent, /TEST/);
});

test("U06/U11: a closed in-scope Narrative link keeps its identity and opens mobile detail in Closed", t => {
  const api = harness(t, {mobile:true});
  api.state.report.signal_theses = [thesis({status:"invalidated"})];
  api.state.report.alerts.push(alert({pool:pool({token_address:"other", pool_address:"other-pool", symbol:"OTHER"})}));
  api.state.tab = "narratives"; api.render();
  api.doc.querySelector('.narrative-token-row[data-token-key="mint"]').click();
  assert.equal(api.state.selectedTokenKey, "mint");
  assert.equal(api.state.reviewQueue, "inactive");
  assert.equal(api.state.tab, "filters");
  assert.equal(api.state.mobileDetailOpen, true);
  assert.equal(api.doc.querySelector(".review-row.is-selected").dataset.tokenKey, "mint");
  assert.match(api.doc.querySelector(".review-workspace.is-detail-open .token-detail h2").textContent, /TEST/);
});

function rhPayload() {
  const token = `0x${"1".repeat(40)}`;
  return {chain_id:4663, generated_at:checked, status:"ready", tokens:[{key:`4663:${token}`, token,
    pool:`0x${"2".repeat(40)}`, protocol:"v3", symbol:"RH", name:"Robinhood test", status:"retained",
    checked_at:checked, first_observed_at:start, cohort_created_at:start, cohort_checks:2, attribution_complete:true,
    wallets:[{address:`0x${"3".repeat(40)}`, bought_raw:"100", retained_lower_bound_raw:"80", retained_upper_bound_raw:"100"}],
    market_checked_at:checked, fdv_usd:100000}]};
}

test("U07: idle timer expires Robinhood wallet/market labels without more network requests", async t => {
  const intervals = []; let calls = 0;
  const api = harness(t, {module:"robinhood", fetch:async () => { calls++; return response(rhPayload()); },
    setup:window => { window.setInterval = fn => { intervals.push(fn); return intervals.length; }; }});
  await tick();
  assert.match(api.doc.querySelector(".review-position").textContent, /supplychecked/);
  api.clock.now += 3 * 60 * 60 * 1000;
  intervals[0]();
  assert.match(api.doc.querySelector(".review-position").textContent, /check overdue/);
  assert.match(api.doc.querySelector(".review-reason").textContent, /fresh check/);
  assert.equal(api.doc.querySelector(".review-market strong").textContent, "Unverified");
  assert.equal(calls, 1);
});

test("U07: returning to a visible Robinhood page reevaluates its evidence clocks", async t => {
  const api = harness(t, {module:"robinhood", fetch:async () => response(rhPayload())});
  await tick();
  api.clock.now += 3 * 60 * 60 * 1000;
  api.doc.dispatchEvent(new api.dom.window.Event("visibilitychange"));
  assert.match(api.doc.querySelector(".review-position").textContent, /check overdue/);
});

test("U07: idle ticks that do not change freshness preserve controls and focus", async t => {
  let interval;
  const api = harness(t, {module:"robinhood", fetch:async () => response(rhPayload()),
    setup:window => { window.setInterval = fn => { interval = fn; return 1; }; }});
  await tick();
  const row = api.doc.querySelector(".review-row");
  row.focus(); interval();
  assert.equal(api.doc.querySelector(".review-row"), row);
  assert.equal(api.doc.activeElement, row);
});

test("U08: each optional compact/deletion/diagnostic error is isolated from a healthy legacy report", async t => {
  const paths = ["data/dashboard_fallback.json", "data/deleted_tokens.json", "data/scanner_status.json", "data/discovery_status.json"];
  for (const failed of paths) for (const type of ["network", "json", "http"]) {
    const api = harness(t, {fetch:async url => {
      if (url === failed) {
        if (type === "network") throw new Error("mock network failure");
        if (type === "json") return {ok:true, json:async () => { throw new SyntaxError("mock malformed JSON"); }};
        return response({}, false);
      }
      if (url === "data/dashboard_fallback.json") return response(null, false);
      return response(url === "data/latest_report.json" ? payload(checked).report : {});
    }});
    assert.equal((await api.loadStaticData()).report.generated_at, checked, `${failed}/${type}`);
  }
});

test("U08: optional body timeout does not prevent required legacy loading", async t => {
  const api = harness(t, {setup:window => { window.setTimeout = fn => setTimeout(fn, 5); window.clearTimeout = clearTimeout; },
    fetch:async url => url === "data/dashboard_fallback.json" ? {ok:true, json:async () => new Promise(() => {})}
      : response(url === "data/latest_report.json" ? payload(checked).report : {})});
  assert.equal((await api.loadStaticData()).report.generated_at, checked);
});

test("U09: successful legacy loading carries authoritative deletions into every token list", async t => {
  const api = harness(t, {fetch:async url => {
    if (url === "data/dashboard_fallback.json") return response(null, false);
    if (url === "data/latest_report.json") return response({generated_at:checked, alerts:[alert()], config:{}});
    return response(url === "data/deleted_tokens.json" ? {tokens:["mint"]} : {});
  }});
  const data = await api.loadStaticData();
  assert.deepEqual(data.deleted_tokens, {tokens:["mint"]});
  api.applyDashboardPayload(data, "static");
  assert.equal(api.isTokenHidden(pool()), true);
  assert.equal(api.alertMatches(alert()), false);
});

test("U09: unavailable or omitted deletion lists preserve the last authoritative set; explicit empty clears it", t => {
  const api = harness(t); api.noRender();
  api.applyDeletedTokenList({pools:["pool"]});
  for (const value of [null, undefined]) {
    api.applyDashboardPayload({...payload(checked), deleted_tokens:value}, "static");
    assert.equal(api.isTokenHidden(pool()), true);
  }
  api.applyDashboardPayload(payload(checked), "static");
  assert.equal(api.isTokenHidden(pool()), false);
});

test("U10: token-only, pool-only and entry deletion identities hide Radar and Events consistently", t => {
  const api = harness(t);
  for (const deleted of [{tokens:["mint"]}, {pools:["pool"]}, {entries:[{pool_address:"pool"}]},
    {entries:[{token_address:"mint"}]}]) {
    api.applyDeletedTokenList(deleted);
    assert.equal(api.isTokenHidden(pool()), true);
    assert.equal(api.alertMatches(alert()), false);
  }
  api.state.showHidden = true;
  assert.equal(api.alertMatches(alert()), true);
});

for (const mobile of [false, true]) test(`U11: Narrative token routing opens the current Radar detail (${mobile ? "mobile" : "desktop"})`, t => {
  const api = harness(t, {mobile});
  api.state.tab = "narratives"; api.render();
  api.doc.querySelector(".narrative-token-row").click();
  assert.equal(api.state.tab, "filters");
  assert.equal(api.state.selectedTokenKey, "mint");
  assert.equal(api.state.mobileDetailOpen, true);
  assert.equal(api.doc.querySelector('.tab[aria-selected="true"]').dataset.tab, "filters");
  assert.ok(api.doc.querySelector(".review-workspace.is-detail-open .token-detail"));
});

test("U11: selecting a Radar row preserves the user's current queue", t => {
  const api = harness(t);
  const token = api.buildTokenSignals()[0];
  api.state.reviewQueue = token.decision.queue;
  api.openRadarToken("mint");
  assert.equal(api.state.reviewQueue, token.decision.queue);
});

test("U12: stale Narrative capitalization is Unverified and missing fresh capitalization is unknown", t => {
  const api = harness(t);
  const token = api.buildTokenSignals()[0];
  token.currentMcap = null;
  for (const fresh of [false, true]) {
    token.currentMarket = {...token.currentMarket, isFresh:fresh};
    const cap = JSDOM.fragment(api.renderNarrativeTokenRows({tokens:[token]})).querySelector(".narrative-token-row").children[2].textContent;
    assert.equal(cap, fresh ? "-" : "Unverified");
    assert.notEqual(cap, "$0");
  }
});

test("U13: Narrative selectors are native keyboard buttons with selection semantics and retained focus", t => {
  const api = harness(t);
  api.state.report.alerts.push(alert({pool:pool({token_address:"other", pool_address:"other-pool", symbol:"OTHER"}),
    token_intel:{narrative:{primary:"Other group", secondary:[], score:50}}}));
  api.state.tab = "narratives"; api.render();
  const cards = [...api.doc.querySelectorAll(".narrative-card")];
  assert.ok(cards.length >= 2);
  for (const card of cards) {
    assert.equal(card.tagName, "BUTTON");
    assert.equal(card.type, "button");
    assert.equal(card.tabIndex, 0);
    assert.ok(card.hasAttribute("aria-pressed"));
  }
  const other = cards.find(card => card.getAttribute("aria-pressed") === "false");
  other.focus(); other.click();
  assert.equal(api.state.selectedNarrative, other.dataset.narrative);
  assert.equal(api.doc.activeElement.dataset.narrative, other.dataset.narrative);
  assert.equal(api.doc.activeElement.getAttribute("aria-pressed"), "true");
});

test("U14: failed Restore persists its action, exposes retry, and retry submits Restore rather than Delete", async t => {
  const actions = []; let fails = true;
  const api = harness(t, {fetch:async (url, options) => {
    if (url === "/api/session") return response({csrf_token:"mock-csrf"});
    actions.push(JSON.parse(options.body).action);
    return response(fails ? {error:"mock restore failure"} : {deleted_tokens:{tokens:[]}}, !fails);
  }});
  api.state.showHidden = true;
  api.state.hiddenTokenKeys.add("mint"); api.applyDeletedTokenList({tokens:["mint"]});
  api.doc.querySelector("#content").innerHTML = '<button class="token-hide-toggle" data-token-key="mint" data-hidden="true">Restore</button>';
  api.bindTokenHideActions(); api.doc.querySelector(".token-hide-toggle").click();
  await tick();
  assert.equal(api.state.hiddenTokenKeys.size, 0);
  assert.equal(api.state.serverDeletedTokenKeys.has("mint"), true);
  assert.equal(api.state.pendingTokenActions.get("mint").hidden, false);
  assert.match(api.doc.querySelector("#syncDeleted").textContent, /Retry pending changes/);
  assert.match(api.doc.querySelector("#appNotice").textContent, /Restore sync failed/);
  const stored = api.dom.window.localStorage.getItem("solana-radar:pending-token-actions:v1");
  const reopened = harness(t, {storage:{"solana-radar:pending-token-actions:v1":stored}});
  assert.equal(reopened.state.pendingTokenActions.get("mint").hidden, false);
  fails = false;
  await api.syncLocalDeletedTokens();
  assert.deepEqual(actions, ["restore", "restore"]);
  assert.equal(api.state.pendingTokenActions.size, 0);
  assert.equal(api.state.serverDeletedTokenKeys.size, 0);
  assert.equal(api.dom.window.localStorage.getItem("solana-radar:pending-token-actions:v1"), "[]");
});

test("U14: a failed Delete retains delete intent and both intents remain retryable without a token in the current scope", async t => {
  const api = harness(t, {fetch:async url => url === "/api/session" ? response({csrf_token:"mock-csrf"}) : response({error:"mock failure"}, false)});
  api.doc.querySelector("#content").innerHTML = '<button class="token-hide-toggle" data-token-key="mint" data-hidden="false">Delete</button>';
  api.dom.window.confirm = () => true;
  api.bindTokenHideActions(); api.doc.querySelector(".token-hide-toggle").click();
  await tick();
  assert.equal(api.state.pendingTokenActions.get("mint").hidden, true);
  api.state.report = payload(checked).report;
  await assert.rejects(api.syncLocalDeletedTokens(), /mock failure/);
  assert.equal(api.state.pendingTokenActions.get("mint").token.token_address, "mint");
  assert.ok(api.doc.querySelector("#syncDeleted"));
});

test("U15: failed initial Learning requests display unavailable, not unsupported evidence-absence claims", async t => {
  const api = harness(t, {fetch:async () => { throw new Error("mock Learning unavailable"); }});
  api.state.tab = "intelligence";
  await api.ensureIntelligence();
  const text = api.doc.querySelector("#content").textContent;
  assert.equal(api.state.intelligence.status, "error");
  assert.match(text, /Historical ledger is unavailable/);
  assert.match(text, /Wallet rankings unavailable/);
  assert.match(text, /Wallet links unavailable/);
  assert.doesNotMatch(text, /No wallet has enough|No independently evidenced|No historical signal episodes/);
});

test("U15: loading and successful-empty Learning remain distinct", t => {
  const api = harness(t);
  api.state.intelligence.status = "loading"; api.renderIntelligence();
  assert.match(api.doc.querySelector("#content").textContent, /Wallet rankings loading/);
  assert.doesNotMatch(api.doc.querySelector("#content").textContent, /No wallet has enough/);
  api.state.intelligence.status = "ready"; api.renderIntelligence();
  assert.match(api.doc.querySelector("#content").textContent, /No wallet has enough/);
});

test("U16: Robinhood unavailable sentinel renders dataset unavailable instead of normal empty results", async t => {
  const api = harness(t, {module:"robinhood", fetch:async () => response({chain_id:4663, status:"unavailable",
    tokens:[], errors:["No published scan"]})});
  await tick();
  const text = api.doc.querySelector("#content").textContent;
  assert.match(text, /Observations unavailable/);
  assert.doesNotMatch(text, /No new buy waves|No matching tokens/);
  assert.equal(api.doc.querySelector("#metrics").textContent, "");
  assert.ok(api.doc.querySelector("#retryData"));
});

test("U16: unavailable refresh retains a previously valid Robinhood dataset with an explicit warning", async t => {
  let unavailable = false;
  const api = harness(t, {module:"robinhood", fetch:async () => response(unavailable
    ? {chain_id:4663, status:"unavailable", tokens:[], errors:["No published scan"]} : rhPayload())});
  await tick();
  unavailable = true; await api.load();
  assert.equal(api.state.payload.tokens.length, 1);
  assert.match(api.doc.querySelector("#appNotice").textContent, /Previous results retained/);
  assert.match(api.doc.querySelector("#scannerSummary").textContent, /No published scan/);
});

test("A10 integration: local scan obtains a session and sends same-origin authenticated JSON with CSRF", async t => {
  const calls = [];
  const api = harness(t, {fetch:async (url, options) => {
    calls.push({url, options});
    if (url === "/api/session") return response({csrf_token:"mock-capability"});
    if (url.startsWith("/api/scan")) return response({ok:true});
    return response(payload(checked));
  }});
  api.noRender();
  await api.runScan();
  assert.deepEqual(calls.map(call => call.url), ["/api/session", "/api/scan?lane=reactivation", "/api/report"]);
  const session = calls[0].options, mutation = calls[1].options;
  assert.equal(session.credentials, "same-origin");
  assert.equal(mutation.method, "POST");
  assert.equal(mutation.credentials, "same-origin");
  assert.equal(mutation.headers["content-type"], "application/json");
  assert.equal(mutation.headers["X-Radar-CSRF"], "mock-capability");
  assert.equal(mutation.body, "{}");
  assert.equal(Object.hasOwn(mutation.headers, "Origin"), false);
});

test("A10 integration: local deletions reuse the session and failed CSRF causes a fresh session on manual retry", async t => {
  const calls = []; let sessions = 0, rejected = true;
  const api = harness(t, {fetch:async (url, options) => {
    calls.push({url, options});
    if (url === "/api/session") return response({csrf_token:`mock-${++sessions}`});
    return rejected ? {ok:false, status:403, json:async () => ({error:"invalid_csrf_token"})}
      : response({deleted_tokens:{}});
  }});
  await assert.rejects(api.persistTokenDeletion({key:"mint", token_address:"mint"}, false), /invalid_csrf_token/);
  rejected = false;
  await api.persistTokenDeletion({key:"mint", token_address:"mint"}, false);
  await api.persistTokenDeletion({key:"mint", token_address:"mint"}, true);
  assert.equal(sessions, 2);
  const mutations = calls.filter(call => call.options.method === "POST");
  assert.deepEqual(mutations.map(call => call.options.headers["X-Radar-CSRF"]), ["mock-1", "mock-2", "mock-2"]);
  assert.deepEqual(mutations.map(call => JSON.parse(call.options.body).action), ["restore", "restore", "delete"]);
  assert.ok(mutations.every(call => call.options.credentials === "same-origin"));
});

test("A10 integration: missing session capabilities prevent POST and stalled sessions have a finite timeout", async t => {
  let posted = false;
  const invalid = harness(t, {fetch:async (_url, options) => {
    posted ||= options.method === "POST";
    return response({});
  }});
  await assert.rejects(invalid.localMutation("/api/deleted-token", {}), /session is unavailable/);
  assert.equal(posted, false);
  const stalled = harness(t, {setup:window => { window.setTimeout = fn => setTimeout(fn, 5); window.clearTimeout = clearTimeout; },
    fetch:async () => ({ok:true, json:async () => new Promise(() => {})})});
  await assert.rejects(stalled.localMutation("/api/scan", {}), /timed out/);
  assert.equal(stalled.state.localSessionPromise, null);
  assert.equal(stalled.state.localCsrfToken, null);
});

test("A10 integration: published Worker deletion requests remain unchanged and never fetch a local session", async t => {
  const calls = [];
  const api = harness(t, {url:"https://audit.github.io/", fetch:async (url, options) => {
    calls.push({url, options}); return response({deleted_tokens:{}});
  }});
  api.state.publishedDashboard = true;
  await api.persistTokenDeletion({key:"mint", token_address:"mint"}, true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /^https:\/\/.*workers\.dev\/deleted-token$/);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(Object.hasOwn(calls[0].options.headers, "X-Radar-CSRF"), false);
  await api.runScan();
  assert.equal(calls.length, 1);
});

test("I04 integration: pending summary-only detail is neither applied nor cached as loaded, and can be retried", async t => {
  let ready = false;
  const api = harness(t, {fetch:async () => response({ok:true, token_key:"mint", report_source_updated_at:checked,
    detail_status:ready ? "ready" : "pending", thesis:ready ? thesis() : thesis({cohort_wallets:undefined}),
    current_alerts:[], history:[]})});
  api.state.report.signal_theses = [thesis({cohort_wallets:undefined})];
  api.state.report.alerts = [alert({events:[]})];
  api.state.dataSource = "static";
  api.state.selectedTokenKey = null;
  api.state.tokenDetailManifest = {generation:checked, files:{mint:`data/token-details/${"a".repeat(64)}.json`}};
  await api.ensureTokenDetail("mint");
  assert.equal(api.state.tokenDetailLoadingKeys.has("mint"), false);
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"), false);
  assert.equal(api.state.tokenDetailCache.has("mint"), false);
  assert.equal(api.buildTokenSignals()[0].wallets.length, 0);
  assert.match(api.state.tokenDetailErrors.get("mint"), /pending/);
  assert.match(api.detailLoadMessage("mint"), /Retry/);
  assert.match(api.detailLoadMessage("mint"), /pending for this snapshot/);
  assert.equal(api.state.tokenDetailRetryAt.get("mint"), now + 5 * 60_000);
  ready = true; api.state.tokenDetailRetryAt.delete("mint");
  await api.ensureTokenDetail("mint");
  assert.equal(api.state.tokenDetailLoadedKeys.has("mint"), true);
  assert.equal(api.state.tokenDetailCache.get("mint").detail_status, "ready");
  assert.equal(api.buildTokenSignals()[0].wallets.length, 1);
});

test("I04 integration: pending status gates both exact-generation application and historical cache compatibility", () => {
  const t = thesis();
  const detail = {ok:true, detail_status:"pending", token_key:"mint", report_source_updated_at:checked, thesis:t};
  assert.equal(decision.canApplyDetail(detail, "mint", checked, checked, t), false);
  assert.equal(decision.sameDetailCohort(detail, "mint", t, checked), false);
  assert.equal(decision.canApplyDetail({...detail, detail_status:"ready"}, "mint", checked, checked, t), true);
});

test("cache integration: every versioned entry asset uses the unified remediation tag", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const tags = [...html.matchAll(/(?:href|src)="(?:[^"]+)\?v=([^"]+)"/g)].map(match => match[1]);
  assert.equal(tags.length, 5);
  assert.ok(tags.every(tag => tag === "20261003-audit-remediation-10"));
});

test("legacy migration integration: a fresh confirmed alert cannot upgrade the original unknown-sale thesis", t => {
  const api = harness(t);
  api.state.report.alerts = [alert({signal_confirmation:{status:"confirmed"}})];
  api.state.report.signal_theses = [thesis({original_sale_history_status:"unknown",
    signal_confirmation:{status:"candidate", reasons:["legacy original-sale history incomplete"]},
    balance_coverage_pct:100, token_balance_coverage_pct:100, cohort_wallet_coverage_pct:100,
    cohort_token_coverage_pct:100, supply_integrity:{status:"distributed", data_quality_status:"complete",
      checked_at:checked, evidence_version:2}})];
  const token = api.buildTokenSignals()[0];
  assert.equal(token.signalLifecycle.currentConfirmed, true);
  assert.equal(token.workflowStatus, "candidate");
  assert.equal(token.decision.queue, "holding");
  api.openRadarToken("mint");
  const text = api.doc.querySelector(".token-detail").textContent;
  assert.match(text, /Balance cap checked/);
  assert.match(text, /Original sale history unknown/);
  assert.match(text, /not proof that the original buys remain unsold/);
  assert.doesNotMatch(text, /Confirmed buying \+ retained balances/);
});

test("release transition integration: unmigrated public theses stay candidate while new tracked v3 cohorts can be Ready", t => {
  const api = harness(t);
  api.state.report.alerts = [alert({signal_confirmation:{status:"confirmed"}})];
  const original = thesis({signal_confirmation:{status:"confirmed"},
    balance_coverage_pct:100, token_balance_coverage_pct:100, cohort_wallet_coverage_pct:100,
    cohort_token_coverage_pct:100, supply_integrity:{status:"distributed", data_quality_status:"complete",
      checked_at:checked, evidence_version:2}});
  for (const patch of [{}, {retention_evidence_version:2},
    {retention_evidence_version:3}, {retention_evidence_version:2, original_sale_history_status:"tracked_from_capture"}]) {
    api.state.report.signal_theses = [{...original, ...patch}];
    const token = api.buildTokenSignals()[0];
    assert.equal(token.signalLifecycle.currentConfirmed, true);
    assert.equal(token.workflowStatus, "candidate");
    assert.equal(token.decision.queue, "holding");
    assert.equal(token.decision.saleHistoryUnknown, true);
  }
  api.state.report.signal_theses = [{...original, retention_evidence_version:3, original_sale_history_status:"tracked_from_capture"}];
  const modern = api.buildTokenSignals()[0];
  assert.equal(modern.decision.saleHistoryUnknown, false);
  assert.equal(modern.decision.queue, "review");
  assert.equal(modern.workflowStatus, "watch");
});
