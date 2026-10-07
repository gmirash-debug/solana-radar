import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {JSDOM} from "jsdom";
import * as dataSource from "../data-source.js";
import * as coordination from "../coordinated-activity.js";
import * as terms from "../terminology.js";
import * as decision from "../decision-view.js";
import * as staticDetail from "../static-detail.js";
import * as tokenState from "../token-state.js";
import * as filterScope from "../filter-scope.js";
import * as r2Budget from "../r2-budget-view.js";
import * as walletActivity from "../wallet-activity-view.js";

const now = Date.parse("2026-10-08T12:00:00Z"), checked = "2026-10-08T11:50:00Z";
const start = "2026-10-08T11:00:00Z", end = "2026-10-08T11:05:00Z";
const progress = "2026-10-01T11:00:00Z", attempt = "2026-10-08T11:40:00Z", retry = "2026-10-09T12:00:00Z";
const statuses = ["stalled", "archive_pending", "archived"];
const labels = ["History stalled", "History archive pending", "History archived"];
const pool = {token_address:"mint", pool_address:"pool", symbol:"TEST", name:"Test token", dex:"pumpswap",
  age_hours:40, mcap_usd:100000, price_usd:0.02, liquidity_usd:10000,
  market_snapshot_at:Date.parse(checked) / 1000, current_market_verified_at:checked};
const health = patch => ({version:1, status:"stalled", last_progress_at:progress, last_attempt_at:attempt,
  stalled_days:7.25, next_history_retry_at:retry, repeat_error_count:12, last_error_category:"provider_timeout",
  hot_bytes:1234567, ...patch});
const storage = patch => ({version:1, unresolved_count:1234, stalled_count:17, archive_pending_count:3,
  archived_count:9, oldest_stalled_days:7.25, hot_bytes:1234567, warning:true, warnings:["Stalled investigations need review"], ...patch});

function fixture(t, patch = {}) {
  const dom = new JSDOM(readFileSync(new URL("../index.html", import.meta.url), "utf8"),
    {url:"https://radar.test/", pretendToBeVisual:true});
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({...dataSource, ...coordination, ...terms, ...decision, ...staticDetail,
    ...tokenState, ...filterScope, ...r2Budget, ...walletActivity,
    decisionView:(token, config) => decision.decisionView(token, config, now),
    resolveCurrentMarket:args => tokenState.resolveCurrentMarket({...args, now}),
    document:dom.window.document, window:dom.window, localStorage:dom.window.localStorage,
    Date:FixedDate, console, URL, matchMedia:() => ({matches:false}),
    fetch:() => { throw new Error("No network requests allowed"); }});
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  // Exercise the real dashboard functions without startup requests or polling.
  const body = source.slice(0, source.lastIndexOf("\nloadData().catch("))
    .replace(/^import\s[\s\S]*?;\n/gm, "");
  vm.runInContext(body, context);
  const api = vm.runInContext(`({state, buildTokenSignals, renderReviewRow, renderTokenRow, renderTokenDetail,
    renderWalletRows, renderStatus, renderAnalysisHealthBadge, renderAnalysisHealthDetails, terminology})`, context);
  api.state.report = {generated_at:checked, config:{}, stats:{scan_health:{status:"healthy"}},
    alerts:[{pool, lane:"reactivation", created_at:end, window_start:start, window_end:end, action_tier:"actionable",
      signal_confirmation:{status:"confirmed"}, events:[{signature:"buy", token_recipient:"buyer", signer:"buyer",
        time:start, sol_amount:1, token_amount:100, price_native:0.0001}]}],
    signal_theses:[{...pool, cohort_id:"original", signal_at:end, signal_window_start:start, signal_window_end:end,
      last_checked_at:checked, updated_at:checked, next_check_at:"2026-10-08T12:50:00Z", status:"intact",
      source_tier:"actionable", source_score:70, original_wallets:1, holders_remaining:1,
      token_retention_pct:80, current_retained_supply_pct:0.8, retention_evidence_version:3,
      original_sale_history_status:"tracked_from_capture", signal_confirmation:{status:"confirmed"},
      balance_coverage_pct:100, token_balance_coverage_pct:100, cohort_wallet_coverage_pct:100, cohort_token_coverage_pct:100,
      supply_integrity:{status:"distributed", data_quality_status:"complete", evidence_version:2, checked_at:checked},
      cohort_wallets:[{owner:"buyer", attributed_tokens:100, buy_sol:1, current_retained_tokens:80,
        current_balance:80, checked_at:checked, is_holder:true}], ...patch}]};
  t.after(() => { api.terminology.destroy(); dom.window.close(); });
  return {...api, dom, doc:dom.window.document, thesis:api.state.report.signal_theses[0]};
}

