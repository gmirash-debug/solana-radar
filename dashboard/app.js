const HIDDEN_TOKENS_KEY = "solana-radar:hidden-token-keys:v1";

function loadHiddenTokenKeys() {
  try {
    return new Set(JSON.parse(localStorage.getItem(HIDDEN_TOKENS_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function saveHiddenTokenKeys(keys) {
  try {
    localStorage.setItem(HIDDEN_TOKENS_KEY, JSON.stringify([...keys]));
  } catch {
    // Local hide state is an optional browser preference.
  }
}

const state = {
  report: null,
  history: [],
  market: {},
  scanStatus: {},
  tab: "filters",
  query: "",
  scope: "current",
  tier: "focus",
  heat: "all",
  lane: "all",
  minScore: 0,
  selectedTokenKey: null,
  selectedNarrative: null,
  selectedFilter: null,
  selectedAlertId: null,
  showHidden: false,
  hiddenTokenKeys: loadHiddenTokenKeys(),
  staticMode: false,
};

const els = {
  subtitle: document.querySelector("#subtitle"),
  statusRow: document.querySelector("#statusRow"),
  metrics: document.querySelector("#metrics"),
  content: document.querySelector("#content"),
  refresh: document.querySelector("#refresh"),
  runScan: document.querySelector("#runScan"),
  modeSelect: document.querySelector("#modeSelect"),
  searchInput: document.querySelector("#searchInput"),
  scopeFilter: document.querySelector("#scopeFilter"),
  tierFilter: document.querySelector("#tierFilter"),
  heatFilter: document.querySelector("#heatFilter"),
  laneFilter: document.querySelector("#laneFilter"),
  scoreInput: document.querySelector("#scoreInput"),
  showHiddenInput: document.querySelector("#showHiddenInput"),
  tabs: document.querySelectorAll(".tab"),
};

const FILTER_META = {
  incubation: {
    label: "Incubation",
    criteria: "3h-72h / universe $50k-$1.5m / actionable <=$150k / watch <=$300k / liq >= $3k",
    thesis: "early post-launch accumulation before the market fully reprices the token.",
  },
  young: {
    label: "Young",
    criteria: "3d-30d / universe $100k-$5m / actionable <=$750k / watch <=$1.5m / liq >= $10k",
    thesis: "post-launch accumulation while the token is still below breakout size.",
  },
  breakout: {
    label: "Breakout",
    criteria: "3d-30d / $5m-$25m mcap / liq >= $50k / 1h volume >= $100k",
    thesis: "momentum expansion after young-lane size, filtered for volume velocity and suspicious onchain flow.",
  },
  reactivation: {
    label: "Reactivation",
    criteria: "30d+ / $100k-$5m / <=40% ATH / actionable <=25% ATH / low-volume setup",
    thesis: "older tokens that are materially corrected from ATH and show low-volume accumulation or dormant-wallet activity.",
  },
  legacy: {
    label: "Legacy",
    criteria: "fallback for insufficient snapshots or old catches outside current filter rules",
    thesis: "historical scanner catches that are missing filter evidence or no longer fit the active lane rules.",
  },
};

const FILTER_ORDER = ["incubation", "young", "breakout", "reactivation", "legacy"];
const PUMPFUN_DEX_ALLOWLIST = new Set(["pumpfun-amm", "pumpswap", "pumpfun"]);
const HARD_WALLET_CLASSES = new Set(["fresh", "freshish", "dormant"]);
const SUPPORT_WALLET_CLASSES = new Set(["low_tx"]);
const TIER_META = {
  actionable: {
    label: "Actionable",
    tone: "good",
    rank: 4,
    summary: "hard onchain evidence before the move looks fully crowded",
  },
  watch: {
    label: "Watch",
    tone: "warn",
    rank: 3,
    summary: "real signal, but needs confirmation or cleaner market setup",
  },
  late_chase: {
    label: "Late/chase",
    tone: "bad",
    rank: 2,
    summary: "signal exists but it appears after an extended move or crowded volume",
  },
  noise: {
    label: "Noise",
    tone: "",
    rank: 1,
    summary: "weak or support-only evidence",
  },
};

const TOKEN_MARKET_CONTEXT = {
  HANTAYLiPiQ8d8dkJizcL8gJQHWBKF5ZeL1neeLqwbzc: {
    source: "Solana Tracker chart",
    launchStartMcapUsd: 2_456,
    launchHighMcapUsd: 394_916,
    day2LowMcapUsd: 91_648,
    day2HighMcapUsd: 321_923,
    athMcapUsd: 1_463_367,
    athAt: "2026-05-10T08:43:04Z",
    riskLabel: "late momentum; 7-8 May buyers are already deep in profit",
  },
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function money(value) {
  const number = Number(value || 0);
  if (!number) return "$0";
  if (number >= 1_000_000) return `$${(number / 1_000_000).toFixed(2)}m`;
  if (number >= 1_000) return `$${(number / 1_000).toFixed(0)}k`;
  return `$${number.toFixed(0)}`;
}

function moneyMaybe(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value)) || !Number(value)) return "-";
  return money(value);
}

function price(value) {
  const number = Number(value || 0);
  if (!number) return "-";
  if (number < 0.00001) return `$${number.toExponential(2)}`;
  if (number < 0.01) return `$${number.toFixed(7)}`;
  return `$${number.toFixed(4)}`;
}

function sol(value) {
  return `${Number(value || 0).toFixed(2)} SOL`;
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const number = Number(value);
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(1)}%`;
}

function compact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const number = Number(value || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 0 : 1)}m`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(number >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(number));
}

function short(value) {
  const text = String(value || "");
  if (text.length <= 14) return text || "-";
  return `${text.slice(0, 6)}...${text.slice(-5)}`;
}

function dateLabel(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function durationLabel(hours) {
  if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "-";
  const value = Number(hours);
  if (value < 1) return `${Math.max(1, Math.round(value * 60))}m`;
  if (value < 48) return `${value.toFixed(value < 10 ? 1 : 0)}h`;
  const days = value / 24;
  if (days < 60) return `${days.toFixed(days < 10 ? 1 : 0)}d`;
  const months = days / 30;
  if (months < 24) return `${months.toFixed(months < 10 ? 1 : 0)}mo`;
  return `${(months / 12).toFixed(1)}y`;
}

function reportFreshness(generatedAt) {
  if (!generatedAt) {
    return { label: "no report", tone: "bad", ageHours: null };
  }
  const generated = new Date(generatedAt);
  if (Number.isNaN(generated.getTime())) {
    return { label: "unknown age", tone: "warn", ageHours: null };
  }
  const ageHours = Math.max(0, (Date.now() - generated.getTime()) / 3_600_000);
  if (ageHours <= 1.25) {
    return { label: `fresh ${durationLabel(ageHours)}`, tone: "good", ageHours };
  }
  if (ageHours <= 2) {
    return { label: `delayed ${durationLabel(ageHours)}`, tone: "warn", ageHours };
  }
  return { label: `stale ${durationLabel(ageHours)}`, tone: "bad", ageHours };
}

function poolCreatedAt(pool = {}) {
  if (pool.pair_created_at_iso) {
    const date = new Date(pool.pair_created_at_iso);
    if (!Number.isNaN(date.getTime())) return date;
  }
  const raw = Number(pool.pair_created_at || 0);
  if (!raw) return null;
  const millis = raw > 10_000_000_000 ? raw : raw * 1000;
  const date = new Date(millis);
  return Number.isNaN(date.getTime()) ? null : date;
}

function rawTimestampDate(value) {
  const raw = Number(value || 0);
  if (!raw) return null;
  const millis = raw > 10_000_000_000 ? raw : raw * 1000;
  const date = new Date(millis);
  return Number.isNaN(date.getTime()) ? null : date;
}

const INFERRABLE_FILTERS = ["incubation", "young", "breakout", "reactivation"];

const FILTER_RULES = {
  incubation: {
    ageMin: 3,
    ageMax: 72,
    mcapMin: 50_000,
    mcapMax: 1_500_000,
    liquidityMin: 3_000,
    volumeMin: 1_000,
  },
  young: {
    ageMin: 72,
    ageMax: 720,
    mcapMin: 100_000,
    mcapMax: 5_000_000,
    liquidityMin: 10_000,
    volumeMin: 1_000,
  },
  breakout: {
    ageMin: 72,
    ageMax: 720,
    mcapMin: 5_000_000,
    mcapMax: 25_000_000,
    liquidityMin: 50_000,
    volumeMin: 100_000,
    volumeToMcapMin: 0.02,
    volumeToLiquidityMin: 0.35,
  },
  reactivation: {
    ageMin: 720,
    ageMax: null,
    mcapMin: 100_000,
    mcapMax: 5_000_000,
    liquidityMin: 10_000,
    volumeMin: 500,
    volumeMax: 150_000,
    athMaxRatio: 0.4,
  },
};

function finiteNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return number;
  }
  return null;
}

function alertSignalDate(alert = {}) {
  const date = new Date(alert.window_start || alert.created_at || alert.obs_mcap_at || alert.pool?.scan_mcap_at || 0);
  return Number.isNaN(date.getTime()) ? null : date;
}

function alertAgeHours(alert = {}) {
  const pool = alert.pool || {};
  const reported = Number(pool.age_hours);
  if (Number.isFinite(reported) && reported >= 0) return reported;
  const created = poolCreatedAt(pool) || rawTimestampDate(alert.token_intel?.profile?.created_time);
  const signal = alertSignalDate(alert);
  if (!created || !signal) return null;
  return Math.max(0, (signal.getTime() - created.getTime()) / 3_600_000);
}

function alertMarketSnapshot(alert = {}) {
  const pool = alert.pool || {};
  return {
    ageHours: alertAgeHours(alert),
    mcapUsd: finiteNumber(
      alert.obs_mcap_usd,
      alert.first_obs_mcap_usd,
      pool.first_obs_mcap_usd,
      pool.scan_mcap_usd,
      pool.mcap_usd,
      pool.latest_mcap_usd
    ),
    liquidityUsd: finiteNumber(
      alert.obs_liquidity_usd,
      pool.first_obs_liquidity_usd,
      pool.scan_liquidity_usd,
      pool.liquidity_usd,
      pool.latest_liquidity_usd
    ),
    volume1hUsd: finiteNumber(pool.volume_1h_usd),
    athMcapUsd: finiteNumber(pool.ath_mcap_usd),
  };
}

function matchesFilterRule(snapshot, rule) {
  const { ageHours, mcapUsd, liquidityUsd, volume1hUsd, athMcapUsd } = snapshot;
  if (ageHours === null || mcapUsd === null || liquidityUsd === null) return false;
  if (ageHours < rule.ageMin) return false;
  if (rule.ageMax !== null && ageHours >= rule.ageMax) return false;
  if (mcapUsd < rule.mcapMin || mcapUsd > rule.mcapMax) return false;
  if (liquidityUsd < rule.liquidityMin) return false;
  if (rule.volumeMin !== undefined) {
    if (volume1hUsd === null || volume1hUsd < rule.volumeMin) return false;
  }
  if (rule.volumeMax !== undefined && volume1hUsd !== null && volume1hUsd > rule.volumeMax) return false;
  if (rule.volumeToMcapMin !== undefined) {
    if (volume1hUsd === null || volume1hUsd / mcapUsd < rule.volumeToMcapMin) return false;
  }
  if (rule.volumeToLiquidityMin !== undefined) {
    if (volume1hUsd === null || volume1hUsd / liquidityUsd < rule.volumeToLiquidityMin) return false;
  }
  if (rule.athMaxRatio !== undefined) {
    if (athMcapUsd === null || athMcapUsd <= 0 || mcapUsd / athMcapUsd > rule.athMaxRatio) return false;
  }
  return true;
}

function inferAlertFilter(alert = {}) {
  const snapshot = alertMarketSnapshot(alert);
  return INFERRABLE_FILTERS.find((name) => matchesFilterRule(snapshot, FILTER_RULES[name])) || "legacy";
}

function baseAlertLane(alert = {}) {
  if (INFERRABLE_FILTERS.includes(alert.lane)) return alert.lane;
  return inferAlertFilter(alert);
}

function effectiveAlertLane(alert = {}) {
  return alert.filterLane || baseAlertLane(alert);
}

function tokenMarketSnapshot(token = {}) {
  return {
    ageHours: Number.isFinite(Number(token.tokenAgeHours)) ? Number(token.tokenAgeHours) : null,
    mcapUsd: finiteNumber(token.scanMcapUsd, token.currentMcap, token.firstObsMcapUsd, token.firstMcap),
    liquidityUsd: finiteNumber(token.liquidityUsd, token.latestPool?.liquidity_usd, token.latestPool?.latest_liquidity_usd),
    volume1hUsd: finiteNumber(token.latestPool?.volume_1h_usd),
    athMcapUsd: finiteNumber(token.athMcapUsd),
  };
}

function tokenFitsReactivationBucket(token) {
  const snapshot = tokenMarketSnapshot(token);
  const rule = FILTER_RULES.reactivation;
  if (snapshot.ageHours === null || snapshot.mcapUsd === null || snapshot.liquidityUsd === null) return false;
  if (snapshot.ageHours < rule.ageMin) return false;
  if (snapshot.mcapUsd < rule.mcapMin || snapshot.mcapUsd > rule.mcapMax) return false;
  if (snapshot.liquidityUsd < rule.liquidityMin) return false;
  if (snapshot.athMcapUsd === null || snapshot.athMcapUsd <= 0) return false;
  return snapshot.mcapUsd / snapshot.athMcapUsd <= rule.athMaxRatio;
}

function alertId(alert) {
  return `${alert?.pool?.pool_address || "pool"}:${alert?.window_start || "window"}:${alert?.score || 0}`;
}

function tokenKeyFromPool(pool = {}) {
  return pool.token_address || `${pool.symbol || pool.name || "unknown"}:${pool.pool_address || ""}`;
}

function socialHeat(alert) {
  const social = alert.social;
  if (!social) return "unchecked";
  if (social.enabled === false) return "disabled";
  return social.heat || "none";
}

function socialLabel(value, reason = "") {
  if (value === "unchecked") return "social not checked";
  if (value === "disabled") return reason ? `social disabled: ${reason.replaceAll("_", " ")}` : "social disabled";
  if (value === "none") return "social no mentions";
  return `social ${value}`;
}

function athSourceLabel(source) {
  if (source === "solana_tracker") return "Solana Tracker";
  if (source === "ohlcv_high") return "OHLCV high";
  return "ATH missing";
}

function athStatusLabel(status, error = "") {
  if (status === "ready") return "ready";
  if (status === "missing_api_key") return "missing API key";
  if (status === "error") return error ? `retry pending: ${error}` : "retry pending";
  return "pending";
}

function filterMeta(name = "legacy") {
  return FILTER_META[name] || {
    label: name,
    criteria: "custom scanner filter",
    thesis: "custom filter match from scanner config.",
  };
}

function normalizeFilterName(name) {
  return FILTER_META[name] ? name : "legacy";
}

function chip(text, tone = "") {
  return `<span class="chip ${tone}">${esc(text)}</span>`;
}

function tokenKey(value) {
  return typeof value === "string" ? value : value?.key || tokenKeyFromPool(value?.pool || value || {});
}

function isTokenHidden(value) {
  const key = tokenKey(value);
  return Boolean(key && state.hiddenTokenKeys.has(key));
}

function setTokenHidden(key, hidden) {
  if (!key) return;
  if (hidden) state.hiddenTokenKeys.add(key);
  else state.hiddenTokenKeys.delete(key);
  saveHiddenTokenKeys(state.hiddenTokenKeys);
}

function renderHiddenAction(token, compact = false) {
  const hidden = isTokenHidden(token);
  return `
    <button
      class="${compact ? "chip-action" : "secondary-action"} token-hide-toggle"
      type="button"
      data-token-key="${esc(token.key)}"
      data-hidden="${hidden ? "true" : "false"}"
      title="${hidden ? "Return token to normal lists" : "Hide this token from dashboard lists"}"
    >${hidden ? "Unhide" : "Hide"}</button>
  `;
}

function normalizeDex(value) {
  return String(value || "").trim().toLowerCase().replaceAll("_", "-");
}

function isPumpfunPool(pool = {}) {
  return PUMPFUN_DEX_ALLOWLIST.has(normalizeDex(pool.dex));
}

function narrativeTone(narrative) {
  return narrative?.source === "scanner_token_intel" ? "good" : "warn";
}

function classChips(classes = {}) {
  return Object.entries(classes)
    .map(([name, count]) => chip(`${name} ${count}`))
    .join("");
}

function tierMeta(tier) {
  return TIER_META[tier] || TIER_META.noise;
}

function tierChip(tier) {
  const meta = tierMeta(tier);
  return chip(meta.label, meta.tone);
}

function alertEvidence(alert = {}) {
  const classes = alert.classes || {};
  const hardWallets = finiteNumber(alert.hard_wallets)
    ?? Object.entries(classes)
      .filter(([name]) => HARD_WALLET_CLASSES.has(name))
      .reduce((sum, [, count]) => sum + Number(count || 0), 0);
  const supportWallets = finiteNumber(alert.support_wallets)
    ?? Object.entries(classes)
      .filter(([name]) => SUPPORT_WALLET_CLASSES.has(name))
      .reduce((sum, [, count]) => sum + Number(count || 0), 0);
  const hardSol = finiteNumber(alert.hard_sol)
    ?? (alert.events || [])
      .filter((event) => HARD_WALLET_CLASSES.has(event.wallet_class))
      .reduce((sum, event) => sum + Number(event.sol_amount || 0), 0);
  const supportSol = finiteNumber(alert.support_sol)
    ?? (alert.events || [])
      .filter((event) => SUPPORT_WALLET_CLASSES.has(event.wallet_class))
      .reduce((sum, event) => sum + Number(event.sol_amount || 0), 0);
  const commonLinks = Number(alert.common_funders?.length || 0)
    + Number(alert.common_recipients?.length || 0)
    + Number(alert.routed_buys || 0);
  return {
    hardWallets: Number(hardWallets || 0),
    supportWallets: Number(supportWallets || 0),
    hardSol: Number(hardSol || 0),
    supportSol: Number(supportSol || 0),
    commonLinks,
  };
}

function deriveAlertTier(alert = {}) {
  const lane = effectiveAlertLane(alert);
  const snapshot = alertMarketSnapshot(alert);
  const mcap = Number(snapshot.mcapUsd || alert.obs_mcap_usd || alert.pool?.mcap_usd || 0);
  const volumeToMcap = snapshot.volume1hUsd && mcap ? snapshot.volume1hUsd / mcap : null;
  const evidence = alertEvidence(alert);
  const hardSignal = evidence.hardWallets >= 2 || evidence.hardSol >= 15 || evidence.commonLinks > 0;
  const supportOnly = evidence.supportWallets > 0 && !evidence.hardWallets && !evidence.commonLinks;
  if (supportOnly) return "noise";
  if (!hardSignal) return Number(alert.score || 0) >= 60 ? "watch" : "noise";
  if (volumeToMcap !== null && volumeToMcap > 1.5 && evidence.commonLinks === 0) return "late_chase";
  if (lane === "incubation") {
    if (mcap > 300_000) return "late_chase";
    return mcap <= 150_000 ? "actionable" : "watch";
  }
  if (lane === "young") {
    if (mcap > 1_500_000) return "late_chase";
    return mcap <= 750_000 ? "actionable" : "watch";
  }
  if (lane === "reactivation") {
    const ratio = snapshot.athMcapUsd && mcap ? mcap / snapshot.athMcapUsd : null;
    if (ratio !== null && ratio > 0.4) return "late_chase";
    if (ratio !== null && ratio <= 0.25 && evidence.commonLinks && evidence.hardWallets >= 2) return "actionable";
    return "watch";
  }
  if (lane === "breakout") return evidence.commonLinks ? "watch" : "late_chase";
  return "watch";
}

function alertTier(alert = {}) {
  return TIER_META[alert.action_tier] ? alert.action_tier : deriveAlertTier(alert);
}

function bestTier(tiers = []) {
  return tiers.reduce((best, tier) => {
    if (!best || tierMeta(tier).rank > tierMeta(best).rank) return tier;
    return best;
  }, "noise");
}

function tierMatches(tier) {
  if (state.tier === "all") return true;
  if (state.tier === "focus") return tier === "actionable" || tier === "watch";
  return tier === state.tier;
}

function aggregateAlertLabels(alerts = [], field) {
  const labels = [];
  alerts.forEach((alert) => {
    (alert[field] || []).forEach((item) => {
      if (!labels.includes(item)) labels.push(item);
    });
  });
  return labels;
}

function pClass(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
  return Number(value) >= 0 ? "good" : "bad";
}

function alertTimeMs(alert = {}) {
  const date = new Date(alert.window_start || alert.created_at || 0);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function sourceAlerts() {
  const current = (state.report?.alerts || []).map((alert) => ({ ...alert, _scope_source: "current" }));
  if (state.scope === "current") return current;
  const history = (state.history || []).map((alert) => ({ ...alert, _scope_source: "history" }));
  const combined = [...history, ...current];
  if (state.scope !== "24h") return combined;
  const anchor = new Date(state.report?.generated_at || Date.now()).getTime();
  const cutoff = anchor - 24 * 3_600_000;
  return combined.filter((alert) => alertTimeMs(alert) >= cutoff);
}

function allAlerts() {
  const byId = new Map();
  sourceAlerts().forEach((alert) => {
    if (!isPumpfunPool(alert.pool || {})) return;
    byId.set(alertId(alert), alert);
  });
  return [...byId.values()].sort((a, b) => new Date(b.window_start || b.created_at) - new Date(a.window_start || a.created_at));
}

function normalizeMarketPool(pool = {}) {
  return {
    ...pool,
    mcap_usd: Number(pool.mcap_usd || pool.latest_mcap_usd || pool.scan_mcap_usd || 0),
    liquidity_usd: Number(pool.liquidity_usd || pool.latest_liquidity_usd || pool.scan_liquidity_usd || 0),
    price_usd: Number(pool.price_usd || pool.latest_price_usd || pool.scan_price_usd || 0),
  };
}

function currentPoolsByToken() {
  const map = new Map();
  const put = (pool, observedAt) => {
    if (!pool) return;
    if (!isPumpfunPool(pool)) return;
    const key = tokenKeyFromPool(pool);
    if (!key) return;
    const existing = map.get(key);
    const observedMs = new Date(observedAt || 0).getTime();
    const currentRank = Number.isNaN(observedMs) ? 0 : observedMs;
    const liquidityRank = Number(pool.volume_1h_usd || 0) + Number(pool.liquidity_usd || 0);
    if (
      !existing
      || currentRank > existing._observed_rank
      || (currentRank === existing._observed_rank && liquidityRank >= existing._liquidity_rank)
    ) {
      map.set(key, { ...pool, _observed_rank: currentRank, _liquidity_rank: liquidityRank });
    }
  };
  allAlerts().forEach((alert) => put(alert.pool, alert.window_start || alert.created_at));
  (state.report?.universe || []).forEach((pool) => put(pool, state.report?.generated_at));
  (state.report?.active_pools || []).forEach((item) => put(item.pool, state.report?.generated_at));
  (state.report?.summaries || []).forEach((item) => put(item.pool, state.report?.generated_at));
  Object.values(state.market || {}).forEach((pool) => put(normalizeMarketPool(pool), pool.latest_seen_at || pool.scan_mcap_at || pool.ath_latest_checked_at));
  return map;
}

function poolObservationsByToken() {
  const map = new Map();
  const add = (pool, observedAt, source) => {
    if (!pool) return;
    if (!isPumpfunPool(pool)) return;
    const key = tokenKeyFromPool(pool);
    const mcapUsd = Number(pool.mcap_usd || 0);
    if (!key || !mcapUsd) return;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push({
      at: observedAt || state.report?.generated_at || null,
      mcap_usd: mcapUsd,
      price_usd: Number(pool.price_usd || 0),
      source,
    });
  };
  allAlerts().forEach((alert) => add(alert.pool, alert.window_start || alert.created_at, "alert"));
  (state.report?.universe || []).forEach((pool) => add(pool, state.report?.generated_at, "universe"));
  (state.report?.active_pools || []).forEach((item) => add(item.pool, state.report?.generated_at, "active"));
  (state.report?.summaries || []).forEach((item) => add(item.pool, state.report?.generated_at, "summary"));
  Object.values(state.market || {}).forEach((pool) => add(normalizeMarketPool(pool), pool.latest_seen_at || pool.scan_mcap_at || pool.ath_latest_checked_at, "market_cache"));
  return map;
}

function backendTokenIntel(token) {
  return token.alerts.find((alert) => alert.token_intel?.narrative)?.token_intel || null;
}

function backendNarrative(token) {
  const intel = backendTokenIntel(token);
  const narrative = intel?.narrative;
  if (!narrative?.primary) return null;
  const overlay = narrative.overlay || {};
  return {
    primary: narrative.primary,
    secondary: narrative.secondary || [],
    tilt: narrative.tilt || "medium tilt",
    score: narrative.score || 0,
    evidence: narrative.evidence || [],
    news: {
      headline: overlay.headline || `Project overlay: ${narrative.primary}`,
      summary: overlay.summary || "Narrative was assigned by scanner token-intel enrichment from public sources.",
      sources: overlay.sources || [],
    },
    lore: narrative.lore || null,
    source: "scanner_token_intel",
  };
}

function choosePrimaryNarrative(token) {
  const backend = backendNarrative(token);
  if (backend) return backend;
  return {
    primary: "Token intel missing",
    secondary: [],
    tilt: "not classified",
    score: 0,
    evidence: ["backend token-intel enrichment is missing for this token"],
    news: {
      headline: "Token intel missing",
      summary: "This token has not been classified by the scanner token-intel pipeline yet. Run or refresh enrichment before using it in a thesis.",
      sources: [],
    },
    lore: null,
    source: "missing_token_intel",
  };
}

function median(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function eventKey(event = {}, alert = {}) {
  if (event.signature) return `sig:${event.signature}`;
  const parts = [
    event.time || alert.window_start || alert.created_at,
    event.signer,
    event.token_recipient,
    event.sol_amount,
    event.token_amount || event.token_recipient_amount,
  ].filter((part) => part !== null && part !== undefined && part !== "");
  return parts.length >= 3 ? `fallback:${parts.join(":")}` : null;
}

function eventOwner(event = {}) {
  return event.token_recipient || event.signer || "";
}

function uniqueAlertEvents(alerts = [], laneName = null) {
  const byKey = new Map();
  alerts.forEach((alert) => {
    const lane = alert.filterLane || effectiveAlertLane(alert);
    if (laneName && lane !== laneName) return;
    (alert.events || []).forEach((event) => {
      const key = eventKey(event, alert);
      if (!key || byKey.has(key)) return;
      byKey.set(key, { ...event, _alert: alert });
    });
  });
  return [...byKey.values()];
}

function rawEventCount(alerts = [], laneName = null) {
  return alerts.reduce((sum, alert) => {
    const lane = alert.filterLane || effectiveAlertLane(alert);
    if (laneName && lane !== laneName) return sum;
    return sum + (alert.events || []).length;
  }, 0);
}

function sumEventSol(events = []) {
  return events.reduce((sum, event) => sum + Number(event.sol_amount || 0), 0);
}

function eventClassCounts(events = []) {
  return events.reduce((counts, event) => {
    const name = event.wallet_class || "unknown";
    counts[name] = (counts[name] || 0) + 1;
    return counts;
  }, {});
}

function sortedClassChips(classes = {}) {
  const entries = Object.entries(classes).sort((a, b) => b[1] - a[1]);
  return entries.length ? entries.map(([name, count]) => chip(`${name} ${count}`)).join(" ") : "-";
}

function aggregateAlertClasses(alerts = [], laneName = null) {
  return alerts.reduce((counts, alert) => {
    const lane = alert.filterLane || effectiveAlertLane(alert);
    if (laneName && lane !== laneName) return counts;
    Object.entries(alert.classes || {}).forEach(([name, count]) => {
      counts[name] = (counts[name] || 0) + Number(count || 0);
    });
    return counts;
  }, {});
}

function sumAlertField(alerts = [], field, laneName = null) {
  return alerts.reduce((sum, alert) => {
    const lane = alert.filterLane || effectiveAlertLane(alert);
    if (laneName && lane !== laneName) return sum;
    return sum + Number(alert[field] || 0);
  }, 0);
}

function aggregateCommonEntries(alerts = [], field, keyNames = [], countName = "wallets") {
  const map = new Map();
  alerts.forEach((alert) => {
    (alert[field] || []).forEach((item) => {
      const key = keyNames.map((name) => item[name]).find(Boolean);
      if (!key) return;
      if (!map.has(key)) {
        map.set(key, { key, count: 0, alerts: 0 });
      }
      const row = map.get(key);
      row.count = Math.max(row.count, Number(item[countName] || item.wallets || item.txs || 0));
      row.alerts += 1;
    });
  });
  return [...map.values()].sort((a, b) => b.count - a.count || b.alerts - a.alerts).slice(0, 4);
}

function callerPostKeys(caller = {}) {
  const urls = [...(caller.post_urls || [])];
  if (caller.top_post?.url) urls.push(caller.top_post.url);
  const normalized = urls
    .filter(Boolean)
    .map((url) => String(url).replace(/\/photo\/\d+$/i, ""));
  if (normalized.length) return [...new Set(normalized)];
  return [`${caller.author || ""}:${caller.top_post?.date_posted || ""}:${caller.top_post?.text || ""}`];
}

function buildTokenSignals() {
  const currentPools = currentPoolsByToken();
  const observationsByToken = poolObservationsByToken();
  const groups = new Map();
  allAlerts().forEach((alert) => {
    const pool = alert.pool || {};
    const key = tokenKeyFromPool(pool);
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        symbol: pool.symbol || pool.name || "Unknown",
        name: pool.name || "",
        token_address: pool.token_address || "",
        pool_address: pool.pool_address || "",
        url: pool.url || "",
        alerts: [],
      });
    }
    groups.get(key).alerts.push(alert);
  });

  const tokens = [...groups.values()].map((token) => {
    token.alerts.sort((a, b) => new Date(a.window_start || a.created_at) - new Date(b.window_start || b.created_at));
    token.alerts.forEach((alert) => {
      alert.baseFilterLane = baseAlertLane(alert);
      alert.filterLane = alert.baseFilterLane;
      alert.filterInferred = !INFERRABLE_FILTERS.includes(alert.lane);
    });
    const first = token.alerts[0];
    const last = token.alerts[token.alerts.length - 1];
    const latestPool = currentPools.get(token.key) || last.pool || first.pool || {};
    token.tokenIntel = [...token.alerts].reverse().find((alert) => alert.token_intel)?.token_intel || null;
    const observations = observationsByToken.get(token.key) || [];
    const latestObservation = observations.reduce((best, item) => {
      const itemTime = new Date(item.at || 0).getTime();
      const bestTime = new Date(best?.at || 0).getTime();
      if (!best || itemTime > bestTime) return item;
      return best;
    }, null);
    const firstPool = first.pool || {};
    const firstPriceUsd = Number(firstPool.price_usd || 0);
    const currentPriceUsd = Number(latestPool.price_usd || last.pool?.price_usd || firstPriceUsd || 0);
    const profitPct = firstPriceUsd && currentPriceUsd ? ((currentPriceUsd / firstPriceUsd) - 1) * 100 : null;
    const uniqueEvents = uniqueAlertEvents(token.alerts);
    const rawEvents = rawEventCount(token.alerts);
    const nativeRatios = [];
    uniqueEvents.forEach((event) => {
      const poolPrice = Number(event._alert?.pool?.price_usd || 0);
      const native = Number(event.price_native || 0);
      if (poolPrice && native) nativeRatios.push(poolPrice / native);
    });
    const solUsd = median(nativeRatios);
    const currentNative = solUsd && currentPriceUsd ? currentPriceUsd / solUsd : null;
    const walletMap = new Map();
    uniqueEvents.forEach((event) => {
      const alert = event._alert || {};
      const owner = eventOwner(event);
      if (!owner) return;
      if (!walletMap.has(owner)) {
        walletMap.set(owner, {
          owner,
          signer_examples: new Set(),
          classes: {},
          buys: 0,
          sol_in: 0,
          tokens: 0,
          routed: 0,
          first_time: event.time || alert.window_start,
        });
      }
      const row = walletMap.get(owner);
      if (event.signer) row.signer_examples.add(event.signer);
      row.classes[event.wallet_class || "unknown"] = (row.classes[event.wallet_class || "unknown"] || 0) + 1;
      row.buys += 1;
      row.sol_in += Number(event.sol_amount || 0);
      row.tokens += Number(event.token_amount || event.token_recipient_amount || 0);
      row.routed += event.routed ? 1 : 0;
      if (new Date(event.time || alert.window_start) < new Date(row.first_time)) row.first_time = event.time || alert.window_start;
    });
    const wallets = [...walletMap.values()].map((row) => {
      row.avg_entry_native = row.tokens ? row.sol_in / row.tokens : null;
      row.pnl_pct = currentNative && row.avg_entry_native ? ((currentNative / row.avg_entry_native) - 1) * 100 : null;
      row.current_value_sol = currentNative && row.tokens ? currentNative * row.tokens : null;
      row.pnl_sol = row.current_value_sol !== null ? row.current_value_sol - row.sol_in : null;
      row.class_label = Object.entries(row.classes).sort((a, b) => b[1] - a[1]).map(([name, count]) => `${name} ${count}`).join(", ");
      row.signer_count = row.signer_examples.size;
      return row;
    }).sort((a, b) => Number(b.sol_in || 0) - Number(a.sol_in || 0));
    const walletPnls = wallets.map((row) => row.pnl_pct).filter((value) => Number.isFinite(value));
    token.latestPool = latestPool;
    token.firstSignalAt = first.window_start || first.created_at;
    token.lastSignalAt = last.window_start || last.created_at;
    token.firstPriceUsd = firstPriceUsd;
    token.currentPriceUsd = currentPriceUsd;
    token.profitPct = profitPct;
    token.firstMcap = Number(firstPool.mcap_usd || 0);
    token.currentMcap = Number(latestPool.mcap_usd || 0);
    token.firstObsMcapUsd = Number(
      first.first_obs_mcap_usd
      || first.obs_mcap_usd
      || firstPool.first_obs_mcap_usd
      || latestPool.first_obs_mcap_usd
      || firstPool.mcap_usd
      || 0
    );
    token.firstObsMcapAt = first.first_obs_mcap_at
      || first.obs_mcap_at
      || firstPool.first_obs_mcap_at
      || latestPool.first_obs_mcap_at
      || first.window_start
      || first.created_at
      || null;
    const createdAt = poolCreatedAt(latestPool) || poolCreatedAt(firstPool);
    const reportedAge = latestPool.age_hours ?? firstPool.age_hours;
    token.tokenAgeHours = reportedAge !== null && reportedAge !== undefined
      ? Number(reportedAge)
      : createdAt
        ? Math.max(0, (Date.now() - createdAt.getTime()) / 3_600_000)
        : null;
    token.tokenCreatedAt = createdAt ? createdAt.toISOString() : null;
    const trustedAth = ["solana_tracker", "ohlcv_high"].includes(latestPool.ath_source) && Number(latestPool.ath_mcap_usd || 0) > 0;
    token.athMcapUsd = trustedAth ? Number(latestPool.ath_mcap_usd || 0) : null;
    token.athMcapAt = trustedAth ? latestPool.ath_mcap_at || null : null;
    token.athSource = trustedAth ? latestPool.ath_source : "missing";
    token.athStatus = trustedAth ? "ready" : latestPool.ath_status || (latestPool.ath_error ? "error" : "pending");
    token.athError = latestPool.ath_error || "";
    token.athLabel = "Solana Tracker ATH";
    token.scanMcapUsd = Number(latestPool.scan_mcap_usd || latestPool.latest_mcap_usd || latestObservation?.mcap_usd || token.currentMcap || token.firstMcap || 0);
    token.scanMcapAt = latestPool.scan_mcap_at || latestPool.latest_seen_at || latestObservation?.at || token.lastSignalAt || null;
    token.liquidityUsd = Number(latestPool.liquidity_usd || 0);
    token.maxScore = Math.max(...token.alerts.map((alert) => Number(alert.score || 0)));
    token.uniqueEvents = uniqueEvents;
    token.rawEventCount = rawEvents;
    token.uniqueEventCount = uniqueEvents.length;
    token.duplicateEventCount = Math.max(0, rawEvents - uniqueEvents.length);
    token.hardEvents = uniqueEvents.filter((event) => HARD_WALLET_CLASSES.has(event.wallet_class));
    token.supportEvents = uniqueEvents.filter((event) => SUPPORT_WALLET_CLASSES.has(event.wallet_class));
    token.totalSuspiciousSol = sumAlertField(token.alerts, "suspicious_sol") || sumEventSol(uniqueEvents);
    token.hardFlowSol = sumAlertField(token.alerts, "hard_sol") || sumEventSol(token.hardEvents);
    token.supportFlowSol = sumAlertField(token.alerts, "support_sol") || sumEventSol(token.supportEvents);
    token.hardSignalCount = sumAlertField(token.alerts, "hard_wallets") || token.hardEvents.length;
    token.supportSignalCount = sumAlertField(token.alerts, "support_wallets") || token.supportEvents.length;
    token.walletClassCounts = Object.keys(aggregateAlertClasses(token.alerts)).length ? aggregateAlertClasses(token.alerts) : eventClassCounts(uniqueEvents);
    token.routedBuyCount = uniqueEvents.filter((event) => event.routed).length;
    token.commonFunders = aggregateCommonEntries(token.alerts, "common_funders", ["source", "funder", "wallet"], "wallets");
    token.commonRecipients = aggregateCommonEntries(token.alerts, "common_recipients", ["recipient", "wallet"], "txs");
    token.alertCount = token.alerts.length;
    token.wallets = wallets;
    token.uniqueWallets = wallets.length;
    token.bestWalletPnl = walletPnls.length ? Math.max(...walletPnls) : null;
    token.medianWalletPnl = median(walletPnls);
    const fitsReactivationNow = tokenFitsReactivationBucket(token);
    token.alerts.forEach((alert) => {
      const baseLane = alert.baseFilterLane || baseAlertLane(alert);
      if (baseLane === "reactivation" || (baseLane === "legacy" && fitsReactivationNow)) {
        alert.filterLane = fitsReactivationNow ? "reactivation" : "legacy";
      } else {
        alert.filterLane = baseLane;
      }
    });
    token.observedFilters = [...new Set(token.alerts.map((alert) => normalizeFilterName(alert.filterLane || "legacy")))];
    token.caughtFilter = normalizeFilterName(
      first.first_obs_lane
      || first.obs_lane
      || firstPool.first_obs_lane
      || latestPool.first_obs_lane
      || first.filterLane
      || token.observedFilters[0]
      || "legacy"
    );
    token.currentFilter = normalizeFilterName(last.filterLane || token.observedFilters[token.observedFilters.length - 1] || token.caughtFilter);
    token.filterCategories = [token.caughtFilter];
    token.primaryFilter = token.caughtFilter;
    token.lanes = token.filterCategories;
    token.hasObservedFilterDrift = token.observedFilters.some((name) => name !== token.caughtFilter);
    token.hasFilterDrift = token.currentFilter !== token.caughtFilter;
    token.hasInferredFilters = token.alerts.some((alert) => alert.filterInferred || alert.filterLane !== alert.baseFilterLane);
    token.alertTiers = token.alerts.map(alertTier);
    token.tierCounts = token.alertTiers.reduce((counts, tier) => {
      counts[tier] = (counts[tier] || 0) + 1;
      return counts;
    }, {});
    token.actionTier = bestTier(token.alertTiers);
    token.qualityReasons = aggregateAlertLabels(token.alerts, "quality_reasons");
    token.qualityPenalties = aggregateAlertLabels(token.alerts, "quality_penalties");
    const heats = token.alerts.map(socialHeat);
    const disabledSocial = token.alerts.map((alert) => alert.social).find((social) => social?.enabled === false);
    token.socialHeat = heats.includes("hot")
      ? "hot"
      : heats.includes("warming")
        ? "warming"
        : heats.includes("quiet")
          ? "quiet"
          : heats.includes("disabled")
            ? "disabled"
            : heats.includes("none")
              ? "none"
              : "unchecked";
    token.socialReason = disabledSocial?.reason || "";
    token.narrative = choosePrimaryNarrative(token);
    token.narratives = [token.narrative.primary];
    token.hidden = isTokenHidden(token);
    return token;
  });

  return tokens.sort((a, b) => {
    const aScore = tierMeta(a.actionTier).rank * 1000 + Number(a.maxScore || 0) + Math.max(0, Number(a.profitPct || 0)) / 10 + Number(a.totalSuspiciousSol || 0) / 5;
    const bScore = tierMeta(b.actionTier).rank * 1000 + Number(b.maxScore || 0) + Math.max(0, Number(b.profitPct || 0)) / 10 + Number(b.totalSuspiciousSol || 0) / 5;
    return bScore - aScore;
  });
}

