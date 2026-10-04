import test from "node:test";
import assert from "node:assert/strict";
import { decisionView, matchesReviewQueue, compareReviewTokens, canApplyDetail, sameDetailCohort, retentionBound, numeric, originalSaleHistoryUnknown } from "../decision-view.js";

test("all position and supply labels preserve bounds, tiny holdings and unknown values", () => {
  assert.equal(retentionBound(85), "\u226485%");
  assert.equal(retentionBound(3.91,2), "\u22643.91%");
  assert.equal(retentionBound(0.02), "<1%");
  assert.equal(retentionBound(0.0002,2), "<0.01%");
  assert.equal(retentionBound(0), "0%");
  assert.equal(retentionBound(85.2), "\u226486%");
  assert.equal(retentionBound(3.911,2), "\u22643.92%");
  for (const value of [null, undefined, false, "", NaN, Infinity, -1, 101]) assert.equal(retentionBound(value), "Unknown");
});
test("blank, boolean and structured values are not numeric evidence", () => {
  for (const value of ["  ", "\t", false, true, [], {}, [0]]) {
    assert.equal(numeric(value), null);
    assert.equal(retentionBound(value), "Unknown");
  }
  assert.equal(numeric("0"), 0);
});

const now = Date.parse("2026-09-04T21:30:00Z");
const checked = "2026-09-04T21:15:00Z";
function token(overrides = {}) {
  return {
    key: "mint", dataStatus: "current", lifecycleStatus: "holding", currentSignalTier: "watch",
    signalLifecycle: { currentConfirmed: true }, currentSignalAlerts: [{ signal_confirmation: { status: "confirmed" } }],
    currentMarket: { isFresh: true }, supplyIntegrity: { status: "distributed", data_quality_status: "complete", checked_at: checked, evidence_version: 2 },
    signalThesis: { status: "intact", token_retention_pct: 80, current_retained_supply_pct: 4,
      last_checked_at: checked, next_check_at: "2026-09-04T22:15:00Z", balance_coverage_pct: 100,
      token_balance_coverage_pct: 100, cohort_wallet_coverage_pct: 100, cohort_token_coverage_pct: 100 },
    ...overrides,
  };
}
const view = (t) => decisionView(t, {}, now);

// A separate synthetic v3 fixture; legacy token() records deliberately lack sale-history proof.
function newV3Token(overrides = {}) {
  const t = token(overrides);
  if (t.signalThesis) t.signalThesis = {...t.signalThesis, retention_evidence_version:3,
    original_sale_history_status:"tracked_from_capture"};
  return t;
}

test("new v3 ready-to-review requires current confirmation and fresh evidence", () => {
  assert.equal(view(newV3Token()).queue, "review");
  assert.equal(view(newV3Token({ signalLifecycle: { currentConfirmed: false } })).queue, "holding");
  assert.equal(view(newV3Token({ currentMarket: { isFresh: false } })).queue, "holding");
  assert.equal(view(newV3Token({ dataStatus: "scanner_stale" })).queue, "holding");
  assert.equal(view(newV3Token({ supplyIntegrity: { status: "watch", data_quality_status: "complete" } })).queue, "holding");
});
test("old holder snapshots and legacy link proof cannot make a ready signal", () => {
  for (const patch of [{checked_at:"2026-09-04T18:00:00Z"}, {evidence_version:1}, {checked_at:null}]) {
    const t = newV3Token(); Object.assign(t.supplyIntegrity, patch);
    assert.equal(view(t).queue, "holding");
    assert.ok(view(t).blockers.length);
  }
});
test("holder freshness uses its own configured refresh interval", () => {
  const t = newV3Token(); t.supplyIntegrity.checked_at = "2026-09-04T19:30:00Z";
  assert.equal(view(t).queue, "review");
  assert.equal(decisionView(t, {supply_integrity_refresh_minutes:60}, now).queue, "holding");
});
test("market rotation stays a risk warning rather than accumulation readiness", () => {
  const t = newV3Token(); t.signalThesis.coordinated_activity = {metrics:{market_rotation_observations:1}};
  assert.equal(view(t).queue, "holding");
  assert.equal(view(t).rotation, true);
  assert.ok(view(t).blockers.some(reason => reason.includes("rotation")));
});
test("weakening is never concealed behind missing confirmation or overdue checks", () => {
  const t = token({ dataStatus: "check_needed", signalLifecycle: { currentConfirmed: false }, lifecycleStatus: "weakening" });
  t.signalThesis = { ...t.signalThesis, status: "weakening", token_retention_pct: 20, last_checked_at: "2026-09-04T10:00:00Z" };
  assert.equal(view(t).queue, "reducing");
  assert.equal(view(t).fresh, false);
  assert.match(view(t).reason, /20%/);
  assert.match(view(t).reason, /^Up to /);
});
test("legacy holding is not promoted into a confirmed entry", () => {
  const result = view(token({ signalLifecycle: { currentConfirmed: false } }));
  assert.equal(result.queue, "holding");
  assert.equal(result.confirmation, "Original sale history unknown");
  assert.ok(result.blockers.some((reason) => reason.includes("no confirmed")));
});

