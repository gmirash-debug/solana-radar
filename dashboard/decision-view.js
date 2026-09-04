// Presentation only: never upgrades the scanner's confirmation or lifecycle.
export const REVIEW_QUEUES = [
  { id: "review", label: "Ready to review", note: "Current confirmed signals with fresh cohort and market checks.", tone: "positive" },
  { id: "holding", label: "Holding", note: "The original cohort still retains its position. Not a new entry signal.", tone: "neutral" },
  { id: "early", label: "Early observations", note: "New activity, not confirmed accumulation.", tone: "info" },
  { id: "reducing", label: "Reduced positions", note: "Tokens left original wallets. Balances alone cannot distinguish sales from transfers.", tone: "negative" },
  { id: "verification", label: "Needs data", note: "Insufficient evidence for a current conclusion.", tone: "muted" },
  { id: "inactive", label: "Closed", note: "The scanner invalidated the original accumulation thesis.", tone: "muted" },
];

export function numeric(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function percent(value) {
  const n = numeric(value);
  return n !== null && n >= 0 && n <= 100 ? n : null;
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
  const thesisConfirmed = thesis.signal_confirmation?.status === "confirmed";
  const blockers = [];
  if (!Number.isFinite(checked)) blockers.push("Original wallet balances have not been checked.");
  else if (!fresh) blockers.push("Wallet check is overdue; holdings below are the last observation.");
  if (!balanceComplete) blockers.push(`Balance checks cover ${walletCoverage === null ? "an unknown share" : `${Math.round(walletCoverage)}%`} of stored wallets.`);
  if (!cohortComplete) blockers.push(`The stored cohort covers ${cohortCoverage === null ? "an unknown share" : `${Math.round(cohortCoverage)}%`} of original signal wallets. Rechecking the same subset will not fill this gap.`);
  if (retained === null) blockers.push("Retained position cannot be verified from this snapshot.");
  if (!currentConfirmed && !thesisConfirmed) blockers.push("The original accumulation has no confirmed signal record.");
  if (!token.currentMarket?.isFresh) blockers.push("Current market data is missing or stale.");
  if (token.dataStatus === "scanner_stale") blockers.push("The latest scan is stale or failed.");
  if (!integrity.status || integrity.status === "unverified" || integrity.data_quality_status !== "complete") {
    blockers.push("Supply ownership is not fully verified.");
  } else if (integrity.status === "concentrated") {
    blockers.push("High holder concentration; inspect the supply breakdown.");
  } else if (integrity.status === "watch") {
    blockers.push("Holder concentration or wallet links need review; this is not a buying signal.");
  }
  if (token.currentSignalTier === "late_chase") blockers.push("The scanner flagged an extended or crowded move.");

  let queue = "verification";
  let label = "Needs data";
  if (thesis.status === "invalidated" || token.lifecycleStatus === "closed") {
    queue = "inactive"; label = "Closed";
  } else if (thesis.status === "weakening" || token.lifecycleStatus === "weakening") {
    queue = "reducing"; label = "Position reduced";
  } else if (thesis.status === "intact" && retained !== null && complete) {
    queue = "holding"; label = fresh ? "Holding" : "Held at last check";
    if (currentConfirmed && fresh && token.dataStatus === "current" && token.currentMarket?.isFresh
      && integrity.data_quality_status === "complete" && integrity.status === "distributed"
      && ["watch", "actionable", "hot_reactivation"].includes(token.currentSignalTier)) {
      queue = "review"; label = token.currentSignalTier === "watch" ? "Confirmed activity" : "Confirmed burst";
    }
  } else if (!currentConfirmed && (token.currentSignalAlerts || []).length && token.dataStatus !== "scanner_stale"
    && token.currentSignalTier !== "noise" && token.currentSignalTier !== "late_chase"
    && (!token.signalThesis || thesis.status !== "unknown")) {
    queue = "early"; label = "Unconfirmed activity";
  }
  const meta = REVIEW_QUEUES.find((item) => item.id === queue);
  const reason = queue === "review" ? "Confirmed buying + retained balances"
    : queue === "holding" ? `${Math.round(retained)}% of original position retained${fresh ? "" : "; check overdue"}`
      : queue === "reducing" ? (retained === null ? "Original cohort balances declined" : `${Math.round(retained)}% of original position remains`)
        : queue === "early" ? "Buying observed; confirmation missing"
          : queue === "inactive" ? "Original accumulation invalidated"
            : !cohortComplete && cohortCoverage !== null ? `Only ${Math.round(cohortCoverage)}% of original wallets covered`
              : !fresh ? "Fresh wallet evidence missing" : "Evidence is incomplete";
  return { queue, label, tone: meta.tone, reason, retained, supply, fresh, complete, balanceComplete,
    cohortComplete, walletCoverage, tokenCoverage, cohortCoverage, cohortTokenCoverage,
    checkedAt: Number.isFinite(checked) ? thesis.last_checked_at : null, blockers,
    confirmation: currentConfirmed ? "Confirmed this scan" : thesisConfirmed ? "Previously confirmed" : "Not confirmed" };
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
export function canApplyDetail(detail, tokenKey, requestedGeneration, currentGeneration, thesis) {
  if (detail?.token_key !== tokenKey || requestedGeneration !== currentGeneration) return false;
  const source = time(detail.report_source_updated_at);
  const generation = time(currentGeneration);
  if (!Number.isFinite(source) || (Number.isFinite(generation) && source < generation)) return false;
  const incoming = time(detail.thesis?.last_checked_at);
  const existing = time(thesis?.last_checked_at);
  return !Number.isFinite(existing) || (Number.isFinite(incoming) && incoming >= existing);
}