function tokenMatchesBaseFilters(token) {
  const query = state.query.toLowerCase();
  if (!state.showHidden && token.hidden) return false;
  const text = [
    token.symbol,
    token.name,
    token.token_address,
    token.pool_address,
    token.narratives.join(" "),
    token.wallets.map((wallet) => wallet.owner).join(" "),
  ].join(" ").toLowerCase();
  if (query && !text.includes(query)) return false;
  if (state.heat !== "all" && token.socialHeat !== state.heat) return false;
  if (state.lane !== "all" && !token.filterCategories.includes(state.lane)) return false;
  if (Number(token.maxScore || 0) < state.minScore) return false;
  return true;
}

function filteredTokens(tokens) {
  return tokens.filter((token) => tokenMatchesBaseFilters(token) && tierMatches(token.actionTier));
}

function metric(label, value) {
  return `
    <div class="metric">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-value">${esc(value)}</div>
    </div>
  `;
}

function emptyMessage(text) {
  const hiddenHint = !state.showHidden && state.hiddenTokenKeys.size
    ? ` <span class="muted-inline">Enable Show hidden to review ${esc(state.hiddenTokenKeys.size)} locally hidden token${state.hiddenTokenKeys.size === 1 ? "" : "s"}.</span>`
    : "";
  return `<div class="empty">${esc(text)}${hiddenHint}</div>`;
}