test("legacy migration: unknown original-sale history blocks Ready despite a newly confirmed alert", () => {
  const t = token();
  t.signalThesis.original_sale_history_status = "unknown";
  t.signalThesis.signal_confirmation = {status:"candidate", reasons:["legacy original-sale history incomplete"]};
  const result = view(t);
  assert.equal(result.queue, "holding");
  assert.equal(result.label, "Balance cap checked");
  assert.equal(result.confirmation, "Original sale history unknown");
  assert.match(result.reason, /^Up to 80%/);
  assert.match(result.reason, /history unknown/);
  assert.ok(result.blockers.some(reason => reason.includes("upper bound, not proof")));
});

test("legacy migration: an inconsistent confirmed thesis cannot override unknown original-sale history", () => {
  const t = token();
  t.signalThesis.original_sale_history_status = "unknown";
  t.signalThesis.signal_confirmation = {status:"confirmed"};
  assert.notEqual(view(t).queue, "review");
  t.signalThesis.status = "unknown";
  assert.equal(view(t).queue, "verification");
  assert.equal(view(t).confirmation, "Original sale history unknown");
});

test("checked balance observations escape the unknown-history dead end without becoming Ready", () => {
  const t = newV3Token({signalLifecycle:{currentConfirmed:false}});
  Object.assign(t.signalThesis, {status:"unknown", original_sale_history_status:"unknown",
    balance_status:"present", outflow_evidence:{balance_check_complete:true}});
  assert.equal(view(t).queue, "holding");
  assert.equal(view(t).confirmation, "Original sale history unknown");
  t.signalLifecycle.currentConfirmed = true;
  assert.notEqual(view(t).queue, "review");
  t.signalThesis.balance_coverage_pct = 66;
  assert.equal(view(t).queue, "verification");
  t.signalThesis.balance_coverage_pct = 100;
  t.signalThesis.cohort_wallet_coverage_pct = 66;
  assert.equal(view(t).queue, "verification");
});

test("outflow is visible but is never labelled a sale or a closed position", () => {
  const t = newV3Token();
  Object.assign(t.signalThesis, {status:"weakening", balance_status:"outflow", token_retention_pct:0});
  const result = view(t);
  assert.equal(result.queue, "reducing");
  assert.equal(result.label, "Original-wallet outflow");
  assert.match(result.reason, /wallet history queued/);
  assert.equal(matchesReviewQueue(result, "overview"), true);
});

