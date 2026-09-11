import {formatSupplyPercent} from "./robinhood-state.js?v=20260911-evidence-1";

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const fact = (label, value) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
const walletLink = value => /^0x[0-9a-f]{40}$/.test(value || "")
  ? `<a href="https://robinhoodchain.blockscout.com/address/${value}" target="_blank" rel="noopener noreferrer">${value.slice(0, 8)}...${value.slice(-6)}</a>` : esc(value);
const chainName = id => ({1:"Ethereum",792703809:"Solana",4663:"Robinhood"})[id] || `Chain ${id}`;

export function accumulationSummary(token) {
  const age = Date.now() - Date.parse(token.position_flow?.checked_at);
  if (token.position_flow?.status === "checked" && age >= -300000 && age < 90 * 60000 && token.position_flow.supply_pct?.transferred > 0)
    return `${formatSupplyPercent(token.position_flow.supply_pct.transferred)} supply traced to recipients`;
  if (token.preparation?.status === "coincidences") return "Funding / timing coincidences";
  const cross = token.cross_chain_at_catch || token.cross_chain;
  return cross?.verified_buys > 0 ? `${cross.recipients} cross-chain recipients${token.cross_chain_at_catch ? " at catch" : " in window"}` : "";
}

export function renderAccumulationEvidence(token) {
  const flow = token.position_flow, cross = token.cross_chain_at_catch || token.cross_chain, prep = token.preparation;
  const age = Date.now() - Date.parse(flow?.checked_at);
  const checked = flow?.status === "checked" && Number.isFinite(age) && age >= -300000 && age < 90 * 60000;
  const pct = key => checked ? formatSupplyPercent(flow.supply_pct?.[key]) : "Not verified";
  const recipients = checked ? flow.recipients || [] : [];
  const groups = prep?.status === "coincidences" ? prep.groups || [] : [];
  return `<section class="accumulation-evidence"><div class="section-heading"><h3>Where the position went</h3><span class="evidence-time">${checked ? `Block ${esc(flow.checked_block)}` : "Check pending"}</span></div>
    <div class="evidence-facts">${fact("At original buyers (min.)", pct("original"))}${fact("At transfer recipients (min.)", pct("transferred"))}${fact("Confirmed sold (min.)", pct("sold"))}${fact("Unresolved", pct("unknown"))}</div>
    <p class="muted">Percent of total supply attributed to the original buys. Tracking starts at ${flow?.from_block != null ? `block ${esc(flow.from_block)}` : "the first position checkpoint"}; earlier outflows remain unresolved. Transferred tokens do not prove the same owner. ${esc(flow?.limits || "")}</p>
    ${!checked ? `<p class="warning">${esc(flow?.reason || "No position trace available yet. Original-wallet balances are shown separately.")}</p>` : ""}
    ${recipients.length ? `<details><summary>${recipients.length} transfer recipients</summary><div class="table-wrap"><table><thead><tr><th>Recipient</th><th>Supply min.</th><th>Hops</th></tr></thead><tbody>${recipients.map(w => `<tr><td>${walletLink(w.address)}</td><td>${formatSupplyPercent(w.supply_pct)}</td><td>${esc(w.depth)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}</section>
    <section class="accumulation-evidence"><div class="section-heading"><h3>Cross-chain inflow</h3><span class="evidence-time">${token.cross_chain_at_catch ? "At catch" : "Current window"}</span></div><div class="evidence-facts">
    ${fact("Verified purchases", cross ? `${cross.verified_buys} / ${cross.recipients} recipients` : "Not checked")}${fact("Gross bought supply", cross ? formatSupplyPercent(cross.gross_bought_supply_pct) : "Unknown")}${fact("Source chains", cross?.source_chains?.map(chainName).join(", ") || "Not established")}${fact("Verified routes", cross?.verified_services?.join(", ") || "None in checked subset")}</div>
    <p class="muted">Verified Relay routes only; other services and normal-activity baseline are unverified. Gross purchases are not current holdings. Shared infrastructure is not an ownership link.</p></section>
    <section class="accumulation-evidence"><div class="section-heading"><h3>Buyer preparation</h3></div><div class="evidence-facts">${fact("Result", groups.length ? `${groups.length} funding / timing coincidences` : prep?.status === "no_match_in_checked_subset" ? "No match in checked subset" : "Not verified")}${fact("Coverage", prep?.scope || "Source-chain funding not checked")}</div>
    ${groups.map(g => `<details><summary>${esc(g.wallet_count)} buyers / unclassified funding source</summary><p>${walletLink(g.source)}</p><p>Funding span ${esc(g.funding_span_seconds)}s / buy span ${esc(g.buy_span_seconds)}s</p><p>${g.wallets.map(walletLink).join(" / ")}</p></details>`).join("")}
    <p class="muted">Partial Robinhood quote-token history before selected buys. Native funding and funding on source chains are not covered. A shared payer may be a service; no common owner is established.</p></section>`;
}
