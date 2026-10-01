const DASH = "\u2014";
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const boundary = "Common control / exact bundle not established";
const text = (value, limit = 220) => typeof value === "string" && value.trim()
  ? value.trim().slice(0, limit) : "";

function number(value) {
  if (typeof value !== "number" && !(typeof value === "string" && /^\d+(\.\d+)?$/.test(value))) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function percent(value) {
  const parsed = number(value);
  if (parsed === null || parsed > 100) return DASH;
  return parsed > 0 && parsed < 0.01 ? "<0.01%" : `${parsed.toFixed(2)}%`;
}

function count(value) {
  const parsed = number(value);
  return Number.isSafeInteger(parsed) ? String(parsed) : DASH;
}

function describe(value) {
  if (typeof value === "string") return text(value, 180) || DASH;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.slice(0, 3).map(item => text(item, 60)).filter(Boolean).join(" / ") || DASH;
  if (value && typeof value === "object") {
    return Object.entries(value).filter(([, item]) => item != null && ["string", "number", "boolean"].includes(typeof item))
      .slice(0, 3).map(([key, item]) => `${text(key.replace(/_/g, " "), 40)}: ${describe(item)}`).join(" / ") || DASH;
  }
  return DASH;
}

function checkAge(value, now) {
  const checked = typeof value === "string" && value.trim() ? Date.parse(value) : NaN;
  const age = now - checked;
  if (!Number.isFinite(now) || !Number.isFinite(checked) || age < -300000) {
    return {label:`Check age ${DASH} / time unverified`, suffix:` / age ${DASH}`};
  }
  const minutes = Math.floor(Math.max(0, age) / 60000);
  const elapsed = minutes < 1 ? "<1m" : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`;
  const stale = age >= 90 * 60000;
  return {label:`${stale ? "Previous check" : "Checked"} ${elapsed} ago`, suffix:stale ? " / previous" : ""};
}

function solanaAddress(value) {
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value)) return false;
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let decoded = 0n;
  for (const char of value) decoded = decoded * 58n + BigInt(alphabet.indexOf(char));
  let bytes = 0;
  while (decoded > 0n) { bytes++; decoded >>= 8n; }
  return bytes + (value.match(/^1+/)?.[0].length || 0) === 32;
}

function memberLink(member, network) {
  const address = typeof member === "string" ? member : member?.address;
  if (typeof address !== "string" || !address) return "";
  const chain = typeof member === "object" ? member.chain_id ?? member.network ?? member.chain : null;
  const sameChain = chain == null || (network === "solana" ? ["solana", 792703809, "792703809"] : ["robinhood", 4663, "4663"]).includes(chain);
  const valid = network === "solana" ? solanaAddress(address) : network === "robinhood" && /^0x[0-9a-fA-F]{40}$/.test(address);
  const label = esc(address.length > 18 ? `${address.slice(0, 8)}...${address.slice(-6)}` : text(address, 40));
  if (!sameChain || !valid) return label;
  const base = network === "solana" ? "https://solscan.io/account/" : "https://robinhoodchain.blockscout.com/address/";
  return `<a href="${base}${encodeURIComponent(address)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

export function resolveCoordinatedActivity(token, network = "solana") {
  if (network === "robinhood") return token?.coordinated_activity || null;
  if (token?.signalThesis?.coordinated_activity) return token.signalThesis.coordinated_activity;
  const alerts = Array.isArray(token?.alerts) ? token.alerts : [];
  let latest = null, latestTime = -Infinity;
  for (const alert of alerts) {
    if (!alert?.coordinated_activity) continue;
    const time = Date.parse(alert.created_at || alert.window_end || alert.window_start);
    const orderedTime = Number.isFinite(time) ? time : -Infinity;
    if (!latest || orderedTime >= latestTime) {
      latest = alert.coordinated_activity;
      latestTime = orderedTime;
    }
  }
  return latest;
}

export function renderCoordinatedActivity(result, {network = "solana", now, compact = false} = {}) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return "";
  const flagged = ["pattern", "coordination_watch"].includes(result.status);
  const material = result.metrics?.material_pattern === true;
  if (compact && (!flagged || !material)) return "";
  const age = checkAge(result.checked_at, now);
  const label = result.status === "coordination_watch" ? "Funding-linked pattern" : material ? "Coordinated pattern" : "Supporting coincidence";
  const supply = percent(result.metrics?.material_union_held_supply_pct ?? result.metrics?.max_material_group_held_supply_pct);
  const badge = `<span class="chip warn" title="${esc(`${age.label}. Held supply at check. ${boundary}`)}">${esc(label)}${compact ? ` / ${esc(supply)} supply${esc(age.suffix)}` : ""}</span>`;
  if (compact) return badge;

  const limits = (Array.isArray(result.limitations) ? result.limitations : []).slice(0, 2).map(item => text(item, 140)).filter(Boolean);
  let content;
  if (!flagged) {
    const status = result.status === "no_pattern_in_checked_subset" ? `No pattern in checked subset. ${age.label}. ${boundary}.`
      : result.status === "not_checked" ? "Coordinated activity not checked."
        : "Coordinated activity status unverified.";
    content = `<span class="muted-inline">${esc(status)}${text(result.detail) ? ` ${esc(text(result.detail))}` : ""}${limits.length ? ` ${esc(limits.join(" / "))}` : ""}</span>`;
    if (result.status === "no_pattern_in_checked_subset") {
      content += `<div class="muted-inline">Scope: ${esc(describe(result.scope))} / Coverage: ${esc(describe(result.coverage))}</div>`;
    }
  } else {
    const signals = (Array.isArray(result.signals) ? result.signals : []).filter(signal => signal && typeof signal === "object").slice(0, 3);
    const explanations = signals.length ? signals.map(signal => {
      const label = text(signal.label, 80) || text(signal.family, 80) || text(signal.code, 80) || "Observed coincidence";
      const members = (Array.isArray(signal.members) ? signal.members : []).slice(0, 3).map(member => memberLink(member, network)).filter(Boolean).join(" / ");
      const detail = describe(signal.detail);
      return `<div><strong>${esc(label)}</strong>${signal.supporting_only === true ? " / supporting only" : ""}${detail !== DASH ? `: ${esc(detail)}` : ""} <span class="muted-inline">(${esc(count(signal.wallet_count))} wallets / ${esc(percent(signal.held_supply_pct))} supply at check)</span>${members ? `<div class="supply-evidence-members">${members}</div>` : ""}</div>`;
    }).join("") : `<div class="muted-inline">Signal detail unavailable; reported pattern remains bounded to the checked subset.</div>`;
    content = `${badge} <span class="muted-inline">${esc(supply)} held supply at check / ${esc(count(result.metrics?.buyer_count))} buyers / ${esc(age.label)}</span>
      ${explanations}
      <div class="muted-inline">Scope: ${esc(describe(result.scope))} / Coverage: ${esc(describe(result.coverage))}</div>
      <div class="muted-inline">${boundary}. Shared CEX / router / LI.FI / Relay markers are not ownership evidence.${limits.length ? ` ${esc(limits.join(" / "))}` : ""}</div>`;
  }
  return `<div class="kv"><span>Coordinated activity</span><div style="min-width:0;overflow-wrap:anywhere">${content}</div></div>`;
}