test("new v3 tracked-from-capture histories retain normal readiness while unknown historical balances stay dated", () => {
  const t = newV3Token();
  assert.equal(view(t).queue, "review");
  t.signalThesis.original_sale_history_status = "unknown";
  t.signalThesis.last_checked_at = "2026-09-04T19:00:00Z";
  assert.equal(view(t).label, "Balance cap at last check");
  assert.match(view(t).reason, /check overdue/);
});
test("stale holding remains a dated observation", () => {
  const t = token();
  t.signalThesis.last_checked_at = "2026-09-04T19:00:00Z";
  assert.equal(view(t).label, "Balance cap at last check");
  assert.equal(view(t).queue, "holding");
});
test("unknown cohort with complete subset still needs original coverage", () => {
  const t = token();
  t.signalThesis = { ...t.signalThesis, status: "unknown", cohort_wallet_coverage_pct: 66.7, token_retention_pct: 1.5 };
  assert.equal(view(t).queue, "verification");
  assert.equal(view(t).balanceComplete, true);
  assert.equal(view(t).cohortComplete, false);
  assert.match(view(t).reason, /67%/);
});
test("missing, invalid and future observations cannot appear verified", () => {
  for (const retained of [null, undefined, "", false, NaN, Infinity, -1, 101]) {
    const t = token(); t.signalThesis.token_retention_pct = retained;
    assert.equal(view(t).retained, null);
    assert.notEqual(view(t).queue, "review");
  }
  const t = token(); t.signalThesis.last_checked_at = "2026-09-05T00:00:00Z";
  assert.equal(view(t).fresh, false);
});
test("zero retention is a real value, not missing data", () => {
  const t = token({ lifecycleStatus: "weakening" });
  t.signalThesis = { ...t.signalThesis, status: "weakening", token_retention_pct: 0, current_retained_supply_pct: 0 };
  assert.equal(view(t).retained, 0); assert.equal(view(t).supply, 0);
});
test("an intact status with measured zero cannot show holding or readiness", () => {
  const t = token(); t.signalThesis.token_retention_pct = 0;
  assert.equal(view(t).queue, "verification");
  t.signalLifecycle.currentConfirmed = false;
  assert.equal(view(t).queue, "verification");
});
test("new observations stay unconfirmed and closed positions stay out of overview", () => {
  const early = view(token({ signalThesis: null, lifecycleStatus: "pending", signalLifecycle: { currentConfirmed: false } }));
  assert.equal(early.queue, "early");
  assert.equal(early.saleHistoryUnknown, false);
  const closed = view(token({ lifecycleStatus: "closed" }));
  assert.equal(closed.queue, "inactive");
  assert.equal(matchesReviewQueue(closed, "overview"), false);
  assert.equal(matchesReviewQueue(closed, "inactive"), true);
});

test("release transition: absent metadata and legacy retention versions cannot become Ready from a fresh confirmed alert", () => {
  for (const patch of [{}, {retention_evidence_version:2},
    {retention_evidence_version:2, original_sale_history_status:"tracked_from_capture"},
    {retention_evidence_version:3}, {original_sale_history_status:"tracked_from_capture"},
    {retention_evidence_version:null, original_sale_history_status:"tracked_from_capture"},
    {retention_evidence_version:false, original_sale_history_status:"tracked_from_capture"},
    {retention_evidence_version:3, original_sale_history_status:"unknown"}]) {
    const t = token(); Object.assign(t.signalThesis, patch);
    t.signalThesis.signal_confirmation = {status:"confirmed"};
    const result = view(t);
    assert.equal(result.saleHistoryUnknown, true, JSON.stringify(patch));
    assert.equal(result.queue, "holding");
    assert.equal(result.label, "Balance cap checked");
    assert.equal(result.confirmation, "Original sale history unknown");
    assert.ok(result.blockers.some(reason => reason.includes("upper bound, not proof")));
  }
});

