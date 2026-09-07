import { validSnapshot, isFresh, selectTokens, formatSupplyPercent, walletFresh, supplyRange } from "./robinhood-state.js?v=20260907-rh3";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const money = value => value == null ? "Unknown" : new Intl.NumberFormat("en", {style:"currency", currency:"USD", notation:"compact", maximumFractionDigits:1}).format(value);
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not checked";
const pct = value => esc(formatSupplyPercent(value));
const labels = {buy_wave:"New buy wave", retained:"Holding confirmed", reduced:"Cohort reduced", risk:"Contract risk", observed:"Activity only", queued:"Waiting for check", check_failed:"Check unavailable"};
let payload = null, selected = null;
function render() {
  if (!payload) return;
  const fresh = isFresh(payload);
  $("#subtitle").textContent = `Last scan: ${date(payload.generated_at)}${fresh ? "" : " / stale or unavailable"}`;
  $("#coverage").textContent = `${payload.checked_pools ?? 0} checked / ${payload.eligible_tokens ?? 0} candidates / ${payload.excluded_stocks ?? 0} stock pools excluded / ${payload.rpc_calls ?? 0} RPC requests`;
  $("#limits").textContent = "New tokens: verified age 1-15 days / liquidity >= $3k / FDV <= $5m. Existing buy cohorts remain tracked. Bounded discovery, not every pool on the network.";
  $("#health").textContent = (payload.errors || []).length ? `${payload.errors.length} checks incomplete. ${payload.errors[0]}` : `Hourly scans / ${payload.provider || "RPC"} / incremental history`;
  const tokens = selectTokens(payload.tokens, $("#search").value, $("#status").value);
  if (!tokens.some(t => t.key === selected)) selected = tokens[0]?.key ?? null;
  $("#tokens").innerHTML = tokens.length ? tokens.map(t => `<button class="rh-token" data-key="${esc(t.key)}" aria-pressed="${t.key === selected}"><strong>${esc(t.symbol)}</strong><span>${esc(labels[t.status] || "Unknown")}</span><small>${esc(money(t.fdv_usd))} FDV / ${esc(t.protocol || "v3")}</small><small>${esc(supplyRange(t))} supply${walletFresh(t) ? "" : " / last check"}</small></button>`).join("") : `<p>No matching tokens. ${payload.checked_pools ? "Other tokens did not meet the filters." : "See scan coverage above."}</p>`;
  document.querySelectorAll(".rh-token").forEach(button => button.addEventListener("click", () => {
    selected = button.dataset.key;
    document.body.classList.add("rh-detail-open");
    render();
    if (innerWidth <= 760) window.scrollTo(0, 0);
  }));
  const t = payload.tokens.find(t => t.key === selected);
  if (!t) { $("#detail").innerHTML = "<p>No token selected</p>"; return; }
  const wallets = t.wallets || [], security = t.security || {status:"unknown", flags:[], unknown:["not_checked"]};
  const riskText = security.status === "risk" ? security.flags.join(", ") : security.status === "no_flags" ? "No flags reported by GoPlus; not a safety guarantee" : `Incomplete: ${(security.unknown || []).join(", ")}`;
  const conclusion = t.status === "retained" ? "The original buy cohort still retains tokens on a later check. This confirms holding, not insider identity or an entry opportunity."
    : t.status === "buy_wave" ? "A distributed buy wave meets the evidence thresholds. Retention needs a later check."
    : t.status === "reduced" ? "The original cohort's conservatively retained amount fell below 60%. Outflows include sales and transfers."
    : "No confirmed accumulation conclusion. Activity alone is not a trade signal.";
  $("#detail").innerHTML = `<button id="back" class="rh-back icon-button" aria-label="Back to tokens" title="Back to tokens"><img src="icons/arrow-left.svg" alt=""></button><h2>${esc(t.symbol)} <span class="rh-badge">${esc(labels[t.status] || "Unknown")}</span></h2>
    <p class="rh-conclusion">${esc(conclusion)}</p>
    ${!fresh || !walletFresh(t) ? '<p class="rh-warning">Wallet evidence is not current. Previous balances below retain their original check date.</p>' : ""}
    <div class="rh-metrics"><div><small>COHORT SUPPLY</small><strong>${esc(supplyRange(t))}</strong></div><div><small>WALLETS</small><strong>${wallets.length}</strong></div><div><small>RETENTION CHECKS</small><strong>${t.cohort_checks ?? 0}</strong></div></div>
    <dl><dt>First observed</dt><dd>${esc(date(t.first_observed_at))}</dd><dt>Token created</dt><dd>${t.token_age_verified ? esc(date(t.token_created_at)) : "Age not verified; discovery candidate only"}</dd><dt>Wallet check</dt><dd>${esc(date(t.checked_at))}</dd><dt>Market cap / FDV</dt><dd>${money(t.market_cap_usd)} / ${money(t.fdv_usd)}</dd><dt>Liquidity</dt><dd>${money(t.liquidity_usd)}</dd><dt>Market checked</dt><dd>${esc(date(t.market_checked_at))}${t.market_stale ? " / stale" : ""}</dd><dt>Buys / sells</dt><dd>${t.buy_swaps ?? "?"} / ${t.sell_swaps ?? "?"} swap events</dd><dt>Buy attribution</dt><dd>${t.attributed_buy_transactions ?? 0} / ${t.buy_transactions ?? "?"} transactions</dd><dt>History</dt><dd>${t.history_complete ? "Latest window complete" : "Incomplete"}${t.backlog_blocks ? ` / ${t.backlog_blocks} blocks pending` : ""}</dd><dt>Contract checks</dt><dd>${esc(riskText)}</dd></dl>
    ${t.error ? `<p class="rh-warning">${esc(t.error)}</p>` : ""}
    <h3>Original Buyers${t.cohort_created_at ? ` / ${esc(date(t.cohort_created_at))}` : " / Current Window"}</h3>
    <table class="rh-wallets"><thead><tr><th>Buyer</th><th>Held range</th><th>Supply max.</th></tr></thead><tbody>${wallets.map(w => {
      const lower = w.retained_lower_bound_raw == null ? null : 100 * Number(w.retained_lower_bound_raw) / Number(w.bought_raw);
      return `<tr><td><a href="https://robinhoodchain.blockscout.com/address/${esc(w.address)}" target="_blank" rel="noopener noreferrer">${esc(w.address.slice(0, 8))}...${esc(w.address.slice(-6))}</a></td><td>${lower == null ? "?" : pct(lower)} - ${pct(w.retention_upper_bound_pct)}</td><td>${pct(w.supply_upper_bound_pct)}</td></tr>`;
    }).join("")}</tbody></table>
    <details><summary>Evidence & Sources</summary><p>Lower bound subtracts all outgoing transfers from attributed purchases. Upper bound is capped by current balance. Incoming transfers cannot restore the lower bound. Smart-account and ambiguous routes are excluded. No wallet connections or realized profit are inferred.</p><p>Risk checks: ${esc(security.source || "not available")} / ${esc(date(security.checked_at))}. Missing taxes are unknown, not zero.</p><p>Indexed blocks: ${t.history_from_block ?? "?"} - ${t.indexed_through_block ?? "?"}. One selected pool per token; not launch-to-date coverage.</p><p>${esc(t.token)}</p><p><a href="https://dexscreener.com/robinhood/${t.pool}" target="_blank" rel="noopener noreferrer">Market chart</a> / <a href="https://robinhoodchain.blockscout.com/token/${t.token}" target="_blank" rel="noopener noreferrer">Token explorer</a></p></details>`;
  $("#back").addEventListener("click", () => { document.body.classList.remove("rh-detail-open"); document.querySelector('.rh-token[aria-pressed="true"]')?.focus(); });
}
async function load() {
  $("#refresh").disabled = true;
  try {
    const response = await fetch(`data/robinhood.json?t=${Date.now()}`, {cache:"no-store", signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error("Robinhood observations are not yet published");
    const next = await response.json();
    if (!validSnapshot(next)) throw new Error("Invalid Robinhood dataset; refusing to mix networks");
    payload = next;
    render();
  } catch (error) { $("#health").textContent = error.message; $("#subtitle").textContent = "Robinhood data unavailable"; }
  finally { $("#refresh").disabled = false; }
}
$("#network").addEventListener("change", event => { if (event.target.value === "solana") location.href = "index.html"; });
$("#refresh").addEventListener("click", load);
$("#search").addEventListener("input", render);
$("#status").addEventListener("change", render);
load();