async function fetchJson(path, optional = false) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    if (optional) return null;
    throw new Error(`${path} ${response.status}`);
  }
  return response.json();
}

async function fetchText(path, optional = false) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    if (optional) return "";
    throw new Error(`${path} ${response.status}`);
  }
  return response.text();
}

function parseJsonl(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean)
    .reverse();
}

async function loadStaticData() {
  const report = await fetchJson("data/latest_report.json");
  const [alertsText, scannerState] = await Promise.all([
    fetchText("data/alerts.jsonl", true),
    fetchJson("data/state.json", true),
  ]);
  return {
    report,
    history: parseJsonl(alertsText),
    market: scannerState?.market || {},
    scan_status: {
      running: false,
      source: "github_actions",
      static_mode: true,
      finished_at: report.generated_at,
      returncode: 0,
    },
  };
}

async function loadData() {
  let payload;
  state.staticMode = false;
  const staticHost = window.location.hostname.endsWith("github.io") || window.location.protocol === "file:";
  if (staticHost) {
    payload = await loadStaticData();
    state.staticMode = true;
  } else {
  try {
    payload = await fetchJson("/api/report");
  } catch {
    payload = await loadStaticData();
    state.staticMode = true;
  }
  }
  state.report = payload.report || {};
  state.history = payload.history || [];
  state.market = payload.market || {};
  state.scanStatus = payload.scan_status || {};
  const tokens = buildTokenSignals();
  const visibleTokens = filteredTokens(tokens);
  if (!state.selectedTokenKey && visibleTokens.length) state.selectedTokenKey = visibleTokens[0].key;
  if (!state.showHidden && isTokenHidden(state.selectedTokenKey)) {
    state.selectedTokenKey = visibleTokens[0]?.key || null;
  }
  render();
}

