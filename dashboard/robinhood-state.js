export const CHAIN_ID = 4663;
export function ageFilterLabel(config) {
  const low = config?.min_pool_age_hours, high = config?.max_pool_age_hours;
  if (![low, high].every(n => typeof n === "number" && Number.isFinite(n) && n >= 0)) return "Age filter unavailable";
  const age = n => n >= 24 && n % 24 === 0 ? `${n / 24}d` : `${n}h`;
  return `${low === 0 ? "From launch" : age(low)} - ${age(high)}`;
}
export function relaySignalLabel(token) {
  if (token.cohort_reason === "Cross-chain buy wave") return "Cross-chain buy wave";
  if (token.cohort_reason === "Relay buy wave") return "Relay buy wave";
  if (token.relay?.wave) return token.relay.wave.services?.includes("LI.FI") ? "Cross-chain wave / holding unconfirmed" : "Relay wave / holding unconfirmed";
  return "";
}
export function formatSupplyPercent(value) {
  if (value == null || !Number.isFinite(Number(value))) return "Unknown";
  const number = Number(value);
  return number > 0 && number < 0.01 ? "<0.01%" : `${number.toFixed(2)}%`;
}
export const addressOk = value => /^0x[0-9a-f]{40}$/.test(value || "");
export const poolOk = t => t.protocol === "v4" ? /^0x[0-9a-f]{64}$/.test(t.pool || "") : (!t.protocol || t.protocol === "v3") && addressOk(t.pool);
const counters = ["window_from_block", "window_to_block", "buy_swaps", "sell_swaps", "receipts_checked", "swap_transactions", "buy_transactions", "attributed_buy_transactions", "cohort_checks", "backlog_blocks"];
function validWave(w) {
  return w == null || ["window_seconds", "buyers", "buy_transactions", "from_timestamp", "to_timestamp"].every(k => Number.isSafeInteger(w[k]) && w[k] >= 0)
    && w.from_timestamp <= w.to_timestamp && w.to_timestamp < 8640000000000
    && Number.isFinite(w.gross_bought_supply_pct) && w.gross_bought_supply_pct >= 0;
}
export function validSnapshot(payload) {
  return payload?.chain_id === CHAIN_ID && Array.isArray(payload.tokens)
    && payload.tokens.every(t => addressOk(t.token) && poolOk(t) && t.key === `${CHAIN_ID}:${t.token}`
      && counters.every(key => t[key] == null || (Number.isSafeInteger(t[key]) && t[key] >= 0))
      && validWave(t.relay?.wave) && validWave(t.relay?.signal_wave)
      && Array.isArray(t.wallets) && t.wallets.every(w => addressOk(w.address)));
}
export function isFresh(payload, now = Date.now()) {
  const generated = Date.parse(payload?.generated_at);
  return payload?.status !== "unavailable" && Number.isFinite(generated)
    && now - generated >= -300000 && now - generated < 90 * 60000;
}
export function walletFresh(token, now = Date.now()) {
  const checked = Date.parse(token?.checked_at);
  return !["queued", "check_failed"].includes(token?.status) && Number.isFinite(checked)
    && now - checked >= -300000 && now - checked < 90 * 60000;
}
export function supplyRange(token) {
  const low = token?.retained_supply_lower_bound_pct, high = token?.retained_supply_upper_bound_pct;
  if (high == null) return "Not checked";
  if (low == null) return `Up to ${formatSupplyPercent(high)}`;
  return Math.abs(low - high) < 0.00001 ? formatSupplyPercent(high) : `${formatSupplyPercent(low)} - ${formatSupplyPercent(high)}`;
}
export function selectTokens(tokens, query = "", status = "all") {
  return tokens.filter(t => (status === "all" || t.status === status)
    && `${t.symbol} ${t.name} ${t.token} ${(t.wallets || []).map(w => w.address).join(" ")}`.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((a, b) => (Date.parse(b.first_observed_at) || 0) - (Date.parse(a.first_observed_at) || 0) || a.key.localeCompare(b.key));
}

export const REVIEW_GROUPS = [
  {id:"buy_wave", label:"New buy waves", tone:"info"},
  {id:"retained", label:"Holding", tone:"positive"},
  {id:"observed", label:"Early observations", tone:"info"},
  {id:"reduced", label:"Reduced positions", tone:"negative"},
  {id:"needs_data", label:"Needs data", tone:"warning"},
  {id:"risk", label:"Contract risk", tone:"negative"},
];

export function reviewGroup(token, payload, now = Date.now()) {
  if (token.status === "risk" || token.security?.status === "risk") return "risk";
  if (!isFresh(payload, now) || !walletFresh(token, now)) return "needs_data";
  if (["retained", "buy_wave"].includes(token.status) && !(token.cohort_checks >= (token.status === "retained" ? 2 : 1) && token.cohort_created_at)) return "needs_data";
  return REVIEW_GROUPS.some(g => g.id === token.status) ? token.status : "needs_data";
}

export function positionBounds(token) {
  const wallets = token?.wallets || [];
  if (!wallets.length) return null;
  let bought = 0, lower = 0, upper = 0, lowerKnown = true;
  for (const w of wallets) {
    if (![w.bought_raw, w.retained_upper_bound_raw].every(n => /^\d+$/.test(String(n)))) return null;
    if (w.retained_lower_bound_raw != null && !/^\d+$/.test(String(w.retained_lower_bound_raw))) return null;
    const b = Number(w.bought_raw), hi = Number(w.retained_upper_bound_raw), lo = Number(w.retained_lower_bound_raw);
    if (w.bought_raw == null || w.retained_upper_bound_raw == null || !Number.isFinite(b + hi) || b <= 0 || hi < 0 || hi > b) return null;
    if (w.retained_lower_bound_raw == null) lowerKnown = false;
    else if (!Number.isFinite(lo) || lo < 0 || lo > hi) return null;
    bought += b; upper += hi; lower += lo;
  }
  return {lower:lowerKnown ? 100 * lower / bought : null, upper:100 * upper / bought};
}

export function marketFresh(token, now = Date.now()) {
  const checked = Date.parse(token?.market_checked_at);
  return !token?.market_stale && Number.isFinite(checked) && now - checked >= -300000 && now - checked < 90 * 60000;
}

export function comparePositions(a, b, mode = "caught") {
  if (mode === "retained") {
    const av = positionBounds(a)?.lower ?? -1, bv = positionBounds(b)?.lower ?? -1;
    if (av !== bv) return bv - av;
  }
  return (Date.parse(b.first_observed_at) || 0) - (Date.parse(a.first_observed_at) || 0) || a.key.localeCompare(b.key);
}
