import {validSnapshot, isFresh, selectTokens, formatSupplyPercent, walletFresh, supplyRange,
  REVIEW_GROUPS, reviewGroup, positionBounds, marketFresh, comparePositions} from "./robinhood-state.js?v=20260907-unified-1";

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const money = value => value == null || !Number.isFinite(Number(value)) ? "Unknown" : new Intl.NumberFormat("en", {style:"currency", currency:"USD", notation:"compact", maximumFractionDigits:1}).format(value);
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString(undefined, {day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit"}) : "Not checked";
const labels = {buy_wave:"New buy wave", retained:"Holding confirmed", reduced:"Cohort reduced", risk:"Contract risk", observed:"Activity only", queued:"Waiting for check", check_failed:"Check unavailable", needs_data:"Check needed"};
const state = {payload:null, query:"", group:"overview", sort:"caught", protocol:"all", tab:"filters", detailTab:"overview", selected:null, mobile:false, scrollY:0, error:"", expanded:new Set(REVIEW_GROUPS.map(g => g.id))};
const avatar = (token, compact = false) => `<span class="token-avatar${compact ? " is-compact" : ""}" aria-hidden="true">${esc([...String(token.symbol || "?")].slice(0, 2).join(""))}</span>`;
const fact = (name, value) => `<div><span>${esc(name)}</span><strong>${esc(value)}</strong></div>`;
const metric = (name, value, note) => `<div class="detail-metric"><span>${esc(name)}</span><strong>${esc(value)}</strong><small class="muted">${esc(note)}</small></div>`;
function positionText(bounds) {
  if (!bounds) return "Unknown";
  const pct = n => n > 0 && n < 0.1 ? "<0.1%" : `${Number(n.toFixed(1))}%`;
  if (bounds.lower == null) return `Up to ${pct(bounds.upper)}`;
  return Math.abs(bounds.lower - bounds.upper) < 0.00001 ? pct(bounds.upper) : `${pct(bounds.lower).replace(/%$/, "")}\u2013${pct(bounds.upper)}`;
}
function reason(t) {
  const group = reviewGroup(t, state.payload);
  if (group === "risk") return "Contract risk flagged";
  if (group === "needs_data") return t.status === "queued" ? "Waiting for wallet check" : "Wallet evidence needs a fresh check";
  if (group === "retained") return "Original buyers still retain tokens";
  if (group === "reduced") return "Original buyer position reduced";
  if (group === "buy_wave") return "Buy wave; retention not yet confirmed";
  return t.attribution_complete ? "Activity only; no confirmed buy wave" : "Partial buy attribution";
}
function filtered() {
  return selectTokens(state.payload?.tokens || [], state.query)
    .filter(t => state.protocol === "all" || t.protocol === state.protocol);
}
function row(t) {
  const fresh = isFresh(state.payload) && walletFresh(t);
  return `<button class="review-row${t.key === state.selected ? " is-selected" : ""}" data-token-key="${esc(t.key)}" aria-pressed="${t.key === state.selected}" type="button">
    <span class="review-identity">${avatar(t, true)}<span class="review-copy"><strong>${esc(t.symbol)}</strong><span class="review-reason">${esc(reason(t))}</span><small>Observed ${esc(date(t.first_observed_at))}</small></span></span>
    <span class="review-position"><strong>${esc(positionText(positionBounds(t)))}</strong><small>${esc(supplyRange(t))} supply</small><small class="${fresh ? "" : "warning"}">${fresh ? "checked" : "check overdue"}${!t.attribution_complete && !t.cohort_created_at ? " / partial" : ""}</small></span>
    <span class="review-market"><strong>${marketFresh(t) ? money(t.fdv_usd) : "Unverified"}</strong><small class="muted">FDV / ${esc(t.protocol || "v3")}</small></span>
  </button>`;
}
function overview(t) {
  const bounds = positionBounds(t), fresh = isFresh(state.payload) && walletFresh(t);
  return `<section><div class="section-heading"><h3>${t.cohort_created_at ? "Original buyer position" : "Observed buyer position"}</h3><span class="evidence-time">${esc(date(t.checked_at))}</span></div>
    <div class="retention-summary"><strong>${esc(positionText(bounds))}</strong><span>of attributed buys remaining<small>${fresh ? "Checked" : "Previous check"} / ${t.wallets.length} wallets</small></span></div>
    ${bounds?.lower != null ? `<meter class="retention-meter" min="0" max="100" value="${bounds.lower}" aria-label="Conservative retained position">${bounds.lower}%</meter>` : ""}
    <div class="evidence-facts">${fact("Retained supply", supplyRange(t))}${fact("Original cohort", t.cohort_created_at ? date(t.cohort_created_at) : "Not confirmed")}${fact("Retention checks", t.cohort_checks ?? 0)}${fact("Buy attribution", `${t.attributed_buy_transactions ?? 0} / ${t.buy_transactions ?? "?"} transactions`)}${fact("History window", t.history_complete ? "Latest window complete" : "Incomplete")}</div>
    </section><section class="decision-caveats"><h3>Assessment</h3><p>${esc(reason(t))}. ${t.cohort_created_at ? "Later unrelated buyers do not replace this cohort." : "Observed activity has not established a retained accumulation signal."}</p><p>Transfers and sales both reduce the conservative holding bound. Holding is not a new entry signal.</p>${t.error ? `<p class="warning">${esc(t.error)}</p>` : ""}</section>`;
}
function wallets(t) {
  if (!t.wallets.length) return `<div class="review-empty"><img src="icons/scan-search.svg" alt=""><h3>No attributed buyers</h3><p>Available evidence does not establish buyer balances. This does not mean that no purchases occurred.</p></div>`;
  return `<div class="section-heading"><h3>${t.cohort_created_at ? "Original buyers" : "Current-window buyers"}</h3><span class="evidence-time">${esc(date(t.checked_at))}</span></div>
    <div class="table-wrap"><table class="network-wallet-table"><thead><tr><th>Wallet</th><th>Position left</th><th>Supply max.</th></tr></thead><tbody>${t.wallets.map(w => `<tr><td><a href="https://robinhoodchain.blockscout.com/address/${w.address}" target="_blank" rel="noopener noreferrer">${esc(w.address.slice(0, 8))}...${esc(w.address.slice(-6))}</a></td><td>${esc(positionText(positionBounds({wallets:[w]})))}</td><td>${esc(formatSupplyPercent(w.supply_upper_bound_pct))}</td></tr>`).join("")}</tbody></table></div>`;
}
function supply(t) {
  const security = t.security || {status:"unknown", flags:[], unknown:["not_checked"]};
  return `<div class="section-heading"><h3>Supply & contract checks</h3><span class="evidence-time">${esc(date(security.checked_at))}</span></div><div class="evidence-facts">
    ${fact("Retained supply", supplyRange(t))}${fact("Total supply", t.total_supply_raw && t.decimals != null ? new Intl.NumberFormat("en", {maximumFractionDigits:2}).format(Number(t.total_supply_raw) / 10 ** t.decimals) : "Unknown")}
    ${fact("Contract risk", security.status === "risk" ? (security.flags || []).join(", ") : security.status === "no_flags" ? "No flags reported" : "Incomplete checks")}
    ${fact("Unknown checks", (security.unknown || []).join(", ") || "None reported")}${fact("Buy tax", security.buy_tax == null ? "Unknown" : formatSupplyPercent(security.buy_tax * 100))}${fact("Sell tax", security.sell_tax == null ? "Unknown" : formatSupplyPercent(security.sell_tax * 100))}
    ${fact("Source", security.source || "Not available")}</div><section class="decision-caveats"><h3>Evidence limits</h3><p>The lower bound subtracts outgoing transfers; the upper bound is capped by wallet balance and attributed buys. Unknown taxes are not zero. Contract checks do not guarantee sellability.</p></section>`;
}
function evidence(t) {
  return `<div class="section-heading"><h3>Observation evidence</h3><span class="evidence-time">${esc(date(t.checked_at))}</span></div><div class="evidence-facts">
    ${fact("First observed", date(t.first_observed_at))}${fact("Token created", t.token_age_verified ? date(t.token_created_at) : "Age not verified")}${fact("Market checked", `${date(t.market_checked_at)}${marketFresh(t) ? "" : " / stale"}`)}${fact("Reported FDV", money(t.fdv_usd))}${fact("Provider market cap", money(t.market_cap_usd))}${fact("Buys / sells", `${t.buy_swaps ?? "?"} / ${t.sell_swaps ?? "?"} events`)}${fact("Buy attribution", `${t.attributed_buy_transactions ?? 0} / ${t.buy_transactions ?? "?"} transactions`)}${fact("Indexed blocks", `${t.history_from_block ?? "?"} - ${t.indexed_through_block ?? "?"}`)}${fact("Backlog", `${t.backlog_blocks ?? "?"} blocks`)}${fact("Token", t.token)}${fact("Pool", t.pool)}</div>
    <section class="decision-caveats"><h3>Coverage</h3><p>One selected Uniswap ${esc(t.protocol || "v3")} pool. Indexing begins at first observation, not token launch. Ambiguous routes are excluded from buyer attribution.</p></section>`;
}
function detail(t) {
  if (!t) return `<aside class="detail token-detail no-selection"><img src="icons/scan-search.svg" alt=""><h2>No token selected</h2></aside>`;
  const group = REVIEW_GROUPS.find(g => g.id === reviewGroup(t, state.payload));
  const tabs = [["overview","Overview"], ["wallets","Wallets"], ["supply","Supply"], ["evidence","Evidence"]];
  const content = state.detailTab === "wallets" ? wallets(t) : state.detailTab === "supply" ? supply(t) : state.detailTab === "evidence" ? evidence(t) : overview(t);
  return `<aside class="detail token-detail"><button class="detail-back" type="button"><img src="icons/arrow-left.svg" alt=""> Tokens</button>
    <div class="detail-head"><div class="detail-identity">${avatar(t)}<div><h2>${esc(t.symbol)}</h2><p class="token-identity-sub">${esc(t.name)} / Uniswap ${esc(t.protocol || "v3")}</p><span class="position-status ${group.tone}">${esc(labels[group.id])}</span></div></div>
    <div class="detail-actions"><a class="secondary-action detail-link" href="https://dexscreener.com/robinhood/${t.pool}" target="_blank" rel="noopener noreferrer">Chart <img src="icons/arrow-up-right.svg" alt=""></a><a class="icon-button" href="https://robinhoodchain.blockscout.com/token/${t.token}" aria-label="Token explorer" title="Token explorer" target="_blank" rel="noopener noreferrer"><img src="icons/arrow-up-right.svg" alt=""></a><button class="icon-button copy-address" data-address="${t.token}" aria-label="Copy token address" title="Copy token address"><img src="icons/copy.svg" alt=""></button></div></div>
    <div class="detail-load-state" role="status">${!isFresh(state.payload) || !walletFresh(t) ? `Wallet evidence is not current. Last check: ${esc(date(t.checked_at))}.` : ""}</div>
    <div class="decision-grid">${metric("Current FDV", marketFresh(t) ? money(t.fdv_usd) : "Unverified", `${money(t.liquidity_usd)} liquidity${marketFresh(t) ? "" : " / last quote"}`)}${metric("Position left", positionText(positionBounds(t)), "Bounds on attributed buys")}${metric(t.cohort_created_at ? "Cohort supply" : "Observed supply", supplyRange(t), `${t.wallets.length} attributed wallets`)}</div>
    <div class="detail-tabs" role="tablist" aria-label="Token research sections">${tabs.map(([id,label]) => `<button class="detail-tab${state.detailTab === id ? " is-active" : ""}" id="detail-tab-${id}" data-detail-tab="${id}" role="tab" aria-selected="${state.detailTab === id}" tabindex="${state.detailTab === id ? 0 : -1}" aria-controls="token-research-panel">${label}${id === "wallets" ? `<span>${t.wallets.length}</span>` : ""}</button>`).join("")}</div>
    <div class="detail-tab-panel" id="token-research-panel" role="tabpanel" aria-labelledby="detail-tab-${state.detailTab}">${content}</div></aside>`;
}

function render({resetList = false} = {}) {
  if (!state.payload) return;
  const listScroll = resetList ? 0 : $(".review-list")?.scrollTop || 0;
  const detailScroll = $(".token-detail")?.scrollTop || 0;
  const previousKey = $(".review-row.is-selected")?.dataset.tokenKey;
  const p = state.payload, fresh = isFresh(p);
  $("#subtitle").textContent = `Last scan ${date(p.generated_at)}${fresh ? "" : " / stale"}`;
  $("#scannerSummary").textContent = state.error || `${p.checked_pools ?? 0} checked / ${p.eligible_tokens ?? 0} candidates${p.errors?.length ? ` / ${p.errors.length} incomplete checks` : " / hourly scans"}`;
  $("#statusRow").textContent = (p.errors || []).join(" | ") || p.provider || "Provider information unavailable";
  $("#metrics").innerHTML = [metric("Pools discovered", p.discovered_pools ?? 0, "Uniswap v3 + v4"), metric("Stock pools excluded", p.excluded_stocks ?? 0, "Official contract registry"), metric("RPC requests", p.rpc_calls ?? 0, "Current scan"), metric("Last attempt", date(p.attempted_at), "Bounded discovery")].join("");
  const all = filtered(), counts = Object.fromEntries(REVIEW_GROUPS.map(g => [g.id, all.filter(t => reviewGroup(t, p) === g.id).length]));
  const groups = REVIEW_GROUPS.map(g => ({...g, tokens:all.filter(t => reviewGroup(t, p) === g.id).sort((a,b) => comparePositions(a,b,state.sort))}));
  const visible = groups.filter(g => state.group === "overview" || state.group === g.id);
  const open = visible.flatMap(g => state.group !== "overview" || state.expanded.has(g.id) || state.query ? g.tokens : []);
  if (!open.some(t => t.key === state.selected)) state.selected = open[0]?.key ?? null;
  const t = open.find(t => t.key === state.selected);
  const sortControl = state.tab === "alerts" ? "" : `<label class="sort-control">Sort within groups<select id="reviewSort" aria-label="Sort within groups"><option value="caught" ${state.sort === "caught" ? "selected" : ""}>Newest catch</option><option value="retained" ${state.sort === "retained" ? "selected" : ""}>Most retained</option></select></label>`;
  const heading = `<div class="radar-heading"><div><h2>${state.tab === "alerts" ? "Latest checks" : "Accumulation radar"}</h2><p>Uniswap v3 + v4 / 1-15d / ${all.length} observed tokens</p></div>${sortControl}</div>`;
  const list = visible.filter(g => g.tokens.length).map(g => {
    const expanded = state.group !== "overview" || state.expanded.has(g.id) || Boolean(state.query);
    return `<section class="review-section"><button class="queue-heading ${g.tone}" data-expand-queue="${g.id}" aria-expanded="${expanded}"><span><img src="icons/chevron-down.svg" alt=""><strong>${g.label}</strong><span class="queue-count">${g.tokens.length}</span></span></button>${expanded ? g.tokens.map(row).join("") : ""}</section>`;
  }).join("");
  $("#content").innerHTML = heading + (state.tab === "alerts" ? `<div class="table-wrap network-checks"><table><thead><tr><th>Wallet check</th><th>Token</th><th>Status</th><th>Attributed buys</th><th>Supply held</th></tr></thead><tbody>${[...all].sort((a,b) => (Date.parse(b.checked_at) || 0) - (Date.parse(a.checked_at) || 0)).map(t => `<tr><td>${esc(date(t.checked_at))}</td><td><button class="network-token-link" data-open-token="${esc(t.key)}">${esc(t.symbol)}</button></td><td>${esc(labels[reviewGroup(t,p)])}</td><td>${t.attributed_buy_transactions ?? 0} / ${t.buy_transactions ?? "?"}</td><td>${esc(supplyRange(t))}</td></tr>`).join("")}</tbody></table>${!all.length ? '<div class="review-empty"><h3>No matching checks</h3></div>' : ""}</div>` : `
    <div class="queue-nav" role="group" aria-label="Position views"><button data-review-queue="overview" aria-pressed="${state.group === "overview"}">Overview</button>${REVIEW_GROUPS.map(g => `<button data-review-queue="${g.id}" aria-pressed="${state.group === g.id}">${g.label}<span>${counts[g.id]}</span></button>`).join("")}</div>
    <div class="review-workspace${state.mobile && t ? " is-detail-open" : ""}"><section class="review-list-panel" aria-label="Tokens by position state"><div class="review-table-head"><span>Token / last observation</span><span title="Bounds on attributed purchases remaining">Position left</span><span>Current FDV</span></div><div class="review-list">
    ${state.group === "overview" && !counts.buy_wave ? '<div class="no-confirmation"><span class="quiet-indicator"></span><span><strong>No new buy waves</strong><small>Observed tokens remain listed below.</small></span></div>' : ""}
    ${list || '<div class="review-empty"><img src="icons/scan-search.svg" alt=""><h3>No matching tokens</h3><button id="resetReview" type="button">All tokens</button></div>'}<div class="list-footer">${visible.reduce((sum,g) => sum + g.tokens.length,0)} tokens / ${state.sort === "caught" ? "Newest observation" : "Highest retention"} first within groups</div></div></section>${detail(t)}</div>`);
  bind();
  if ($(".review-list")) $(".review-list").scrollTop = listScroll;
  if (previousKey === state.selected && $(".token-detail")) $(".token-detail").scrollTop = detailScroll;
}

function openToken(key) {
  state.scrollY = window.scrollY; state.selected = key; state.mobile = true; state.detailTab = "overview";
  render();
  if (matchMedia("(max-width: 1000px)").matches) {
    $(".review-workspace")?.scrollIntoView({block:"start"});
    $(".detail-back")?.focus({preventScroll:true});
  }
}
function bind() {
  document.querySelectorAll("[data-token-key]").forEach(b => b.onclick = () => openToken(b.dataset.tokenKey));
  document.querySelectorAll("[data-open-token]").forEach(b => b.onclick = () => { setTab("filters"); state.group = "overview"; state.expanded = new Set(REVIEW_GROUPS.map(g => g.id)); openToken(b.dataset.openToken); });
  document.querySelectorAll("[data-review-queue]").forEach(b => b.onclick = () => { state.group = b.dataset.reviewQueue; state.mobile = false; state.selected = null; render({resetList:true}); document.querySelector(`[data-review-queue="${state.group}"]`)?.focus({preventScroll:true}); });
  document.querySelectorAll("[data-expand-queue]").forEach(b => b.onclick = () => { if (state.group !== "overview" || state.query) return; const id = b.dataset.expandQueue; state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id); render(); document.querySelector(`[data-expand-queue="${id}"]`)?.focus({preventScroll:true}); });
  if ($("#reviewSort")) $("#reviewSort").onchange = e => { state.sort = e.target.value; render({resetList:true}); $("#reviewSort").focus(); };
  if ($("#resetReview")) $("#resetReview").onclick = () => { state.group = "overview"; state.query = ""; state.protocol = "all"; $("#searchInput").value = ""; $("#protocolFilter").value = "all"; render(); };
  if ($(".detail-back")) $(".detail-back").onclick = () => { state.mobile = false; render(); window.scrollTo(0, state.scrollY); $(".review-row.is-selected")?.focus({preventScroll:true}); };
  document.querySelectorAll(".detail-tab").forEach(b => {
    b.onclick = () => { state.detailTab = b.dataset.detailTab; const y = $(".token-detail").scrollTop; render(); $(".token-detail").scrollTop = y; $(`#detail-tab-${state.detailTab}`).focus({preventScroll:true}); };
    b.onkeydown = e => {
      if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
      e.preventDefault(); const tabs = [...document.querySelectorAll(".detail-tab")], i = tabs.indexOf(b);
      tabs[e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length].click();
    };
  });
  if ($(".copy-address")) $(".copy-address").onclick = async e => {
    const address = e.currentTarget.dataset.address;
    try { await navigator.clipboard.writeText(address); notice("Token address copied."); }
    catch { notice(`Token address: ${address}`); }
  };
}
function notice(message) { $("#appNotice span").textContent = message; $("#appNotice").hidden = false; }
function setTab(tab) {
  state.tab = tab; state.mobile = false;
  document.querySelectorAll(".tab").forEach(b => { const active = b.dataset.tab === tab; b.classList.toggle("is-active",active); b.setAttribute("aria-selected",String(active)); b.tabIndex = active ? 0 : -1; });
  $("#content").setAttribute("aria-labelledby", tab === "alerts" ? "tab-events" : "tab-radar");
}
async function load() {
  $("#refresh").disabled = true;
  try {
    const response = await fetch(`data/robinhood.json?t=${Date.now()}`, {cache:"no-store", signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error(`Robinhood data unavailable (${response.status})`);
    const next = await response.json();
    if (!validSnapshot(next)) throw new Error("Invalid Robinhood dataset; networks were not mixed");
    if (state.payload && Date.parse(next.generated_at) < Date.parse(state.payload.generated_at)) throw new Error("Older snapshot received; previous results retained");
    state.payload = next; state.error = ""; render();
  } catch (error) {
    state.error = error.message;
    if (state.payload) { render(); notice(`${error.message}. Previous results retained.`); }
    else { $("#subtitle").textContent = "Robinhood data unavailable"; $("#scannerSummary").textContent = error.message; $("#content").innerHTML = '<div class="review-empty"><img src="icons/scan-search.svg" alt=""><h3>Observations unavailable</h3><p>The dataset could not be loaded.</p><button id="retryData">Retry</button></div>'; $("#retryData").onclick = load; }
  } finally { $("#refresh").disabled = false; }
}

document.title = "Robinhood | Radar";
document.body.classList.add("is-radar");
$("h1").textContent = "Robinhood Radar";
$(".brand-mark").textContent = "RR";
$("#runScan").disabled = true;
for (const id of ["#tab-intelligence", "#tab-narratives"]) { $(id).disabled = true; $(id).title = "Not available on Robinhood"; $(id).setAttribute("aria-label", `${$(id).textContent}: not available on Robinhood`); }
$("#advancedFilters").innerHTML = '<label class="field-control"><span>Protocol</span><select id="protocolFilter" aria-label="Protocol"><option value="all">All protocols</option><option value="v3">Uniswap v3</option><option value="v4">Uniswap v4</option></select></label>';
$("#protocolFilter").onchange = e => { state.protocol = e.target.value; state.mobile = false; render({resetList:true}); };
$("#filterToggle").onclick = () => { const open = $(".filters").classList.toggle("is-open"); $("#filterToggle").setAttribute("aria-expanded",String(open)); };
document.addEventListener("keydown", e => { if (e.key === "Escape") { $(".filters").classList.remove("is-open"); $("#filterToggle").setAttribute("aria-expanded","false"); } });
$("#searchInput").oninput = e => { state.query = e.target.value; state.mobile = false; render({resetList:true}); };
$("#refresh").onclick = load;
$("#dismissNotice").onclick = () => { $("#appNotice").hidden = true; };
document.querySelectorAll(".tab:not(:disabled)").forEach(b => {
  b.onclick = () => { setTab(b.dataset.tab); render(); };
  b.onkeydown = e => { if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return; e.preventDefault(); const tabs = [...document.querySelectorAll(".tab:not(:disabled)")], i = tabs.indexOf(b); const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length; tabs[next].click(); tabs[next].focus(); };
});
setTab("filters");
load();
