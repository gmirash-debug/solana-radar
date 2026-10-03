function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
}

export function renderEvaluationSummary(summary) {
  if (!summary || summary.mode !== "shadow") return `<section class="intelligence-section" data-evaluation-summary><h2>Signal quality</h2><p>Prospective observations pending.</p></section>`;
  const counts = summary.counts || {};
  const rows = Object.entries(summary.horizons || {}).map(([horizon, item]) => {
    const status = item.signal_outcome_status_counts || {};
    const missing = Object.entries(status).filter(([key]) => !["eligible", "pending"].includes(key)).reduce((total, [, count]) => total + Number(count || 0), 0);
    return `<tr><td>${esc(horizon)}</td><td>${esc(status.eligible || 0)}</td><td>${esc(status.pending || 0)}</td><td>${esc(missing)}</td><td>${esc(item.complete_price_pairs || 0)}</td></tr>`;
  }).join("");
  return `<section class="intelligence-section" data-evaluation-summary>
    <div class="section-title-row"><div><h2>Signal quality</h2><p>${esc(counts.primary_signals || 0)} frozen signals / ${esc(counts.primary_controls || 0)} market controls. Shadow results, not a proven trading edge.</p></div></div>
    <div class="table-wrap compact-table"><table><thead><tr><th>Horizon</th><th>Observed</th><th>Pending</th><th>Missing / late</th><th>Matched pairs</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p>Costs and slippage are estimates. Missing observations stay unknown; strategy versions and chronological holdout are evaluated separately.</p>
  </section>`;
}
