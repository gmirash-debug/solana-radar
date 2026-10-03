function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}

function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : null; }

export function r2BudgetView(budget) {
  if (!budget?.enabled) return {alert:"",panel:""};
  const unknown = !budget.initialized || budget.status === "unavailable";
  const paused = Boolean(budget.paused) || unknown;
  const warning = !paused && Object.values(budget.used_pct || {}).some(value => Number.isFinite(value) && value >= 80);
  const tone = paused ? "bad" : warning ? "warn" : "good";
  const title = unknown ? "R2 check unavailable: archive access is blocked"
    : paused ? "R2 paused to protect free quotas" : warning ? "R2 is approaching a free-tier limit" : "R2 within free quotas";
  const reset = /^\d{4}-\d{2}-01T00:00:00/.test(budget.resets_at || "") ? budget.resets_at.slice(0,10) : "the first of next month";
  const note = unknown ? "Current scans continue in the primary database. Archive requests stay blocked until budget verification succeeds."
    : paused ? `Billable archive requests are stopped. Monthly reset: ${reset} at 00:00 UTC; the 33-day billing safety cap may keep R2 paused longer. Storage must also remain below 9 GB. Current scans continue in the primary database.`
      : "Warning at 80%; archive requests stop at 90% of any quota. Monthly counters reset at 00:00 UTC on the first, but a 33-day cap protects the actual billing cycle; stored bytes do not reset.";
  const alert = paused || warning ? `<aside class="r2-budget-alert freshness-${tone}" role="status"><strong>${esc(title)}</strong><span>${esc(note)}</span></aside>` : "";
  const rows = [["storage_bytes","Stored data",10_000_000_000], ["class_a","Write / list operations",1_000_000], ["class_b","Read / head operations",10_000_000]].map(([key,label,limit]) => {
    const used = unknown ? null : count(budget.usage?.[key]);
    const percent = used === null ? null : 100 * used / limit;
    const value = used === null ? "not verified" : key === "storage_bytes" ? `${(used/1e9).toFixed(3)} / 10 GB` : `${used.toLocaleString("en-US")} / ${limit.toLocaleString("en-US")}`;
    return `<div class="r2-budget-quota"><div><span>${label}</span><strong>${esc(value)}</strong></div><meter min="0" max="100" value="${Math.min(100,percent ?? 0)}" low="80" high="90" optimum="0" aria-label="${label}" aria-valuetext="${esc(value)}"></meter><small>${percent === null ? "unknown" : `${percent.toFixed(2)}% of free allowance`}</small></div>`;
  }).join("");
  return {alert,panel:`<section class="r2-budget-monitor"><div class="r2-budget-heading"><h2>R2 free-tier budget</h2><span class="status-pill freshness-${tone}">${esc(title)}</span></div><div class="r2-budget-quotas">${rows}</div><p>Conservative reserved usage, not a bill. Covers scanner archive requests; external clients are not covered.</p><p>${esc(note)}</p></section>`};
}
