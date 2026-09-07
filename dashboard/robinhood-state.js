export const CHAIN_ID = 4663;
export function formatSupplyPercent(value) {
  if (value == null || !Number.isFinite(Number(value))) return "Unknown";
  const number = Number(value);
  return number > 0 && number < 0.01 ? "<0.01%" : `${number.toFixed(2)}%`;
}
export const addressOk = value => /^0x[0-9a-f]{40}$/.test(value || "");
const counters = ["window_from_block", "window_to_block", "buy_swaps", "sell_swaps", "receipts_checked", "swap_transactions", "buy_transactions"];
export function validSnapshot(payload) {
  return payload?.chain_id === CHAIN_ID && Array.isArray(payload.tokens)
    && payload.tokens.every(t => addressOk(t.token) && addressOk(t.pool) && t.key === `${CHAIN_ID}:${t.token}`
      && counters.every(key => t[key] == null || (Number.isSafeInteger(t[key]) && t[key] >= 0))
      && Array.isArray(t.wallets) && t.wallets.every(w => addressOk(w.address)));
}
export function isFresh(payload, now = Date.now()) {
  const generated = Date.parse(payload?.generated_at);
  return payload?.status !== "unavailable" && Number.isFinite(generated)
    && now - generated >= -300000 && now - generated < 90 * 60000;
}
export function selectTokens(tokens, query = "", status = "all") {
  return tokens.filter(t => (status === "all" || t.status === status)
    && `${t.symbol} ${t.name} ${t.token}`.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => (Date.parse(b.first_observed_at) || 0) - (Date.parse(a.first_observed_at) || 0) || a.key.localeCompare(b.key));
}