function renderStatus() {
  const report = state.report || {};
  const status = state.scanStatus || {};
  const running = Boolean(status.running);
  const freshness = reportFreshness(report.generated_at);
  if (els.showHiddenInput) els.showHiddenInput.checked = state.showHidden;
  if (els.scopeFilter) els.scopeFilter.value = state.scope;
  if (els.tierFilter) els.tierFilter.value = state.tier;
  const reportLanes = (report.lanes_scanned || []).filter((name) => FILTER_ORDER.includes(name) && name !== "legacy");
  const laneText = status.lane || status.mode || reportLanes.join(", ") || report.mode || "-";
  els.subtitle.textContent = report.generated_at
    ? `Latest scan ${dateLabel(report.generated_at)} - ${report.profile || report.lane || report.mode || "unknown"}`
    : "No scan report yet";
  els.runScan.disabled = running || state.staticMode;
  els.runScan.title = state.staticMode ? "Scanner runs by GitHub Actions schedule" : "";
  els.statusRow.innerHTML = [
    `<span class="status-pill"><span class="dot ${running ? "warn" : ""}"></span>${running ? "scan running" : "idle"}</span>`,
    `<span class="status-pill freshness-${freshness.tone}"><span class="dot ${freshness.tone === "good" ? "" : freshness.tone}"></span>${esc(freshness.label)}</span>`,
    `<span class="status-pill">lane ${esc(laneText)}</span>`,
    state.hiddenTokenKeys.size ? `<span class="status-pill">${esc(state.hiddenTokenKeys.size)} hidden locally</span>` : "",
    state.staticMode ? `<span class="status-pill">auto via Cloudflare + GitHub Actions</span>` : "",
    status.next_scan_at ? `<span class="status-pill">next auto ${esc(dateLabel(status.next_scan_at))}</span>` : "",
    status.returncode === 0 ? `<span class="status-pill"><span class="dot"></span>last scan ok</span>` : "",
  ].filter(Boolean).join("");
}

