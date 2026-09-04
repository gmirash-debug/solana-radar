import test from "node:test";
import assert from "node:assert/strict";
import { decisionView, matchesReviewQueue, compareReviewTokens, canApplyDetail } from "../decision-view.js";

const now = Date.parse("2026-09-04T21:30:00Z");
const checked = "2026-09-04T21:15:00Z";
function token(overrides = {}) {
  return {
    key: "mint", dataStatus: "current", lifecycleStatus: "holding", currentSignalTier: "watch",
    signalLifecycle: { currentConfirmed: true }, currentSignalAlerts: [{ signal_confirmation: { status: "confirmed" } }],
    currentMarket: { isFresh: true }, supplyIntegrity: { status: "distributed", data_quality_status: "complete" },
    signalThesis: { status: "intact", token_retention_pct: 80, current_retained_supply_pct: 4,
      last_checked_at: checked, next_check_at: "2026-09-04T22:15:00Z", balance_coverage_pct: 100,
      token_balance_coverage_pct: 100, cohort_wallet_coverage_pct: 100, cohort_token_coverage_pct: 100 },
    ...overrides,
  };
}
const view = (t) => decisionView(t, {}, now);

test("ready-to-review requires current confirmation and fresh evidence", () => {
  assert.equal(view(token()).queue, "review");
  assert.equal(view(token({ signalLifecycle: { currentConfirmed: false } })).queue, "holding");
  assert.equal(view(token({ currentMarket: { isFresh: false } })).queue, "holding");
  assert.equal(view(token({ dataStatus: "scanner_stale" })).queue, "holding");
  assert.equal(view(token({ supplyIntegrity: { status: "watch", data_quality_status: "complete" } })).queue, "holding");
});
test("weakening is never concealed behind missing confirmation or overdue checks", () => {
  const t = token({ dataStatus: "check_needed", signalLifecycle: { currentConfirmed: false }, lifecycleStatus: "weakening" });
  t.signalThesis = { ...t.signalThesis, status: "weakening", token_retention_pct: 20, last_checked_at: "2026-09-04T10:00:00Z" };
  assert.equal(view(t).queue, "reducing");
  assert.equal(view(t).fresh, false);
  assert.match(view(t).reason, /20%/);
});
test("legacy holding is not promoted into a confirmed entry", () => {
  const result = view(token({ signalLifecycle: { currentConfirmed: false } }));
  assert.equal(result.queue, "holding");
  assert.equal(result.confirmation, "Not confirmed");
  assert.ok(result.blockers.some((reason) => reason.includes("no confirmed")));
});
test("stale holding remains a dated observation", () => {
  const t = token();
  t.signalThesis.last_checked_at = "2026-09-04T19:00:00Z";
  assert.equal(view(t).label, "Held at last check");
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
test("new observations stay unconfirmed and closed positions stay out of overview", () => {
  const early = view(token({ signalThesis: null, lifecycleStatus: "pending", signalLifecycle: { currentConfirmed: false } }));
  assert.equal(early.queue, "early");
  const closed = view(token({ lifecycleStatus: "closed" }));
  assert.equal(closed.queue, "inactive");
  assert.equal(matchesReviewQueue(closed, "overview"), false);
  assert.equal(matchesReviewQueue(closed, "inactive"), true);
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
