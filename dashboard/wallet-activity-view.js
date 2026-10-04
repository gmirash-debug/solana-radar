// Gross wallet flows are evidence of activity, not an original-lot partition.
export function walletActivityView(thesis = {}) {
  const audit = thesis.wallet_activity || {};
  const amounts = audit.amounts_tokens || {};
  const positive = key => typeof amounts[key] === "number" && Number.isFinite(amounts[key]) && amounts[key] > 0;
  const sold = positive("sold"), moved = positive("transferred"), service = positive("service");
  const checked = audit.status === "checked" && audit.wallet_coverage_pct === 100;
  let label = "Original-wallet outflow";
  let reason = audit.status ? `Wallet history ${audit.wallets_checked ?? 0}/${audit.wallets_total ?? 0} checked` : "Wallet history queued";
  if (sold && moved) { label = "Sales + transfers observed"; reason = `Sales and direct transfers found; ${reason.toLowerCase()}`; }
  else if (sold) { label = "Sales observed"; reason = `Decoded sales found; ${reason.toLowerCase()}`; }
  else if (moved) { label = "Transfers observed"; reason = `Direct transfers found; ${reason.toLowerCase()}`; }
  else if (service) { label = "Service outflow observed"; reason = `Service destination found; ${reason.toLowerCase()}`; }
  else if ((thesis.outflow_evidence?.observed_sale_transactions || 0) > 0) {
    reason = `Sale trades observed in pool history; ${reason.toLowerCase()}`;
  } else if ((thesis.outflow_evidence?.direct_transfer_transactions || 0) > 0) {
    reason = `Transfers observed in pool history; ${reason.toLowerCase()}`;
  }
  return {audit, label, reason, checked, sold, moved, service,
    tone: sold ? "negative" : moved ? "neutral" : "warning"};
}