function renderMetrics(tokens) {
  const report = state.report || {};
  const stats = report.stats || {};
  const baseTokens = buildTokenSignals().filter(tokenMatchesBaseFilters);
  const tierCount = (tier) => baseTokens.filter((token) => token.actionTier === tier).length;
  const lateNoise = tierCount("late_chase") + tierCount("noise");
  els.metrics.innerHTML = [
    metric("Actionable", tierCount("actionable")),
    metric("Watch", tierCount("watch")),
    metric("Late/noise", lateNoise),
    metric("Scanned pools", stats.scanned_pools ?? 0),
    metric("Report age", reportFreshness(report.generated_at).label),
  ].join("");
}

function renderTokenRow(token) {
  const selected = token.key === state.selectedTokenKey ? " is-selected" : "";
  const hidden = token.hidden ? " is-hidden" : "";
  const athText = token.athMcapUsd
    ? `${token.athLabel} ${money(token.athMcapUsd)}`
    : `${token.athLabel} ${athStatusLabel(token.athStatus)}`;
  const gmgnUrl = gmgnTokenUrl(token);
  const phase = marketPhase(token);
  return `
    <article class="token-row${selected}${hidden}" data-token-key="${esc(token.key)}">
      <div class="token-main">
        <div class="symbol-line">
          <span class="symbol">${esc(token.symbol)}</span>
          <span class="muted">${esc(token.name)}</span>
          ${token.hidden ? chip("hidden", "warn") : ""}
        </div>
        <div class="meta">
          <span>first ${esc(dateLabel(token.firstSignalAt))}</span>
          <span>age ${esc(durationLabel(token.tokenAgeHours))}</span>
          <span>caught ${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)} mcap</span>
          <span>${esc(athText)}</span>
          <span>${money(token.liquidityUsd)} liq</span>
          <span>${token.alertCount} signals</span>
          <span>${token.uniqueWallets} wallets</span>
        </div>
        <div class="chips">
          ${tierChip(token.actionTier)}
          ${chip(`${token.narrative.primary} - ${token.narrative.tilt}`, narrativeTone(token.narrative))}
          ${token.narrative.secondary.slice(0, 1).map((name) => chip(`${name} flavor`)).join("")}
          ${chip(`caught ${filterMeta(token.caughtFilter).label}`)}
          ${token.hasFilterDrift ? chip(`now ${filterMeta(token.currentFilter).label}`, "warn") : ""}
          ${phase && phase.label !== "Mid-range" ? chip(phase.label, phase.tone) : ""}
          ${chip(socialLabel(token.socialHeat, token.socialReason), token.socialHeat === "disabled" ? "warn" : "")}
          ${gmgnUrl ? `<a class="chip token-terminal-link" href="${esc(gmgnUrl)}" target="_blank" rel="noreferrer">GMGN</a>` : ""}
          ${renderHiddenAction(token, true)}
        </div>
      </div>
      <div class="token-numbers">
        <span class="${pClass(token.profitPct)}">${pct(token.profitPct)}</span>
        <small>since caught</small>
      </div>
      <div class="token-numbers">
        <span class="${pClass(token.medianWalletPnl)}">${pct(token.medianWalletPnl)}</span>
        <small>median wallet pnl</small>
      </div>
      <div class="token-numbers">
        <span>${sol(token.totalSuspiciousSol)}</span>
        <small>unique noticed flow</small>
      </div>
    </article>
  `;
}

function selectedToken(tokens) {
  return tokens.find((token) => token.key === state.selectedTokenKey) || tokens[0] || null;
}

function selectedNarrativeGroup(groups) {
  return groups.find((group) => group.name === state.selectedNarrative) || groups[0] || null;
}

function bindTokenHideActions() {
  document.querySelectorAll(".token-hide-toggle").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const key = button.dataset.tokenKey;
      setTokenHidden(key, button.dataset.hidden !== "true");
      render();
    });
  });
}

