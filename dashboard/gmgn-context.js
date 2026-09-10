const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const num = n => typeof n === "number" && Number.isFinite(n) && n >= 0;
const price = n => num(n) ? `$${n.toPrecision(5)}` : "Unknown";
const money = n => num(n) ? new Intl.NumberFormat("en", {style:"currency", currency:"USD", notation:"compact"}).format(n) : "Unknown";
const date = t => Number.isFinite(Date.parse(t)) ? new Date(t).toLocaleString() : "Not checked";
const fact = (label, value) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;

export function gmgnUrl(token) {
  return /^0x[0-9a-f]{40}$/.test(token || "") ? `https://gmgn.ai/robinhood/token/${token}` : "";
}

export function gmgnMarket(token, now = Date.now()) {
  const info = token?.gmgn?.info;
  if (!info || info.chain_id !== 4663 || info.token !== token.token) return null;
  const age = now - Date.parse(info.checked_at);
  const fresh = info.status === "ok" && Number.isFinite(age) && age >= -300000 && age < 90 * 60000;
  const ath = info.ath;
  const verified = ath?.identity_version === 2 && ath.token_address === token.token;
  const high = verified && num(ath.highest_price) && ath.highest_price > 0 ? ath.highest_price : null;
  const current = num(info.price_usd) && info.price_usd > 0 ? info.price_usd : null;
  return {info, fresh, high, current, drawdown: fresh && high && current && current <= high ? (1 - current / high) * 100 : null};
}

export function renderGmgnMarket(token, now = Date.now()) {
  const context = gmgnMarket(token, now);
  if (!context) return `<section class="decision-caveats"><h3>GMGN context</h3><p>Not available. On-chain checks are shown separately.</p></section>`;
  const {info, fresh, high, current, drawdown} = context;
  const hour = info.activity?.["1h"] || {}, tags = info.wallet_tags || {};
  return `<section><div class="section-heading"><h3>GMGN market context</h3><span class="evidence-time">${fresh ? "Checked" : "Previous quote"} / ${esc(date(info.checked_at))}</span></div><div class="evidence-facts">
    ${fact("Token price", price(current))}${fact("ATH price / GMGN", price(high))}${fact("Below price ATH", drawdown == null ? "Unverified" : `${drawdown.toFixed(1)}%`)}
    ${fact("1h buy / sell volume", `${money(hour.buy_volume)} / ${money(hour.sell_volume)}`)}${fact("1h buys / sells", `${hour.buys ?? "?"} / ${hour.sells ?? "?"}`)}${fact("GMGN holders", info.holder_count ?? "Unknown")}
    ${fact("GMGN wallet labels", `Smart ${tags.smart_wallets ?? "?"} / KOL ${tags.renowned_wallets ?? "?"} / Bundler ${tags.bundler_wallets ?? "?"}`)}
    </div><p class="muted">Provider-reported price ATH; peak date not verified. Wallet labels are not proof of insider activity.</p></section>`;
}

export function renderGmgnHolders(token) {
  const sample = token?.gmgn?.holders;
  if (!sample || sample.token !== token.token || !Array.isArray(sample.wallets)) return "";
  const rows = sample.wallets.filter(w => gmgnUrl(w.address));
  return `<details class="research-fold"><summary>GMGN holder sample</summary><section><div class="section-heading"><h3>Provider observations</h3><span class="evidence-time">${esc(date(sample.checked_at))} / ${esc(sample.status)}</span></div>
    <p class="muted">Top-holder sample, not the signal cohort. ${esc(sample.excluded)} infrastructure or unclassified addresses excluded. Provider totals, not the latest scan window.</p>
    <div class="table-wrap"><table class="network-wallet-table"><thead><tr><th>Holder</th><th>Supply</th><th>Buy / sell USD</th><th>Origin</th></tr></thead><tbody>${rows.map(w => `<tr><td><a href="https://robinhoodchain.blockscout.com/address/${w.address}" target="_blank" rel="noopener noreferrer">${esc(w.address.slice(0,8))}...${esc(w.address.slice(-6))}</a><small>${esc((w.tags || []).join(", "))}</small></td><td>${num(w.supply_fraction) ? (w.supply_fraction * 100).toFixed(2) + "%" : "Unknown"}</td><td>${esc(money(w.buy_volume_usd))} / ${esc(money(w.sell_volume_usd))}</td><td>${w.has_reported_buys ? "Reported buys" : num(w.transferred_in) && w.transferred_in > 0 ? "Transfer-in" : "Unknown"}</td></tr>`).join("")}</tbody></table></div></section></details>`;
}

export function renderGmgnSecurity(token) {
  const s = token?.gmgn?.security;
  if (!s?.flags) return "";
  return `<section><div class="section-heading"><h3>GMGN risk flags</h3><span class="evidence-time">${esc(date(s.checked_at))} / ${esc(s.status)}</span></div><div class="evidence-facts">${Object.entries(s.flags).map(([key,value]) => fact(key, value === true ? "Reported" : value === false ? "Not reported" : "Unknown")).join("")}</div><p class="muted">Independent provider assessment. Does not override on-chain evidence or guarantee sellability.</p></section>`;
}