function truth(token) {
  return JSON.stringify({decision:token.decision, tier:token.currentSignalTier, workflow:token.workflowStatus,
    lifecycle:token.lifecycleStatus, dataStatus:token.dataStatus, signalLifecycle:token.signalLifecycle,
    retained:token.signalThesis.token_retention_pct, balance:token.signalThesis.cohort_wallets[0].current_balance});
}

test("stalled and archived history do not demote current confirmed signals or disable valid balances", t => {
  const api = fixture(t);
  const baseline = api.buildTokenSignals()[0];
  assert.equal(baseline.decision.queue, "review");
  assert.equal(baseline.decision.fresh, true);
  const before = truth(baseline), wallets = api.renderWalletRows(baseline);
  for (const status of statuses) {
    api.thesis.analysis_health = health({status});
    const token = api.buildTokenSignals()[0];
    const saved = JSON.stringify(api.thesis);
    assert.equal(truth(token), before, status);
    assert.equal(api.renderWalletRows(token), wallets, status);
    api.renderTokenDetail(token);
    api.renderReviewRow(token);
    assert.equal(JSON.stringify(api.thesis), saved, status);
  }
});

test("unknown original sale history stays unconfirmed while checked balance caps remain usable", t => {
  const api = fixture(t, {status:"unknown", original_sale_history_status:"unknown", balance_status:"present",
    outflow_evidence:{balance_check_complete:true}});
  const baseline = api.buildTokenSignals()[0], before = truth(baseline);
  assert.equal(baseline.decision.queue, "holding");
  assert.equal(baseline.decision.label, "Balance cap checked");
  assert.equal(baseline.decision.confirmation, "Original sale history unknown");
  for (const status of statuses) {
    api.thesis.analysis_health = health({status});
    const token = api.buildTokenSignals()[0];
    assert.equal(truth(token), before, status);
    assert.equal(token.decision.fresh, true);
    assert.match(api.renderTokenDetail(token), /Unknown; checked balances are only a cap/);
    assert.doesNotMatch(api.renderReviewRow(token), /Confirmed buying|Confirmed burst/);
  }
});

test("a recent verification attempt cannot refresh stale balances or promote a legacy thesis", t => {
  for (const legacy of [false, true]) {
    const api = fixture(t, {last_checked_at:progress, next_check_at:null,
      ...(legacy ? {retention_evidence_version:undefined, original_sale_history_status:undefined} : {})});
    const baseline = api.buildTokenSignals()[0], before = truth(baseline);
    assert.equal(baseline.decision.fresh, false);
    assert.equal(baseline.decision.retained, 80);
    assert.notEqual(baseline.decision.queue, "review");
    for (const status of statuses) {
      api.thesis.analysis_health = health({status});
      const token = api.buildTokenSignals()[0];
      assert.equal(truth(token), before, status);
      assert.match(api.renderReviewRow(token), /check overdue/);
      assert.equal(token.decision.checkedAt, progress);
    }
  }
});