function renderWalletRows(token) {
  if (!token.wallets.length) return `<div class="empty compact">No wallet events.</div>`;
  return `
    <div class="table-wrap compact-table">
      <table>
        <thead>
          <tr>
            <th>Wallet</th>
            <th>Class</th>
            <th>Buys</th>
            <th>Entry</th>
            <th>PnL</th>
            <th>Profit</th>
          </tr>
        </thead>
        <tbody>
          ${token.wallets.slice(0, 16).map((wallet) => `
            <tr>
              <td><code>${esc(short(wallet.owner))}</code></td>
              <td>${esc(wallet.class_label || "-")}${wallet.routed ? ` ${chip(`routed ${wallet.routed}`, "warn")}` : ""}</td>
              <td>${esc(wallet.buys)}</td>
              <td>${sol(wallet.sol_in)}</td>
              <td class="${pClass(wallet.pnl_pct)}">${pct(wallet.pnl_pct)}</td>
              <td class="${pClass(wallet.pnl_sol)}">${wallet.pnl_sol === null ? "-" : sol(wallet.pnl_sol)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function tokenCallerGraph(token) {
  const byAuthor = new Map();
  token.alerts.forEach((alert) => {
    (alert.social?.caller_graph || []).forEach((caller) => {
      const key = String(caller.author || "").toLowerCase();
      if (!key) return;
      const postKeys = callerPostKeys(caller);
      if (!byAuthor.has(key)) {
        byAuthor.set(key, {
          author: caller.author,
          name: caller.name,
          followers: caller.followers,
          following: caller.following,
          profile_posts_count: caller.profile_posts_count,
          is_verified: Boolean(caller.is_verified),
          verification_type: caller.verification_type,
          watched: Boolean(caller.watched),
          posts: 0,
          views: 0,
          likes: 0,
          reposts: 0,
          replies: 0,
          quotes: 0,
          bookmarks: 0,
          engagements: 0,
          influence_score: 0,
          top_post: null,
          post_urls: [],
          _seenPostKeys: new Set(),
        });
      }
      const row = byAuthor.get(key);
      const isRepeatedSnapshot = postKeys.some((postKey) => row._seenPostKeys.has(postKey));
      postKeys.forEach((postKey) => row._seenPostKeys.add(postKey));
      row.post_urls = [...row._seenPostKeys];
      if (!isRepeatedSnapshot) {
        row.views += Number(caller.views || 0);
        row.likes += Number(caller.likes || 0);
        row.reposts += Number(caller.reposts || 0);
        row.replies += Number(caller.replies || 0);
        row.quotes += Number(caller.quotes || 0);
        row.bookmarks += Number(caller.bookmarks || 0);
        row.engagements += Number(caller.engagements || 0);
      }
      row.followers = Math.max(Number(row.followers || 0), Number(caller.followers || 0)) || row.followers;
      row.following = Math.max(Number(row.following || 0), Number(caller.following || 0)) || row.following;
      row.profile_posts_count = Math.max(Number(row.profile_posts_count || 0), Number(caller.profile_posts_count || 0)) || row.profile_posts_count;
      row.is_verified = row.is_verified || caller.is_verified;
      row.watched = row.watched || caller.watched;
      row.influence_score = Math.max(Number(row.influence_score || 0), Number(caller.influence_score || 0));
      const currentTopScore = Number(row.top_post?.views || 0) + Number(row.top_post?.likes || 0) * 25;
      const nextTopScore = Number(caller.top_post?.views || 0) + Number(caller.top_post?.likes || 0) * 25;
      if (nextTopScore > currentTopScore) row.top_post = caller.top_post;
    });
  });
  return [...byAuthor.values()].map((row) => {
    row.posts = row._seenPostKeys.size;
    row.engagement_rate_views_pct = row.views ? row.engagements / row.views * 100 : row.engagement_rate_views_pct;
    row.engagement_rate_followers_pct = row.followers ? row.engagements / row.followers * 100 : row.engagement_rate_followers_pct;
    delete row._seenPostKeys;
    return row;
  }).sort((a, b) => Number(b.influence_score || 0) - Number(a.influence_score || 0));
}

function renderCallerRows(token) {
  const callers = tokenCallerGraph(token);
  if (!callers.length) {
    return `<div class="empty compact">No enriched caller metrics yet. Bright Data Discover may have found posts, but X post metrics were not scraped for them.</div>`;
  }
  return `
    <div class="table-wrap compact-table">
      <table class="caller-table">
        <thead>
          <tr>
            <th>Caller</th>
            <th>Followers</th>
            <th>Posts</th>
            <th>Views</th>
            <th>Likes</th>
            <th>Reposts</th>
            <th>Quotes</th>
            <th>Comments</th>
            <th>Eng</th>
            <th>Top post</th>
          </tr>
        </thead>
        <tbody>
          ${callers.slice(0, 12).map((caller) => `
            <tr>
              <td>
                <a href="https://x.com/${esc(caller.author)}" target="_blank" rel="noreferrer">@${esc(caller.author)}</a>
                ${caller.is_verified ? chip("verified", "good") : ""}
                ${caller.watched ? chip("watched", "warn") : ""}
              </td>
              <td>${compact(caller.followers)}</td>
              <td>${compact(caller.posts)}</td>
              <td>${compact(caller.views)}</td>
              <td>${compact(caller.likes)}</td>
              <td>${compact(caller.reposts)}</td>
              <td>${compact(caller.quotes)}</td>
              <td>${compact(caller.replies)}</td>
              <td>${caller.engagement_rate_views_pct === null || caller.engagement_rate_views_pct === undefined ? "-" : `${caller.engagement_rate_views_pct.toFixed(1)}%`}</td>
              <td>${caller.top_post?.url ? `<a href="${esc(caller.top_post.url)}" target="_blank" rel="noreferrer">${compact(caller.top_post.views)} views</a>` : "-"}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderTokenSourceLinks(token) {
  const links = [];
  const push = (link) => {
    if (!link?.url || links.some((item) => item.url === link.url)) return;
    links.push(link);
  };
  (token.tokenIntel?.profile?.links || []).forEach(push);
  (token.tokenIntel?.dex?.links || []).forEach(push);
  (token.tokenIntel?.narrative?.overlay?.sources || []).forEach(push);
  if (!links.length) return "-";
  return links.slice(0, 8).map((link) => `<a href="${esc(link.url)}" target="_blank" rel="noreferrer">${esc(link.label || link.title || link.type || "Source")}</a>`).join(" ");
}

function cleanLoreText(text = "") {
  return String(text)
    .replaceAll("Read more", "")
    .replace(/\s+/g, " ")
    .trim();
}

function loreEvidenceBasis(evidence = []) {
  const kinds = new Set(evidence.map((item) => String(item.kind || "").toLowerCase()));
  const basis = [];
  if ([...kinds].some((kind) => kind.includes("official"))) basis.push("official profile");
  if ([...kinds].some((kind) => kind.includes("public"))) basis.push("project/public context");
  if ([...kinds].some((kind) => kind.includes("social"))) basis.push("social posts");
  return basis;
}

function humanLoreSummary(token, lore, evidence = []) {
  const summary = cleanLoreText(lore.summary || "");
  if (summary && !/classified as|source evidence ties|based on scanner token-intel/i.test(summary)) {
    return summary;
  }
  const primary = token.narrative.primary;
  const secondary = token.narrative.secondary?.[0];
  const allClaims = cleanLoreText(evidence.map((item) => item.claim).join(" "));
  if (primary === "Health/Bio" && /hanta|hantavirus/i.test(allClaims)) {
    return `${token.symbol} is a Health/Bio news-cycle meme: project/public sources frame it as Hanta-Kun, an anime mascot created around the hantavirus theme and reused for the current Hantavirus narrative. ${secondary ? `${secondary} is packaging, not the lead thesis.` : ""}`;
  }
  if (primary === "Gaming/Creator Infra") {
    return `${token.symbol} is a Gaming/Creator Infra thesis backed by project/profile sources; meme wording is secondary unless social evidence becomes stronger.`;
  }
  if (primary === "Animals") {
    return `${token.symbol} is an animal mascot/community meme; no stronger project, news, or social-lore catalyst is visible in the current evidence.`;
  }
  if (primary === "Token intel missing") {
    return "Token intel is missing, so the lore thesis is not ready for investment use.";
  }
  return summary || `${token.symbol} is a ${primary} thesis backed by scanner token-intel evidence.`;
}

function renderLoreProof(token) {
  const lore = token.narrative.lore;
  if (!lore) return "-";
  const evidence = lore.evidence || [];
  const headline = cleanLoreText(lore.headline);
  const summary = humanLoreSummary(token, lore, evidence);
  const normalizedHeadline = headline.replace(/\s+lore$/i, "").toLowerCase();
  const summaryLower = summary.toLowerCase();
  const shouldShowHeadline = headline && !summaryLower.startsWith(token.symbol.toLowerCase()) && !summaryLower.includes(normalizedHeadline);
  const thesis = shouldShowHeadline ? [headline, summary].filter(Boolean).join(": ") : summary;
  return `
    ${chip(lore.confidence || "unknown", lore.confidence === "high" ? "good" : "")}
    ${esc(thesis || `${token.symbol} narrative is not normalized yet.`)}
  `;
}

function renderNarrativeLine(token) {
  const flavors = token.narrative.secondary.slice(0, 2).map((name) => chip(`${name} flavor`)).join(" ");
  return `${chip(`${token.narrative.primary} - ${token.narrative.tilt}`, narrativeTone(token.narrative))}${flavors ? ` ${flavors}` : ""}`;
}

function renderEvidenceLine(token) {
  const basis = loreEvidenceBasis(token.narrative.lore?.evidence || []);
  const basisChips = basis.length
    ? basis.map((item) => chip(item)).join(" ")
    : chip(token.narrative.source === "scanner_token_intel" ? "scanner token-intel" : "token intel missing", "warn");
  return `${basisChips} ${chip(socialLabel(token.socialHeat, token.socialReason), token.socialHeat === "disabled" ? "warn" : "")}`;
}

function renderFilterLine(token) {
  const caught = token.caughtFilter || token.primaryFilter || "legacy";
  const current = token.currentFilter || caught;
  const chips = [
    chip(`caught ${filterMeta(caught).label}`),
    current !== caught ? chip(`now ${filterMeta(current).label}`, "warn") : "",
  ].filter(Boolean).join(" ");
  const inferred = token.hasInferredFilters ? ` ${chip("inferred from snapshot", "warn")}` : "";
  const primary = filterMeta(caught);
  return `${chips}${inferred} <span class="muted-inline">${esc(primary.criteria)}</span>`;
}

function renderSignalQuality(token) {
  const duplicateChip = token.duplicateEventCount
    ? ` ${chip(`${token.duplicateEventCount} duplicate rows collapsed`, "warn")}`
    : "";
  return [
    `max score ${esc(token.maxScore)}`,
    `${esc(token.hardSignalCount)} hard wallets / ${sol(token.hardFlowSol)}`,
    `${esc(token.supportSignalCount)} support wallets / ${sol(token.supportFlowSol)}`,
    `${esc(token.uniqueWallets)} wallets`,
  ].join(" / ") + duplicateChip;
}

function renderSignalTier(token) {
  const reasons = token.qualityReasons.length
    ? token.qualityReasons.slice(0, 4).map((item) => chip(item)).join(" ")
    : `<span class="muted-inline">${esc(tierMeta(token.actionTier).summary)}</span>`;
  const penalties = token.qualityPenalties.length
    ? ` ${token.qualityPenalties.slice(0, 4).map((item) => chip(item, item.includes("late") || item.includes("blowoff") || item.includes("only") ? "bad" : "warn")).join(" ")}`
    : "";
  return `${tierChip(token.actionTier)} ${reasons}${penalties}`;
}

function renderWalletCluster(token) {
  const parts = [];
  const classSummary = sortedClassChips(token.walletClassCounts);
  if (classSummary !== "-") parts.push(classSummary);
  token.commonFunders.forEach((item) => {
    parts.push(chip(`common funder ${short(item.key)} -> ${item.count} wallets`, "warn"));
  });
  token.commonRecipients.forEach((item) => {
    parts.push(chip(`common recipient ${short(item.key)} <- ${item.count} txs`, "warn"));
  });
  if (token.routedBuyCount) parts.push(chip(`routed buys ${token.routedBuyCount}`, "warn"));
  return parts.length ? parts.join(" ") : "-";
}

function renderWalletSummary(token) {
  return `${esc(token.uniqueWallets)} unique wallets / median ${pct(token.medianWalletPnl)} / best ${pct(token.bestWalletPnl)}`;
}

function tokenAthRatio(token) {
  const currentMcap = Number(token.scanMcapUsd || token.currentMcap || 0);
  const athMcap = Number(token.athMcapUsd || 0);
  return currentMcap && athMcap ? currentMcap / athMcap : null;
}

function marketPhase(token) {
  const ratio = tokenAthRatio(token);
  if (ratio === null) return null;
  if (ratio >= 0.85) return { label: "Near ATH", tone: "bad", detail: `${(ratio * 100).toFixed(0)}% of ATH` };
  if (ratio > 0.4) return { label: "Upper range", tone: "warn", detail: `${(ratio * 100).toFixed(0)}% of ATH` };
  if (ratio <= 0.2) return { label: "Low range", tone: "good", detail: `${(ratio * 100).toFixed(0)}% of ATH` };
  return { label: "Mid-range", tone: "", detail: `${(ratio * 100).toFixed(0)}% of ATH` };
}

function renderMarketPhase(token) {
  const phase = marketPhase(token);
  if (!phase) return "";
  const reactivationHighRange = token.filterCategories.includes("reactivation")
    && (phase.label === "Upper range" || phase.label === "Near ATH");
  const caution = reactivationHighRange
    ? "not a clean low-volume reactivation setup"
    : "";
  return `
    <div class="kv">
      <span>Market phase</span>
      <span>${chip(phase.label, phase.tone)} ${esc(phase.detail)}${caution ? ` <span class="muted-inline">${esc(caution)}</span>` : ""}</span>
    </div>
  `;
}

function gmgnTokenUrl(token) {
  return token.token_address ? `https://gmgn.ai/sol/token/${encodeURIComponent(token.token_address)}` : "";
}

function renderLaunchContext(token) {
  const context = TOKEN_MARKET_CONTEXT[token.token_address];
  if (!context) return "";
  const currentMcap = Number(token.scanMcapUsd || token.currentMcap || 0);
  const lowMultiple = currentMcap && context.day2LowMcapUsd ? currentMcap / context.day2LowMcapUsd : null;
  const highMultiple = currentMcap && context.day2HighMcapUsd ? currentMcap / context.day2HighMcapUsd : null;
  const athUpside = currentMcap && context.athMcapUsd ? ((context.athMcapUsd / currentMcap) - 1) * 100 : null;
  const multiples = lowMultiple && highMultiple
    ? `early 7-8 May entries are ~${highMultiple.toFixed(1)}x-${lowMultiple.toFixed(1)}x vs current scan`
    : "early 7-8 May entries are materially in profit";
  return `
    <div class="kv">
      <span>Launch context</span>
      <span>
        07 May ${money(context.launchStartMcapUsd)} -> ${money(context.launchHighMcapUsd)};
        08 May ${money(context.day2LowMcapUsd)} low -> ${money(context.day2HighMcapUsd)} high;
        ATH ${context.athAt ? `${esc(dateLabel(context.athAt))} / ` : ""}${money(context.athMcapUsd)}.
        ${esc(multiples)}${athUpside !== null ? `; ATH upside ${pct(athUpside)}` : ""}.
        ${chip(context.riskLabel, "warn")}
        <span class="muted-inline">${esc(context.source)}</span>
      </span>
    </div>
  `;
}

function renderTokenDetail(token) {
  if (!token) return `<aside class="detail"><h2>No token selected</h2></aside>`;
  const gmgnUrl = gmgnTokenUrl(token);
  return `
    <aside class="detail token-detail">
      <div class="detail-head">
        <h2>${esc(token.symbol)} <span class="muted">${esc(token.name)}</span>${token.hidden ? ` ${chip("hidden", "warn")}` : ""}</h2>
        ${renderHiddenAction(token)}
      </div>
      <div class="detail-hero">
        <div>
          <strong class="${pClass(token.profitPct)}">${pct(token.profitPct)}</strong>
          <span>profit since caught</span>
        </div>
        <div>
          <strong>${sol(token.totalSuspiciousSol)}</strong>
          <span>unique noticed flow</span>
        </div>
      </div>
      <div class="kv"><span>Token age</span><span>${esc(durationLabel(token.tokenAgeHours))}${token.tokenCreatedAt ? ` / launched ${esc(dateLabel(token.tokenCreatedAt))}` : ""}</span></div>
      <div class="kv"><span>Caught</span><span>${esc(dateLabel(token.firstSignalAt))} / ${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)} mcap</span></div>
      <div class="kv"><span>ATH mcap</span><span>${token.athMcapUsd ? `${token.athMcapAt ? `${esc(dateLabel(token.athMcapAt))} / ` : ""}${money(token.athMcapUsd)} mcap ${chip(athSourceLabel(token.athSource), "good")}` : `${chip(athStatusLabel(token.athStatus, token.athError), "warn")}`}</span></div>
      <div class="kv"><span>Market now</span><span>${moneyMaybe(token.scanMcapUsd || token.currentMcap)} mcap / ${money(token.liquidityUsd)} liq${token.scanMcapAt ? ` / ${esc(dateLabel(token.scanMcapAt))}` : ""}</span></div>
      ${renderMarketPhase(token)}
      ${renderLaunchContext(token)}
      <div class="kv"><span>Signal tier</span><span>${renderSignalTier(token)}</span></div>
      <div class="kv"><span>Scanner filter</span><span>${renderFilterLine(token)}</span></div>
      <div class="kv"><span>Signal quality</span><span>${renderSignalQuality(token)}</span></div>
      <div class="kv"><span>Wallet cluster</span><span>${renderWalletCluster(token)}</span></div>
      <div class="kv"><span>Wallet PnL</span><span>${renderWalletSummary(token)}</span></div>
      <div class="kv"><span>Primary narrative</span><span>${renderNarrativeLine(token)}</span></div>
      <div class="kv"><span>Lore thesis</span><span>${renderLoreProof(token)}</span></div>
      <div class="kv"><span>Proof basis</span><span>${renderEvidenceLine(token)}</span></div>
      <div class="kv"><span>Source links</span><span>${renderTokenSourceLinks(token)}</span></div>
      <div class="kv"><span>Token / terminal</span><span><code>${esc(token.token_address)}</code> ${gmgnUrl ? `<a href="${esc(gmgnUrl)}" target="_blank" rel="noreferrer">Open GMGN</a>` : `<code>${esc(token.pool_address)}</code>`}</span></div>
      <h2>Caller Network</h2>
      ${renderCallerRows(token)}
      <h2>Noticed Wallet PnL</h2>
      ${renderWalletRows(token)}
      <h2>Signal Timeline</h2>
      <div class="timeline">
        ${token.alerts.map((alert) => `
          <div class="timeline-item">
            <strong>${esc(dateLabel(alert.window_start))} ${tierChip(alertTier(alert))}</strong>
            <span>OBS ${moneyMaybe(alert.obs_mcap_usd || alert.pool?.mcap_usd)} / score ${esc(alert.score)} / hard ${sol(alert.hard_sol || 0)} / support ${sol(alert.support_sol || 0)} / ${esc(alert.filterLane || effectiveAlertLane(alert))}</span>
          </div>
        `).join("")}
      </div>
    </aside>
  `;
}

function renderTokens() {
  const tokens = filteredTokens(buildTokenSignals());
  renderMetrics(tokens);
  if (!tokens.length) {
    els.content.innerHTML = emptyMessage("No caught tokens match the filters.");
    return;
  }
  const token = selectedToken(tokens);
  state.selectedTokenKey = token.key;
  els.content.innerHTML = `
    <div class="grid token-grid">
      <div class="list">${tokens.map(renderTokenRow).join("")}</div>
      ${renderTokenDetail(token)}
    </div>
  `;
  document.querySelectorAll(".token-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTokenKey = row.dataset.tokenKey;
      render();
    });
  });
  document.querySelectorAll(".token-terminal-link").forEach((link) => {
    link.addEventListener("click", (event) => event.stopPropagation());
  });
  bindTokenHideActions();
}