test("release transition: absent cohorts do not create unknown-sale claims; only explicit v3 tracking establishes history", () => {
  for (const absent of [null, undefined, {}, []]) assert.equal(originalSaleHistoryUnknown(absent), false);
  assert.equal(originalSaleHistoryUnknown({retention_evidence_version:3, original_sale_history_status:"tracked_from_capture"}), false);
  assert.equal(originalSaleHistoryUnknown({status:"intact"}), true);
  assert.equal(view(newV3Token()).saleHistoryUnknown, false);
  const legacy = token();
  assert.equal(Object.hasOwn(legacy.signalThesis, "retention_evidence_version"), false);
  assert.equal(Object.hasOwn(legacy.signalThesis, "original_sale_history_status"), false);
});
test("coverage thresholds follow the scanner configuration", () => {
  const t = token(); t.signalThesis.cohort_wallet_coverage_pct = 75;
  assert.equal(decisionView(t, {}, now).complete, true);
  assert.equal(decisionView(t, { signal_thesis_min_cohort_wallet_coverage_pct: 80 }, now).complete, false);
});
test("sorting is newest catch first with stable keys and missing dates last", () => {
  const items = [ {key: "old", firstSignalAt: "2026-09-01T00:00:00Z"}, {key: "none"},
    {key: "new", firstSignalAt: checked}, {key: "also-new", firstSignalAt: checked} ];
  assert.deepEqual(items.sort(compareReviewTokens).map((t) => t.key), ["also-new", "new", "old", "none"]);
  assert.ok(compareReviewTokens({key:"a"}, {key:"b"}) < 0);
});
test("retention sort does not treat zero as unknown", () => {
  const items = [{key:"unknown",decision:{retained:null}}, {key:"zero",decision:{retained:0}}, {key:"high",decision:{retained:80}}];
  assert.deepEqual(items.sort((a,b) => compareReviewTokens(a,b,"retained")).map(t=>t.key), ["high","zero","unknown"]);
});
test("old, mismatched and in-flight stale details cannot overwrite a snapshot", () => {
  const detail = { token_key: "mint", report_source_updated_at: checked, thesis: { last_checked_at: checked } };
  assert.equal(canApplyDetail(detail, "mint", checked, checked, {last_checked_at: checked}), true);
  assert.equal(canApplyDetail(detail, "other", checked, checked, {}), false);
  assert.equal(canApplyDetail(detail, "mint", checked, "2026-09-04T22:00:00Z", {}), false);
  assert.equal(canApplyDetail({...detail, report_source_updated_at:null}, "mint", checked, checked, {}), false);
  assert.equal(canApplyDetail({...detail, thesis:{last_checked_at:"2026-09-04T20:00:00Z"}}, "mint", checked, checked, {last_checked_at: checked}), false);
});
test("newer source reports and replacement cohorts cannot mix with the selected summary", () => {
  const thesis = {cohort_id:"original", signal_at:checked, last_checked_at:checked, updated_at:checked};
  const detail = {token_key:"mint", report_source_updated_at:checked, thesis};
  assert.equal(canApplyDetail(detail,"mint",checked,checked,thesis), true);
  for (const patch of [{cohort_id:"replacement"}, {cohort_id:null}, {signal_at:null},
    {last_checked_at:"2026-09-04T21:20:00Z"}, {updated_at:null}]) {
    assert.equal(canApplyDetail({...detail,thesis:{...thesis,...patch}},"mint",checked,checked,thesis), false);
  }
  assert.equal(canApplyDetail({...detail,report_source_updated_at:"2026-09-04T22:00:00Z"},"mint",checked,checked,thesis), false);
  assert.equal(canApplyDetail({...detail,report_source_updated_at:checked},"mint","invalid","invalid",{}), false);
});
test("only older matching-cohort details can survive as dated wallet observations", () => {
  const thesis = {cohort_id:"original",signal_at:checked};
  const detail = {token_key:"mint",report_source_updated_at:checked,thesis};
  assert.equal(sameDetailCohort(detail,"mint",thesis,"2026-09-04T22:00:00Z"),true);
  assert.equal(sameDetailCohort(detail,"mint",thesis,"2026-09-04T20:00:00Z"),false);
  assert.equal(sameDetailCohort({...detail,report_source_updated_at:null},"mint",thesis,checked),false);
  assert.equal(sameDetailCohort(detail,"mint",{...thesis,cohort_id:"replacement"},checked),false);
});