test("missing balance evidence remains unknown, not zero or fresh after a recovery attempt", t => {
  const api = fixture(t, {last_checked_at:null, next_check_at:null, token_retention_pct:null,
    current_retained_supply_pct:null, analysis_health:health()});
  const token = api.buildTokenSignals()[0];
  assert.equal(token.decision.queue, "verification");
  assert.equal(token.decision.fresh, false);
  assert.equal(token.decision.retained, null);
  assert.match(api.renderReviewRow(token), /Unknown/);
  assert.doesNotMatch(api.renderTokenDetail(token), /<meter class="retention-meter/);
});

test("history badges do not reopen invalidated positions or change known outflow", t => {
  for (const status of ["invalidated", "weakening"]) {
    const api = fixture(t, {status}), before = truth(api.buildTokenSignals()[0]);
    for (const historyStatus of statuses) {
      api.thesis.analysis_health = health({status:historyStatus});
      assert.equal(truth(api.buildTokenSignals()[0]), before);
    }
  }
});

test("all history states appear in existing rows and the selected card with separate evidence, attempt and retry times", t => {
  const api = fixture(t);
  for (const [index, status] of statuses.entries()) {
    api.thesis.analysis_health = health({status});
    const token = api.buildTokenSignals()[0];
    api.doc.querySelector("#content").innerHTML = api.renderReviewRow(token) + api.renderTokenDetail(token);
    const badges = api.doc.querySelectorAll(".analysis-health-badge");
    assert.equal(badges.length, 2);
    badges.forEach(badge => assert.equal(badge.textContent, labels[index]));
    const rows = [...api.doc.querySelectorAll(".analysis-health-row")];
    assert.deepEqual(rows.map(row => row.firstElementChild.textContent),
      ["Last new evidence", "Last verification attempt", "Next history retry"]);
    assert.deepEqual(rows.map(row => row.querySelector("time").dateTime), [progress, attempt, retry]);
    assert.match(rows[0].textContent, /7\.25d without progress/);
    assert.match(rows[1].textContent, /12 repeated errors \/ provider_timeout/);
    assert.match(api.renderTokenRow(token), new RegExp(labels[index]));
  }
});

test("Russian tooltips explain missing history, preserved balances and continuing checks without nested row buttons", t => {
  const api = fixture(t);
  for (const [index, status] of statuses.entries()) {
    api.thesis.analysis_health = health({status});
    const token = api.buildTokenSignals()[0], root = api.doc.querySelector("#content");
    root.innerHTML = api.renderReviewRow(token) + api.renderTokenDetail(token);
    api.terminology.refresh(root);
    api.terminology.refresh(root);
    const badge = root.querySelector(".token-detail .analysis-health-badge .term-trigger");
    assert.equal(badge.textContent, labels[index]);
    assert.equal(terms.termForLabel(labels[index]), badge.dataset.term);
    badge.focus();
    const popup = api.doc.querySelector('[role="tooltip"]');
    assert.equal(popup.hidden, false);
    assert.equal(popup.lang, "ru");
    assert.match(popup.textContent, /Отсутствующая история не означает продажу/);
    assert.match(popup.textContent, /последние достоверные балансы сохранены без изменений/);
    assert.match(popup.textContent, /Проверки балансов продолжаются/);
    assert.equal(badge.getAttribute("aria-describedby"), popup.id);
    assert.equal(root.querySelectorAll("button button").length, 0);
    assert.match(root.querySelector(".review-row .analysis-health-badge").title, /не означает продажу/);
    badge.dispatchEvent(new api.dom.window.KeyboardEvent("keydown", {key:"Escape", bubbles:true}));
    assert.equal(popup.hidden, true);
    for (const id of ["new_evidence", "verification_attempt", "history_retry"]) assert.ok(root.querySelector(`[data-term="${id}"]`));
  }
});

test("new evidence uses the supplied balance-or-history progress time, never an unchanged fresh balance check", t => {
  const api = fixture(t, {analysis_health:health()}), root = api.doc.querySelector("#content");
  root.innerHTML = api.renderAnalysisHealthDetails(api.buildTokenSignals()[0]);
  assert.equal(root.querySelector(".analysis-health-row time").dateTime, progress);
  assert.equal(api.buildTokenSignals()[0].decision.checkedAt, checked);
  api.thesis.analysis_health = health({last_progress_at:checked, stalled_days:0});
  root.innerHTML = api.renderAnalysisHealthDetails(api.buildTokenSignals()[0]);
  assert.equal(root.querySelector(".analysis-health-row time").dateTime, checked);
  assert.match(root.textContent, /0d without progress/);
  assert.match(terms.TERMS.new_evidence.text, /по балансам.*операций истории/);
  assert.match(terms.TERMS.new_evidence.note, /Свежая повторная проверка с прежними балансами не продвигает это время/);
  assert.match(terms.TERMS.verification_attempt.text, /проверить балансы или восстановить историю/);
  assert.match(terms.TERMS.history_retry.text, /попытки восстановить недостающую историю/);
});

test("null, invalid and future evidence times never inherit a fresh report or attempt timestamp", t => {
  const api = fixture(t);
  for (const value of [null, undefined, "", "not a date", true, 0, {}, "<script>alert(1)</script>", "2026-10-10T00:00:00Z"]) {
    api.thesis.analysis_health = health({last_progress_at:value, last_attempt_at:value, next_history_retry_at:null});
    const token = api.buildTokenSignals()[0], root = api.doc.querySelector("#content");
    root.innerHTML = api.renderAnalysisHealthDetails(token);
    assert.equal(root.querySelectorAll("time").length, 0);
    assert.equal((root.textContent.match(/Not recorded/g) || []).length, 2);
    assert.doesNotMatch(root.textContent, /Next history retry|fresh|Checked/);
    assert.equal(token.decision.checkedAt, checked);
    assert.equal(token.decision.fresh, true);
  }
  for (const value of [null, undefined, "", "not a date", false, 0, {}]) {
    api.thesis.analysis_health = health({next_history_retry_at:value});
    assert.doesNotMatch(api.renderAnalysisHealthDetails(api.buildTokenSignals()[0]), /Next history retry/);
  }
});

test("absent, active and unsupported health fields preserve older dashboard output", t => {
  const api = fixture(t), token = api.buildTokenSignals()[0];
  const baseline = api.renderTokenDetail(token), row = api.renderReviewRow(token);
  for (const value of [undefined, null, {}, [], {version:1, status:"active"}, health({version:2}),
    health({status:"unknown"}), health({status:"__proto__"})]) {
    api.thesis.analysis_health = value;
    assert.equal(api.renderAnalysisHealthBadge(token), "");
    assert.equal(api.renderAnalysisHealthDetails(token), "");
    assert.equal(api.renderTokenDetail(token), baseline);
    assert.equal(api.renderReviewRow(token), row);
  }
});

test("investigation storage warnings stay in the health area and expose exact counts, bytes, age and reasons", t => {
  const api = fixture(t);
  api.state.report.storage_health = storage();
  api.renderStatus();
  const warning = api.doc.querySelector("#statusRow .storage-health-warning");
  assert.ok(warning);
  assert.match(warning.textContent, /1234 unresolved \/ 17 stalled \/ 3 archive pending \/ 9 archived/);
  assert.match(warning.textContent, /hot 1,234,567 bytes \/ oldest stalled 7\.25d/);
  assert.match(warning.textContent, /^Investigation storage warning:/);
  assert.equal(warning.title, "Stalled investigations need review");
  assert.equal(api.doc.querySelectorAll(".storage-health-warning").length, 1);
  assert.doesNotMatch(api.renderTokenDetail(api.buildTokenSignals()[0]), /Investigation storage warning/);
  for (const value of [undefined, null, {}, storage({warning:false}), storage({warning:"true"}), storage({version:2})]) {
    api.state.report.storage_health = value;
    api.renderStatus();
    assert.equal(api.doc.querySelectorAll(".storage-health-warning").length, 0);
  }
});

test("zero storage counts remain exact; missing or invalid metrics are unknown rather than invented zeros", t => {
  const api = fixture(t);
  api.state.report.storage_health = storage({unresolved_count:0, stalled_count:0, archive_pending_count:0,
    archived_count:0, hot_bytes:0, oldest_stalled_days:0});
  api.renderStatus();
  let warning = api.doc.querySelector(".storage-health-warning");
  assert.match(warning.textContent, /^Investigation storage warning:/);
  assert.doesNotMatch(warning.textContent, /growth/i);
  assert.match(warning.textContent, /0 unresolved \/ 0 stalled \/ 0 archive pending \/ 0 archived \/ hot 0 bytes \/ oldest stalled 0d/);
  api.state.report.storage_health = storage({unresolved_count:null, stalled_count:-1, archive_pending_count:1.5,
    archived_count:false, hot_bytes:Infinity, oldest_stalled_days:null});
  api.renderStatus();
  warning = api.doc.querySelector(".storage-health-warning");
  assert.match(warning.textContent, /unknown unresolved \/ unknown stalled \/ unknown archive pending \/ unknown archived \/ hot unknown bytes \/ oldest stalled unknown/);
});

test("health values and warning strings cannot inject markup, attributes or archive links", t => {
  const api = fixture(t), attack = '\"><img src=x onerror="alert(1)"><script>alert(1)</script>';
  api.thesis.analysis_health = health({last_error_category:attack, archive_ref:"javascript:alert(1)",
    stalled_days:attack, repeat_error_count:attack, last_progress_at:attack, next_history_retry_at:attack});
  const root = api.doc.querySelector("#content");
  root.innerHTML = api.renderAnalysisHealthDetails(api.buildTokenSignals()[0]);
  assert.equal(root.querySelectorAll("img, script, [onerror], a").length, 0);
  assert.match(root.textContent, /<img src=x/);
  assert.doesNotMatch(root.innerHTML, /javascript:/);
  api.state.report.storage_health = storage({warnings:[attack, null, {text:"not a safe string"}], hot_bytes:attack});
  api.renderStatus();
  const status = api.doc.querySelector("#statusRow");
  assert.equal(status.querySelectorAll("img, script, [onerror]").length, 0);
  assert.equal(status.querySelector(".storage-health-warning").title, attack);
  assert.match(status.textContent, /hot unknown bytes/);
});

test("long diagnostics and badges can wrap; explanation placement stays within a narrow viewport", () => {
  const css = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  for (const selector of ["\\.status-pill\\.storage-health-warning", "\\.analysis-health-badge"]) {
    const rule = css.match(new RegExp(`${selector}\\s*\\{([^}]+)\\}`))[1];
    assert.match(rule, /min-width: 0/);
    assert.match(rule, /max-width: 100%/);
    assert.match(rule, /white-space: normal/);
    assert.match(rule, /overflow-wrap: anywhere/);
  }
  assert.match(css, /\.analysis-health-row > span\s*\{[^}]*min-width: 0;[^}]*overflow-wrap: anywhere;/);
  const position = terms.tooltipPosition({left:280, top:700, bottom:730}, {width:340, height:290}, {width:320, height:740});
  assert.equal(position.width, 296);
  assert.ok(position.left >= 12 && position.left + position.width <= 308);
  assert.ok(position.top >= 12 && position.top + 290 <= 728);
});
