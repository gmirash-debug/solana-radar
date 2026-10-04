import { walletActivityView } from "./wallet-activity-view.js?v=20261004-wallet-activity-v1";
// Presentation only: never upgrades the scanner's confirmation or lifecycle.
export const REVIEW_QUEUES = [
  { id: "review", label: "Ready to review", note: "Current confirmed signals with fresh cohort and market checks.", tone: "positive" },
  { id: "holding", label: "Balances checked", note: "Original-wallet balance bounds checked. Sale history may remain unknown; this is not confirmed accumulation or an entry signal.", tone: "neutral" },
  { id: "early", label: "Early observations", note: "New activity, not confirmed accumulation.", tone: "info" },
  { id: "reducing", label: "Outflow from original wallets", note: "Original-position balance caps declined. Sales, transfers and unresolved outflows are shown separately; common control is not established.", tone: "negative" },
  { id: "verification", label: "Needs data", note: "Insufficient evidence for a current conclusion.", tone: "muted" },
  { id: "inactive", label: "Closed", note: "The scanner invalidated the original accumulation thesis.", tone: "muted" },
];

export function numeric(value) {
  if (!["number", "string"].includes(typeof value) || (typeof value === "string" && !value.trim())) return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

export function originalSaleHistoryUnknown(thesis) {
  if (!thesis || typeof thesis !== "object" || Array.isArray(thesis) || !Object.keys(thesis).length) return false;
  const version = numeric(thesis.retention_evidence_version);
  if (version === null || version < 3) return true;
  if (thesis.original_sale_history_status === "tracked_from_capture") return false;
  const audit = thesis.wallet_activity;
  return thesis.original_sale_history_status !== "reconstructed_from_capture"
    || audit?.status !== "checked" || audit.interpretation_complete !== true
    || audit.wallet_coverage_pct !== 100 || (numeric(audit.token_coverage_pct) ?? 0) < 99.99
    || thesis.sale_history_recovery?.checked_at !== audit.checked_at;
}

function percent(value) {
  const n = numeric(value);
  return n !== null && n >= 0 && n <= 100 ? n : null;
}

export function retentionBound(value, digits = 0) {
  const amount = percent(value);
  if (amount === null) return "Unknown";
  if (amount === 0) return "0%";
  const cutoff = 10 ** -digits;
  if (amount < cutoff) return `<${cutoff}%`;
  const factor = 10 ** digits;
  const rounded = Number(amount.toFixed(digits));
  return `\u2264${(rounded < amount ? rounded + 1 / factor : rounded).toFixed(digits)}%`;
}

function time(value) {
  return typeof value === "string" && value.trim() ? Date.parse(value) : NaN;
}

export function decisionView(token, config = {}, now = Date.now()) {
  const thesis = token.signalThesis || {};
  const integrity = token.supplyIntegrity || {};
  const retained = percent(thesis.token_retention_pct);
  const supply = percent(thesis.current_retained_supply_pct);
  const walletCoverage = percent(thesis.balance_coverage_pct);
  const tokenCoverage = percent(thesis.token_balance_coverage_pct);
  const cohortCoverage = percent(thesis.cohort_wallet_coverage_pct);
  const cohortTokenCoverage = percent(thesis.cohort_token_coverage_pct);
  const checked = time(thesis.last_checked_at);
  const age = now - checked;
  const grace = (numeric(config.signal_thesis_recheck_grace_minutes) ?? 15) * 60_000;
  const maxAge = ((numeric(config.signal_thesis_recheck_minutes) ?? 60) * 60_000) + grace;
  const integrityAge = now - time(integrity.checked_at);
  const holderMaxAge = ((numeric(config.supply_integrity_refresh_minutes) ?? 180) * 60_000) + grace;
  const integrityFresh = Number.isFinite(integrityAge) && integrityAge >= 0 && integrityAge <= holderMaxAge;
  const linkPolicyCurrent = numeric(integrity.evidence_version) >= 2;
  const coordination = thesis.coordinated_activity || (token.currentSignalAlerts || []).map(a => a.coordinated_activity).find(Boolean);
  const rotation = numeric(coordination?.metrics?.market_rotation_observations) > 0;
  const next = time(thesis.next_check_at);
  const fresh = Number.isFinite(age) && age >= 0 && age <= maxAge
    && (!Number.isFinite(next) || now < next + grace);
  const balanceComplete = walletCoverage !== null && tokenCoverage !== null
    && walletCoverage >= (numeric(config.signal_thesis_min_balance_coverage_pct) ?? 80)
    && tokenCoverage >= (numeric(config.signal_thesis_min_token_balance_coverage_pct) ?? 80);
  const cohortComplete = cohortCoverage !== null && cohortTokenCoverage !== null
    && cohortCoverage >= (numeric(config.signal_thesis_min_cohort_wallet_coverage_pct) ?? 70)
    && cohortTokenCoverage >= (numeric(config.signal_thesis_min_cohort_token_coverage_pct) ?? 70);
  const complete = balanceComplete && cohortComplete && retained !== null;
  const currentConfirmed = token.signalLifecycle?.currentConfirmed === true;
  const saleHistoryUnknown = originalSaleHistoryUnknown(token.signalThesis);
  const movement = walletActivityView(thesis);
  const thesisConfirmed = !saleHistoryUnknown && thesis.signal_confirmation?.status === "confirmed";
  const blockers = [];
  if (!Number.isFinite(checked)) blockers.push("Original wallet balances have not been checked.");
  else if (!fresh) blockers.push("Wallet check is overdue; holdings below are the last observation.");
  if (!balanceComplete) blockers.push(`Balance checks cover ${walletCoverage === null ? "an unknown share" : `${Math.round(walletCoverage)}%`} of stored wallets.`);
  if (!cohortComplete) blockers.push(`The stored cohort covers ${cohortCoverage === null ? "an unknown share" : `${Math.round(cohortCoverage)}%`} of original signal wallets. Rechecking the same subset will not fill this gap.`);
  if (retained === null) blockers.push("Retained position cannot be verified from this snapshot.");
  if (!currentConfirmed && !thesisConfirmed) blockers.push("The original accumulation has no confirmed signal record.");
  if (saleHistoryUnknown) blockers.push("Original sale history is unknown. Checked balances are an upper bound, not proof that the original buys remain unsold.");
  if (!token.currentMarket?.isFresh) blockers.push("Current market data is missing or stale.");
  if (token.dataStatus === "scanner_stale") blockers.push("The latest scan is stale or failed.");
  if (!integrity.status || integrity.status === "unverified" || integrity.data_quality_status !== "complete") {
    blockers.push("The checked holder sample is incomplete; full ownership is not established.");
  } else if (integrity.status === "concentrated") {
    blockers.push("High holder concentration; inspect the supply breakdown.");
  } else if (integrity.status === "watch") {
    blockers.push("Holder concentration or wallet links need review; this is not a buying signal.");
  }
  if (!integrityFresh) blockers.push("Holder snapshot is missing or stale; concentration may have changed.");
  if (!linkPolicyCurrent) blockers.push("Legacy wallet links require rechecking under the current evidence rules.");
  if (rotation) blockers.push("Funding-linked sell/rebuy rotation was observed; turnover is not new accumulation.");
  if (token.currentSignalTier === "late_chase") blockers.push("The scanner flagged an extended or crowded move.");

  let queue = "verification";
  let label = "Needs data";
  if (thesis.status === "invalidated" || token.lifecycleStatus === "closed") {
    queue = "inactive"; label = "Closed";
  } else if (thesis.status === "weakening" || token.lifecycleStatus === "weakening"
    || thesis.balance_status === "outflow" && complete) {
    queue = "reducing"; label = movement.label;
  } else if (retained > 0 && complete && (thesis.status === "intact"
    || thesis.balance_status === "present" && thesis.outflow_evidence?.balance_check_complete === true)) {
    queue = "holding";
    label = saleHistoryUnknown ? (fresh ? "Balance cap checked" : "Balance cap at last check") : fresh ? "Holding" : "Held at last check";
    if (thesis.status === "intact" && !saleHistoryUnknown && currentConfirmed && fresh && token.dataStatus === "current" && token.currentMarket?.isFresh
      && integrity.data_quality_status === "complete" && integrity.status === "distributed"
      && integrityFresh && linkPolicyCurrent && !rotation
      && ["watch", "actionable", "hot_reactivation"].includes(token.currentSignalTier)) {
      queue = "review"; label = token.currentSignalTier === "watch" ? "Confirmed activity" : "Confirmed burst";
    }
  } else if (!currentConfirmed && (token.currentSignalAlerts || []).length && token.dataStatus !== "scanner_stale"
    && token.currentSignalTier !== "noise" && token.currentSignalTier !== "late_chase"
    && (!token.signalThesis || !["unknown", "intact"].includes(thesis.status))) {
    queue = "early"; label = "Unconfirmed activity";
  }
  const meta = REVIEW_QUEUES.find((item) => item.id === queue);
  const reason = queue === "review" ? "Confirmed buying + retained balances"
    : queue === "holding" ? `${retentionBound(retained).replace("\u2264", "Up to ")} original-position balance cap${fresh ? "" : "; check overdue"}${saleHistoryUnknown ? "; original sale history unknown" : ""}`
      : queue === "reducing" ? `${retained === null ? "Balance cap unknown" : `${retentionBound(retained).replace("\u2264", "Up to ")} original-position balance cap`}; ${movement.reason.toLowerCase()}`
        : queue === "early" ? "Buying observed; confirmation missing"
          : queue === "inactive" ? "Original accumulation invalidated"
            : !cohortComplete && cohortCoverage !== null ? `Only ${Math.round(cohortCoverage)}% of original wallets covered`
              : !fresh ? "Fresh wallet evidence missing" : "Evidence is incomplete";
  return { queue, label, tone: queue === "reducing" ? movement.tone : meta.tone, reason, retained, supply, fresh, complete, balanceComplete,
    cohortComplete, walletCoverage, tokenCoverage, cohortCoverage, cohortTokenCoverage,
    checkedAt: Number.isFinite(checked) ? thesis.last_checked_at : null, blockers,
    integrityFresh, linkPolicyCurrent, rotation, saleHistoryUnknown,
    confirmation: saleHistoryUnknown ? "Original sale history unknown" : currentConfirmed ? "Confirmed this scan" : thesisConfirmed ? "Previously confirmed" : "Not confirmed" };
}

export function matchesReviewQueue(view, queue) {
  return queue === "overview" ? view.queue !== "inactive" : queue === "all" || view.queue === queue;
}

export function compareReviewTokens(a, b, sort = "caught") {
  if (sort === "retained") {
    const diff = (b.decision?.retained ?? -1) - (a.decision?.retained ?? -1);
    if (diff) return diff;
  }
  const stamp = (value) => Number.isFinite(time(value)) ? time(value) : -Infinity;
  const diff = stamp(b.firstSignalAt) - stamp(a.firstSignalAt);
  return (Number.isNaN(diff) ? 0 : diff) || String(a.key).localeCompare(String(b.key));
}

// Never let slow detail requests replace a newer summary or another snapshot.
export function sameDetailCohort(detail, tokenKey, thesis, generation) {
  if (detail?.detail_status != null && detail.detail_status !== "ready") return false;
  const source = time(detail?.report_source_updated_at), current = time(generation);
  return detail?.token_key === tokenKey && Boolean(thesis?.cohort_id)
    && Number.isFinite(source) && Number.isFinite(current) && source <= current
    && detail.thesis?.cohort_id === thesis.cohort_id
    && ["signal_at", "signal_window_start", "signal_window_end"].every(field =>
      thesis[field] == null || detail.thesis?.[field] === thesis[field]);
}

export function canApplyDetail(detail, tokenKey, requestedGeneration, currentGeneration, thesis) {
  if (detail?.detail_status != null && detail.detail_status !== "ready") return false;
  if (detail?.token_key !== tokenKey || requestedGeneration !== currentGeneration) return false;
  const source = time(detail.report_source_updated_at);
  const generation = time(currentGeneration);
  if (!Number.isFinite(generation) || source !== generation) return false;
  if (detail.thesis?.token_address && detail.thesis.token_address !== tokenKey
    && detail.thesis.pool_address !== tokenKey) return false;
  for (const field of ["cohort_id", "signal_at", "signal_window_start", "signal_window_end"]) {
    if (thesis?.[field] != null && detail.thesis?.[field] !== thesis[field]) return false;
  }
  const incoming = time(detail.thesis?.last_checked_at);
  const existing = time(thesis?.last_checked_at);
  const updated = time(thesis?.updated_at);
  return (!Number.isFinite(existing) || incoming === existing)
    && (!Number.isFinite(updated) || time(detail.thesis?.updated_at) === updated);
}
