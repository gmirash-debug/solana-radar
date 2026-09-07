import { validSnapshot, isFresh, selectTokens } from "./robinhood-state.js?v=20260907-rh1";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const money = value => value == null ? "Unknown" : new Intl.NumberFormat("en", {style:"currency", currency:"USD", notation:"compact", maximumFractionDigits:1}).format(value);
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not checked";
const pct = value => value == null ? "Unknown" : `${Number(value).toFixed(2)}%`;
const labels = {buy_wave:"Buy wave", observed:"Observed activity", queued:"Queued", check_failed:"Check failed"};
let payload = null, selected = null;
function render() {
  if (!payload) return;
  const fresh = isFresh(payload);
  $("#subtitle").textContent = `Last observations: ${date(payload.generated_at)}${fresh ? "" : " / stale or unavailable"}`;
  $("#coverage").textContent = `${payload.discovered_pools ?? 0} discovered pools / ${payload.eligible_tokens ?? 0} eligible tokens / ${payload.checked_pools ?? 0} attempted checks / ${payload.rpc_calls ?? 0} RPC requests`;
  $("#limits").textContent = "Uniswap v3 only. Pool age 1-15 days (token age unverified), liquidity >= $3k, FDV <= $5m. Three discovery pages, up to six pools per run. V4 and other DEXs excluded.";
  $("#health").textContent = (payload.errors || []).length ? `${payload.errors.length} checks incomplete. ${payload.errors[0]}` : `Rolling one-hour windows / ${payload.provider || "RPC"}. Not continuous network-wide coverage.`;
  const tokens = selectTokens(payload.tokens, $("#search").value, $("#status").value);
  $("#tokens").innerHTML = tokens.length ? tokens.map(t => `<button class="rh-token" data-key="${esc(t.key)}" aria-pressed="${t.key === selected}"><strong>${esc(t.symbol)}</strong><span>${esc(labels[t.status] || "Unknown")}</span><small>${esc(money(t.fdv_usd))} FDV / ${esc(money(t.liquidity_usd))} liquidity</small><small>${["queued", "check_failed"].includes(t.status) ? "Wallets not checked" : `${(t.wallets || []).length} attributed buyers`}</small></button>`).join("") : `<p>No matching observations in this bounded universe.</p>`;
  document.querySelectorAll(".rh-token").forEach(button => button.addEventListener("click", () => {
    selected = button.dataset.key;
    document.body.classList.add("rh-detail-open");
    render();
    if (innerWidth <= 760) window.scrollTo(0, 0);
  }));
  const t = payload.tokens.find(t => t.key === selected);
  if (!t) { $("#detail").innerHTML = "<p>Select a token</p>"; return; }
  const wallets = t.wallets || [];
  $("#detail").innerHTML = `<button id="back" class="rh-back icon-button" aria-label="Back to tokens" title="Back to tokens"><img src="icons/arrow-left.svg" alt=""></button><h2>${esc(t.symbol)} <span class="rh-badge">${esc(labels[t.status] || "Unknown")}</span></h2>
    <p>${esc(t.token)}</p><p><a href="https://dexscreener.com/robinhood/${t.pool}" target="_blank" rel="noopener noreferrer">Market chart</a> / <a href="https://robinhoodchain.blockscout.com/token/${t.token}" target="_blank" rel="noopener noreferrer">Explorer</a></p>
    <p class="rh-warning">Research observation, not a confirmed accumulation signal. Contract restrictions, taxes and wallet connections have not been checked.</p>
    ${!fresh || ["queued", "check_failed"].includes(t.status) ? '<p class="rh-warning">No current verified wallet conclusion.</p>' : ""}
    <dl><dt>First observed</dt><dd>${esc(date(t.first_observed_at))}</dd><dt>Wallet check</dt><dd>${esc(date(t.checked_at))}</dd><dt>Pool created</dt><dd>${esc(date(t.pool_created_at))}; token age unknown</dd><dt>Market cap / FDV</dt><dd>${money(t.market_cap_usd)} / ${money(t.fdv_usd)}</dd><dt>Swap window</dt><dd>${t.window_from_block ?? "?"} - ${t.window_to_block ?? "?"} blocks</dd><dt>Buys / sells</dt><dd>${t.buy_swaps ?? "?"} / ${t.sell_swaps ?? "?"} swap events</dd><dt>Buy receipts checked</dt><dd>${t.receipts_checked ?? 0} / ${t.buy_transactions ?? "?"}</dd><dt>Attribution</dt><dd>${t.attribution_complete ? "All buy swaps attributed" : "Partial or not checked"}</dd><dt>Held supply upper bound</dt><dd>${pct(t.retained_supply_upper_bound_pct)}</dd></dl>
    <p>Held is capped at the observed purchases. Pre-existing balances or transfers can inflate this bound; it is not proven retained accumulation. Unattributed routed buys are excluded.</p>
    ${t.error ? `<p class="rh-warning">${esc(t.error)}</p>` : ""}
    <table class="rh-wallets"><thead><tr><th>Buyer</th><th>Held upper bound</th><th>Supply upper bound</th></tr></thead><tbody>${wallets.map(w => `<tr><td><a href="https://robinhoodchain.blockscout.com/address/${esc(w.address)}" target="_blank" rel="noopener noreferrer">${esc(w.address.slice(0, 8))}...${esc(w.address.slice(-6))}</a></td><td>${pct(w.retention_upper_bound_pct)}</td><td>${pct(w.supply_upper_bound_pct)}</td></tr>`).join("")}</tbody></table>`;
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