function narrativeGroups(tokens) {
  const groups = new Map();
  tokens.forEach((token) => {
    token.narratives.forEach((name) => {
      if (!groups.has(name)) {
        groups.set(name, {
          name,
          tokens: [],
          totalSol: 0,
          bestPnl: null,
          bestToken: null,
          alerts: 0,
          context: token.narrative.news,
          firstSignalAt: null,
          latestSignalAt: null,
          maxScore: 0,
          wallets: new Set(),
          secondary: new Map(),
        });
      }
      const group = groups.get(name);
      group.tokens.push(token);
      group.totalSol += token.totalSuspiciousSol;
      group.alerts += token.alertCount;
      if (token.profitPct !== null && (group.bestPnl === null || token.profitPct > group.bestPnl)) {
        group.bestPnl = token.profitPct;
        group.bestToken = token;
      }
      group.maxScore = Math.max(group.maxScore, token.maxScore || 0);
      token.wallets.forEach((wallet) => group.wallets.add(wallet.owner));
      token.narrative.secondary.forEach((secondary) => {
        group.secondary.set(secondary, (group.secondary.get(secondary) || 0) + 1);
      });
      if (!group.firstSignalAt || new Date(token.firstSignalAt) < new Date(group.firstSignalAt)) {
        group.firstSignalAt = token.firstSignalAt;
      }
      if (!group.latestSignalAt || new Date(token.lastSignalAt) > new Date(group.latestSignalAt)) {
        group.latestSignalAt = token.lastSignalAt;
      }
    });
  });
  return [...groups.values()].map((group) => {
    group.tokens.sort((a, b) => {
      const aScore = Math.max(0, Number(a.profitPct || 0)) + Number(a.totalSuspiciousSol || 0) / 5 + Number(a.maxScore || 0);
      const bScore = Math.max(0, Number(b.profitPct || 0)) + Number(b.totalSuspiciousSol || 0) / 5 + Number(b.maxScore || 0);
      return bScore - aScore;
    });
    group.secondaryList = [...group.secondary.entries()].sort((a, b) => b[1] - a[1]);
    group.uniqueWallets = group.wallets.size;
    return group;
  }).sort((a, b) => b.tokens.length - a.tokens.length || b.totalSol - a.totalSol);
}

function renderNarrativeTokenRows(group) {
  return group.tokens.map((token) => `
    <button class="narrative-token-row${token.hidden ? " is-hidden" : ""}" type="button" data-token-key="${esc(token.key)}">
      <span>
        <strong>${esc(token.symbol)}</strong>
        <small>${esc([token.name || token.narrative.primary, token.hidden ? "hidden" : ""].filter(Boolean).join(" / "))}</small>
      </span>
      <span class="${pClass(token.profitPct)}">${pct(token.profitPct)}</span>
      <span>${money(token.currentMcap)}</span>
      <span>${sol(token.totalSuspiciousSol)}</span>
    </button>
  `).join("");
}

function renderNarrativeDetail(group) {
  if (!group) return `<aside class="detail"><h2>No narrative selected</h2></aside>`;
  const secondary = group.secondaryList.length
    ? group.secondaryList.map(([name, count]) => chip(`${name} ${count}`)).join("")
    : "-";
  return `
    <aside class="detail narrative-detail">
      <h2>${esc(group.name)}</h2>
      <div class="detail-hero">
        <div>
          <strong>${esc(group.tokens.length)}</strong>
          <span>caught tokens</span>
        </div>
        <div>
          <strong class="${pClass(group.bestPnl)}">${pct(group.bestPnl)}</strong>
          <span>best signal${group.bestToken ? ` / ${esc(group.bestToken.symbol)}` : ""}</span>
        </div>
      </div>
      <div class="kv"><span>Why it matters</span><span>${esc(group.context?.summary || group.context?.headline || "Narrative is based on scanner metadata and social context.")}</span></div>
      <div class="kv"><span>First/latest signal</span><span>${esc(dateLabel(group.firstSignalAt))} -> ${esc(dateLabel(group.latestSignalAt))}</span></div>
      <div class="kv"><span>Signal strength</span><span>${esc(group.alerts)} signals / max score ${esc(group.maxScore)} / ${sol(group.totalSol)}</span></div>
      <div class="kv"><span>Wallet coverage</span><span>${esc(group.uniqueWallets)} noticed wallets across this narrative</span></div>
      <div class="kv"><span>Secondary flavor</span><span>${secondary}</span></div>
      ${group.context?.sources?.length ? `<div class="kv"><span>Sources</span><span>${group.context.sources.map((source) => `<a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.label)}</a>`).join(" ")}</span></div>` : ""}
      <h2>Tokens In Narrative</h2>
      <div class="narrative-token-head">
        <span>Token</span>
        <span>PnL</span>
        <span>Mcap</span>
        <span>Flow</span>
      </div>
      <div class="narrative-token-list">
        ${renderNarrativeTokenRows(group)}
      </div>
    </aside>
  `;
}

function renderNarratives() {
  const tokens = filteredTokens(buildTokenSignals());
  renderMetrics(tokens);
  const groups = narrativeGroups(tokens);
  if (!groups.length) {
    els.content.innerHTML = emptyMessage("No narratives match the filters.");
    return;
  }
  const group = selectedNarrativeGroup(groups);
  state.selectedNarrative = group.name;
  els.content.innerHTML = `
    <div class="grid narrative-layout">
      <div class="narrative-grid">
        ${groups.map((item) => `
        <article class="narrative-card${item.name === group.name ? " is-selected" : ""}" data-narrative="${esc(item.name)}">
          <div class="symbol-line">
            <span class="symbol">${esc(item.name)}</span>
            <span class="muted">${item.tokens.length} tokens</span>
          </div>
          <div class="meta">
            <span>${esc(item.alerts)} signals</span>
            <span>${sol(item.totalSol)}</span>
            <span class="${pClass(item.bestPnl)}">best ${pct(item.bestPnl)}</span>
          </div>
          <p class="narrative-context">${esc(item.context?.headline || "")}</p>
          <div class="chips">${item.tokens.slice(0, 12).map((token) => chip(`${token.symbol} ${pct(token.profitPct)}`)).join("")}</div>
        </article>
      `).join("")}
      </div>
      ${renderNarrativeDetail(group)}
    </div>
  `;
  document.querySelectorAll(".narrative-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedNarrative = card.dataset.narrative;
      render();
    });
  });
  document.querySelectorAll(".narrative-token-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTokenKey = row.dataset.tokenKey;
      state.tab = "tokens";
      render();
    });
  });
}

function selectedFilterGroup(groups) {
  return groups.find((group) => group.name === state.selectedFilter) || groups[0] || null;
}

function selectedFilterToken(group) {
  if (!group?.tokens?.length) return null;
  return group.tokens.find((token) => token.key === state.selectedTokenKey) || group.tokens[0];
}

function filterGroups(tokens) {
  const groups = new Map();
  tokens.forEach((token) => {
    (token.filterCategories.length ? token.filterCategories : ["legacy"]).forEach((name) => {
      if (!groups.has(name)) {
        groups.set(name, {
          name,
          meta: filterMeta(name),
          tokens: [],
          tokenKeys: new Set(),
          totalSol: 0,
          bestPnl: null,
          bestToken: null,
          alerts: 0,
          firstSignalAt: null,
          latestSignalAt: null,
          maxScore: 0,
          wallets: new Set(),
          rawEvents: 0,
          uniqueEvents: 0,
          noticedWallets: 0,
          tierCounts: {},
        });
      }
      const group = groups.get(name);
      if (!group.tokenKeys.has(token.key)) {
        group.tokens.push(token);
        group.tokenKeys.add(token.key);
        group.tierCounts[token.actionTier] = (group.tierCounts[token.actionTier] || 0) + 1;
      }
      const groupAlerts = token.alerts;
      const groupEvents = uniqueAlertEvents(groupAlerts);
      group.alerts += groupAlerts.length;
      group.rawEvents += rawEventCount(groupAlerts);
      group.uniqueEvents += groupEvents.length;
      group.noticedWallets += sumAlertField(groupAlerts, "suspicious_wallets") || groupEvents.length;
      group.totalSol += sumAlertField(groupAlerts, "suspicious_sol") || sumEventSol(groupEvents);
      const laneMaxScore = groupAlerts.length ? Math.max(...groupAlerts.map((alert) => Number(alert.score || 0))) : 0;
      group.maxScore = Math.max(group.maxScore, laneMaxScore);
      if (token.profitPct !== null && (group.bestPnl === null || token.profitPct > group.bestPnl)) {
        group.bestPnl = token.profitPct;
        group.bestToken = token;
      }
      groupAlerts.forEach((alert) => {
        const signalAt = alert.window_start || alert.created_at;
        if (!group.firstSignalAt || new Date(signalAt) < new Date(group.firstSignalAt)) group.firstSignalAt = signalAt;
        if (!group.latestSignalAt || new Date(signalAt) > new Date(group.latestSignalAt)) group.latestSignalAt = signalAt;
      });
      groupEvents.forEach((event) => {
        const owner = eventOwner(event);
        if (owner) group.wallets.add(owner);
      });
    });
  });
  return [...groups.values()].map((group) => {
    group.tokens.sort((a, b) => {
      const aScore = Math.max(0, Number(a.profitPct || 0)) + Number(a.totalSuspiciousSol || 0) / 5 + Number(a.maxScore || 0);
      const bScore = Math.max(0, Number(b.profitPct || 0)) + Number(b.totalSuspiciousSol || 0) / 5 + Number(b.maxScore || 0);
      return bScore - aScore;
    });
    group.uniqueWallets = group.wallets.size;
    return group;
  }).sort((a, b) => {
    const orderA = FILTER_ORDER.indexOf(a.name);
    const orderB = FILTER_ORDER.indexOf(b.name);
    return (orderA === -1 ? 999 : orderA) - (orderB === -1 ? 999 : orderB);
  });
}

function renderFilterTokenRows(group) {
  return group.tokens.map((token) => {
    const flow = sumAlertField(token.alerts, "suspicious_sol") || sumEventSol(uniqueAlertEvents(token.alerts));
    const phase = marketPhase(token);
    const driftLabel = token.hasFilterDrift ? `now ${filterMeta(token.currentFilter).label}` : "";
    const selected = token.key === state.selectedTokenKey ? " is-selected" : "";
    const hidden = token.hidden ? " is-hidden" : "";
    return `
      <button class="filter-token-row${selected}${hidden}" type="button" data-token-key="${esc(token.key)}">
        <span class="filter-token-name">
          <strong>${esc(token.symbol)}</strong>
          <small>${esc([tierMeta(token.actionTier).label, token.name || token.narrative.primary, driftLabel, phase?.label, token.hidden ? "hidden" : ""].filter(Boolean).join(" / "))}</small>
        </span>
        <span class="filter-token-value">
          <strong>${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)}</strong>
          <small>mcap</small>
        </span>
        <span class="filter-token-value ${pClass(token.profitPct)}">
          <strong>${pct(token.profitPct)}</strong>
          <small>since catch</small>
        </span>
        <span class="filter-token-value">
          <strong>${sol(flow)}</strong>
          <small>noticed</small>
        </span>
      </button>
    `;
  }).join("");
}

function renderFilterTokenPanel(group) {
  if (!group) return `<section class="filter-token-panel"><div class="empty">No filter selected.</div></section>`;
  return `
    <section class="filter-token-panel">
      <div class="filter-panel-head">
        <div>
          <h2>${esc(group.meta.label)}</h2>
          <p>${esc(group.meta.criteria)}</p>
        </div>
        <div class="filter-panel-metrics">
          <span><strong>${esc(group.tokens.length)}</strong> tokens</span>
          <span><strong>${esc(group.noticedWallets)}</strong> wallets</span>
          <span><strong>${sol(group.totalSol)}</strong></span>
        </div>
      </div>
      <div class="filter-thesis">${esc(group.meta.thesis)}</div>
      <div class="filter-quick-stats">
        <span>${esc(group.alerts)} signals</span>
        <span>max score ${esc(group.maxScore)}</span>
        <span>${esc(group.noticedWallets)} wallets</span>
        <span>${esc(dateLabel(group.firstSignalAt))} -> ${esc(dateLabel(group.latestSignalAt))}</span>
      </div>
      <div class="filter-token-head">
        <span>Token</span>
        <span>Caught</span>
        <span>PnL</span>
        <span>Flow</span>
      </div>
      <div class="filter-token-list">
        ${renderFilterTokenRows(group)}
      </div>
    </section>
  `;
}

function renderFilters() {
  const tokens = filteredTokens(buildTokenSignals());
  renderMetrics(tokens);
  const groups = filterGroups(tokens);
  if (!groups.length) {
    els.content.innerHTML = emptyMessage("No scanner filters match the current filters.");
    return;
  }
  const group = selectedFilterGroup(groups);
  state.selectedFilter = group.name;
  const token = selectedFilterToken(group);
  if (token) state.selectedTokenKey = token.key;
  els.content.innerHTML = `
    <div class="filter-workspace">
      <section class="filter-master">
        <div class="workspace-title">Scanner Filters</div>
        <div class="filter-rail">
          ${groups.map((item) => `
          <article class="narrative-card filter-card${item.name === group.name ? " is-selected" : ""}" data-filter="${esc(item.name)}">
            <div class="symbol-line">
              <span class="symbol">${esc(item.meta.label)}</span>
              <span class="muted">${item.tokens.length}</span>
            </div>
            <div class="meta">
              <span>${esc(item.alerts)} sig</span>
              <span>${sol(item.totalSol)}</span>
              <span class="${pClass(item.bestPnl)}">${pct(item.bestPnl)}</span>
            </div>
            <div class="chips">${item.tierCounts.actionable ? chip(`${item.tierCounts.actionable} actionable`, "good") : ""}${item.tierCounts.watch ? chip(`${item.tierCounts.watch} watch`, "warn") : ""}${item.tierCounts.late_chase || item.tierCounts.noise ? chip(`${Number(item.tierCounts.late_chase || 0) + Number(item.tierCounts.noise || 0)} parked`, "bad") : ""}</div>
          </article>
        `).join("")}
        </div>
      </section>
      ${renderFilterTokenPanel(group)}
      ${renderTokenDetail(token)}
    </div>
  `;
  document.querySelectorAll(".filter-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedFilter = card.dataset.filter;
      const nextGroup = groups.find((item) => item.name === state.selectedFilter);
      if (nextGroup && !nextGroup.tokens.some((token) => token.key === state.selectedTokenKey)) {
        state.selectedTokenKey = nextGroup.tokens[0]?.key || null;
      }
      render();
    });
  });
  document.querySelectorAll(".filter-token-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTokenKey = row.dataset.tokenKey;
      render();
    });
  });
  document.querySelectorAll(".token-terminal-link").forEach((link) => {
    link.addEventListener("click", (event) => event.stopPropagation());
  });
  bindTokenHideActions();
}

function alertMatches(alert) {
  const pool = alert.pool || {};
  const key = tokenKeyFromPool(pool);
  if (!state.showHidden && isTokenHidden(key)) return false;
  const token = buildTokenSignals().find((item) => item.key === key);
  if (!tierMatches(alertTier(alert))) return false;
  if (state.lane !== "all" && effectiveAlertLane(alert) !== state.lane) return false;
  if (state.heat !== "all" && socialHeat(alert) !== state.heat) return false;
  if (Number(alert.score || 0) < state.minScore) return false;
  const query = state.query.toLowerCase();
  if (!query) return true;
  return [pool.symbol, pool.name, pool.token_address, pool.pool_address, token?.narratives?.join(" ")].join(" ").toLowerCase().includes(query);
}

function renderAlertRow(alert) {
  const pool = alert.pool || {};
  const token = buildTokenSignals().find((item) => item.key === tokenKeyFromPool(pool));
  return `
    <article class="alert-row">
      <div class="score"><strong>${esc(alert.score ?? 0)}</strong><span>score</span></div>
      <div>
        <div class="symbol-line">
          <span class="symbol">${esc(pool.symbol || pool.name || "Unknown")}</span>
          <span class="muted">${esc(effectiveAlertLane(alert))}</span>
        </div>
        <div class="meta">
          <span>${esc(dateLabel(alert.window_start))}</span>
          <span>${esc(alert.suspicious_wallets || 0)} wallets</span>
          <span>${sol(alert.suspicious_sol)}</span>
          <span class="${pClass(token?.profitPct)}">token ${pct(token?.profitPct)}</span>
        </div>
        <div class="chips">${classChips(alert.classes)}${chip(socialLabel(socialHeat(alert), alert.social?.reason || ""), socialHeat(alert) === "disabled" ? "warn" : "")}${(alert.routed_buys || 0) ? chip(`routed ${alert.routed_buys}`, "warn") : ""}</div>
        <div class="chips">${tierChip(alertTier(alert))}${(alert.quality_reasons || []).slice(0, 3).map((item) => chip(item)).join("")}${(alert.quality_penalties || []).slice(0, 3).map((item) => chip(item, "warn")).join("")}</div>
      </div>
      <div class="right-metrics">
        <span>${money(pool.mcap_usd)} mcap</span>
        <span>${money(pool.liquidity_usd)} liq</span>
      </div>
    </article>
  `;
}

function renderRawAlerts() {
  const alerts = allAlerts().filter(alertMatches);
  renderMetrics(filteredTokens(buildTokenSignals()));
  if (!alerts.length) {
    els.content.innerHTML = emptyMessage("No raw alerts match the filters.");
    return;
  }
  els.content.innerHTML = `<div class="list">${alerts.slice(0, 80).map(renderAlertRow).join("")}</div>`;
}

function updateTabs() {
  els.tabs.forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.tab === state.tab);
  });
}

function render() {
  renderStatus();
  updateTabs();
  if (!state.report?.generated_at) {
    els.metrics.innerHTML = "";
    els.content.innerHTML = `<div class="empty">No JSON report yet. Wait for the auto scan or force a scan.</div>`;
    return;
  }
  if (state.tab === "narratives") renderNarratives();
  else if (state.tab === "filters") renderFilters();
  else if (state.tab === "alerts") renderRawAlerts();
  else renderTokens();
}

async function runScan() {
  if (state.staticMode) return;
  const lane = els.modeSelect.value;
  els.runScan.disabled = true;
  await fetch(`/api/scan?lane=${encodeURIComponent(lane)}`, { method: "POST" });
  await loadData();
}

els.refresh.addEventListener("click", loadData);
els.runScan.addEventListener("click", runScan);
els.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  render();
});
els.scopeFilter.addEventListener("change", (event) => {
  state.scope = event.target.value;
  state.selectedTokenKey = null;
  state.selectedFilter = null;
  render();
});
els.tierFilter.addEventListener("change", (event) => {
  state.tier = event.target.value;
  state.selectedTokenKey = null;
  render();
});
els.heatFilter.addEventListener("change", (event) => {
  state.heat = event.target.value;
  render();
});
els.laneFilter.addEventListener("change", (event) => {
  state.lane = event.target.value;
  render();
});
els.scoreInput.addEventListener("input", (event) => {
  state.minScore = Number(event.target.value || 0);
  render();
});
els.showHiddenInput.addEventListener("change", (event) => {
  state.showHidden = event.target.checked;
  if (!state.showHidden && isTokenHidden(state.selectedTokenKey)) {
    state.selectedTokenKey = null;
  }
  render();
});
els.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    render();
  });
});

loadData().catch((error) => {
  els.content.innerHTML = `<div class="empty">Dashboard API is not ready: ${esc(error.message)}</div>`;
});

setInterval(() => {
  loadData().catch(() => {});
}, 15000);
