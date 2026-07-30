const HIDDEN_TOKENS_KEY = "solana-radar:hidden-token-keys:v1";
const DELETE_SYNC_SECRET_KEY = "solana-radar:delete-sync-secret:v1";
const DELETE_SYNC_ENDPOINT = "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/deleted-token";

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
    // Local delete state is an optional browser preference.
  }
}

function normalizeDeletedId(value) {
  const text = String(value || "").trim();
  return text || null;
}

function deletedIdSet(values, keys) {
  const ids = new Set();
  if (values && typeof values === "object" && !Array.isArray(values)) {
    Object.values(values).forEach((entry) => {
      if (entry && typeof entry === "object") {
        keys.forEach((key) => {
          const cleaned = normalizeDeletedId(entry[key]);
          if (cleaned) ids.add(cleaned);
        });
      } else {
        const cleaned = normalizeDeletedId(entry);
        if (cleaned) ids.add(cleaned);
      }
    });
    return ids;
  }
  if (!Array.isArray(values)) return ids;
  values.forEach((entry) => {
    if (entry && typeof entry === "object") {
      keys.forEach((key) => {
        const cleaned = normalizeDeletedId(entry[key]);
        if (cleaned) ids.add(cleaned);
      });
    } else {
      const cleaned = normalizeDeletedId(entry);
      if (cleaned) ids.add(cleaned);
    }
  });
  return ids;
}

const state = {
  report: null,
  history: [],
  market: {},
  scanStatus: {},
  discoveryStatus: {},
  tab: "filters",
  query: "",
  workflow: "new_signals",
  signal: "all",
  heat: "all",
  lane: "reactivation",
  minScore: 0,
  selectedTokenKey: null,
  selectedNarrative: null,
  selectedFilter: null,
  selectedAlertId: null,
  detailTab: "overview",
  mobileDetailOpen: false,
  showHidden: false,
  hiddenTokenKeys: loadHiddenTokenKeys(),
  serverDeletedTokenKeys: new Set(),
  serverDeletedPoolKeys: new Set(),
  staticMode: false,
  staticExtrasLoadedFor: null,
  staticExtrasLoadingFor: null,
};

const els = {
  subtitle: document.querySelector("#subtitle"),
  statusRow: document.querySelector("#statusRow"),
  metrics: document.querySelector("#metrics"),
  content: document.querySelector("#content"),
  refresh: document.querySelector("#refresh"),
  runScan: document.querySelector("#runScan"),
  searchInput: document.querySelector("#searchInput"),
  workflowFilter: document.querySelector("#workflowFilter"),
  signalFilter: document.querySelector("#signalFilter"),
  heatFilter: document.querySelector("#heatFilter"),
  scoreInput: document.querySelector("#scoreInput"),
  showHiddenInput: document.querySelector("#showHiddenInput"),
  filterToggle: document.querySelector("#filterToggle"),
  filters: document.querySelector(".filters"),
  tabs: document.querySelectorAll(".tab"),
};

const FILTER_META = {
  reactivation: {
    label: "Reactivation",
    criteria: "15d+ / $0-$5m mcap / liq >= $3k / 5m burst + retained buy-wave",
    thesis: "older migrated tokens ranked by renewed 5m and 1h activity, then confirmed by distributed net buying and balances that still retain the acquired supply. ATH is risk context, not a discovery gate.",
  },
};

const ACTIVE_SCANNER_LANES = new Set(["reactivation"]);
const FILTER_ORDER = ["reactivation"];
const MIGRATED_PUMPFUN_DEX_ALLOWLIST = new Set(["pumpfun-amm", "pumpswap"]);
const HARD_WALLET_CLASSES = new Set(["fresh", "freshish", "dormant"]);
const SUPPORT_WALLET_CLASSES = new Set(["low_tx"]);
const CLASS_LABELS = {
  market_wave: "market wave",
  wave_buyer: "wave buyer",
  sticky_buyer: "sticky buyer",
};
const TIER_META = {
  actionable: {
    label: "Strong signal",
    tone: "good",
    rank: 5,
    summary: "hard onchain evidence before the move looks fully crowded",
  },
  hot_reactivation: {
    label: "Fast move",
    tone: "warn",
    rank: 4,
    summary: "strong early reactivation with high velocity or incomplete ATH confirmation",
  },
  watch: {
    label: "Watch",
    tone: "warn",
    rank: 3,
    summary: "real signal, but needs confirmation or cleaner market setup",
  },
  holding: {
    label: "Holding",
    tone: "good",
    rank: 3.5,
    summary: "the original buyer cohort still retains the accumulated tokens",
  },
  weakening: {
    label: "Cohort reduced",
    tone: "warn",
    rank: 2.5,
    summary: "tokens left original buyer wallets; the balance check does not distinguish a sale from a transfer",
  },
  late_chase: {
    label: "Late entry",
    tone: "bad",
    rank: 2,
    summary: "signal exists but it appears after an extended move or crowded volume",
  },
  recheck_due: {
    label: "Data check needed",
    tone: "warn",
    rank: 1.5,
    summary: "the original buyer cohort has not been verified recently enough",
  },
  noise: {
    label: "Low confidence",
    tone: "",
    rank: 1,
    summary: "weak or support-only evidence",
  },
  inactive: {
    label: "Closed",
    tone: "bad",
    rank: 0,
    summary: "the original accumulation thesis was invalidated by confirmed cohort distribution",
  },
};

const WORKFLOW_META = {
  new_signals: {
    label: "New signals",
    tone: "good",
    rank: 4,
    summary: "fresh onchain activity found in the latest successful scan",
  },
  holding: {
    label: "Holding",
    tone: "good",
    rank: 3,
    summary: "the original accumulation cohort still holds",
  },
  needs_attention: {
    label: "Needs attention",
    tone: "warn",
    rank: 2,
    summary: "cohort reduction, late activity, or a wallet-balance check needs review",
  },
  closed: {
    label: "Closed",
    tone: "bad",
    rank: 1,
    summary: "the original accumulation thesis was invalidated",
  },
  events_only: {
    label: "Events only",
    tone: "",
    rank: 0,
    summary: "low-confidence activity is kept in Events, not the working radar",
  },
};

const TOKEN_MARKET_CONTEXT = {
  HANTAYLiPiQ8d8dkJizcL8gJQHWBKF5ZeL1neeLqwbzc: {
    source: "historical chart snapshot",
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

const EXPLICIT_FILTERS = ["reactivation"];
const INFERRABLE_FILTERS = ["reactivation"];

const FILTER_RULES = {
  reactivation: {
    ageMin: 360,
    ageMax: null,
    mcapMin: 0,
    mcapMax: 5_000_000,
    liquidityMin: 3_000,
    volumeMin: 100,
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
  if (EXPLICIT_FILTERS.includes(alert.lane)) return alert.lane;
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
  if (snapshot.volume1hUsd !== null && snapshot.volume1hUsd < rule.volumeMin) return false;
  return true;
}

function alertId(alert) {
  const pool = alert?.pool || {};
  return [
    pool.pool_address || pool.token_address || pool.symbol || "pool",
    alert?.window_start || alert?.created_at || "window",
    alert?.window_end || "",
  ].join(":");
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
  if (source === "gmgn") return "GMGN";
  if (source === "solana_tracker") return "Solana Tracker";
  if (source === "ohlcv_high") return "OHLCV high";
  return "ATH missing";
}

function athStatusLabel(status, error = "") {
  if (status === "ready") return "ready";
  if (status === "partial") return "market cap ready, date pending";
  if (status === "missing_api_key") return "missing API key";
  if (status === "error") {
    if (String(error).includes("403")) return "GMGN 403";
    if (String(error).includes("429")) return "GMGN rate limited";
    return "ATH retry pending";
  }
  if (status === "unverified") return "ATH unverified";
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

function tokenAvatar(token, compact = false) {
  const label = String(token?.symbol || token?.name || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
  const image = token?.imageUrl
    ? `<img src="${esc(token.imageUrl)}" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  return `<span class="token-avatar${compact ? " is-compact" : ""}" aria-hidden="true"><span>${esc(label)}</span>${image}</span>`;
}

function tokenKey(value) {
  return typeof value === "string" ? value : value?.key || tokenKeyFromPool(value?.pool || value || {});
}

function tokenPoolAddress(value) {
  if (!value || typeof value === "string") return "";
  return value.pool_address || value.latestPool?.pool_address || value.pool?.pool_address || "";
}

function applyDeletedTokenList(data = {}) {
  state.serverDeletedTokenKeys = new Set([
    ...deletedIdSet(data.tokens, ["token_address", "token", "address", "id"]),
    ...deletedIdSet(data.token_addresses, ["token_address", "token", "address", "id"]),
    ...deletedIdSet(data.entries, ["token_address", "token", "address", "id"]),
  ]);
  state.serverDeletedPoolKeys = new Set([
    ...deletedIdSet(data.pools, ["pool_address", "pool", "address", "id"]),
    ...deletedIdSet(data.pool_addresses, ["pool_address", "pool", "address", "id"]),
    ...deletedIdSet(data.entries, ["pool_address", "pool", "address", "id"]),
  ]);
}

function isTokenServerDeleted(value) {
  const key = tokenKey(value);
  const poolAddress = tokenPoolAddress(value);
  return Boolean(
    (key && state.serverDeletedTokenKeys.has(key))
    || (poolAddress && state.serverDeletedPoolKeys.has(poolAddress))
  );
}

function isTokenHidden(value) {
  const key = tokenKey(value);
  return Boolean((key && state.hiddenTokenKeys.has(key)) || isTokenServerDeleted(value));
}

function setTokenHidden(key, hidden) {
  if (!key) return;
  if (hidden) state.hiddenTokenKeys.add(key);
  else state.hiddenTokenKeys.delete(key);
  saveHiddenTokenKeys(state.hiddenTokenKeys);
}

function deleteSyncSecret() {
  let secret = localStorage.getItem(DELETE_SYNC_SECRET_KEY) || "";
  if (!secret) {
    secret = window.prompt("Enter scanner delete sync secret") || "";
    if (secret) localStorage.setItem(DELETE_SYNC_SECRET_KEY, secret);
  }
  return secret;
}

async function persistTokenDeletion(token, hidden, providedSecret = "") {
  if (!token) return;
  let endpoint = "/api/deleted-token";
  const headers = { "content-type": "application/json" };
  if (state.staticMode) {
    endpoint = DELETE_SYNC_ENDPOINT;
    const secret = providedSecret || deleteSyncSecret();
    if (!secret) return;
    headers["x-dispatch-secret"] = secret;
  }
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({
      action: hidden ? "delete" : "restore",
      token_address: token.token_address || "",
      pool_address: token.pool_address || token.latestPool?.pool_address || "",
      symbol: token.symbol || "",
      name: token.name || "",
    }),
  });
  const payload = await response.json();
  if (!response.ok && response.status === 401 && state.staticMode) {
    localStorage.removeItem(DELETE_SYNC_SECRET_KEY);
  }
  if (!response.ok) throw new Error(payload.error || "delete_failed");
  applyDeletedTokenList(payload.deleted_tokens || {});
}

async function syncLocalDeletedTokens() {
  if (!state.staticMode || !state.hiddenTokenKeys.size) return;
  const secret = deleteSyncSecret();
  if (!secret) return;
  const tokens = buildTokenSignals().filter((token) => state.hiddenTokenKeys.has(token.key) && !isTokenServerDeleted(token));
  for (const token of tokens) {
    await persistTokenDeletion(token, true, secret);
  }
  render();
}

function renderHiddenAction(token, compact = false) {
  const hidden = isTokenHidden(token);
  const serverDeleted = isTokenServerDeleted(token);
  const disabled = serverDeleted && state.staticMode;
  const label = hidden ? (disabled ? "Deleted" : "Restore") : "Delete";
  return `
    <button
      class="${compact ? "chip-action" : "secondary-action"} token-hide-toggle"
      type="button"
      data-token-key="${esc(token.key)}"
      ${disabled ? "disabled" : ""}
      data-hidden="${hidden ? "true" : "false"}"
      title="${hidden ? (disabled ? "Deleted in scanner blacklist" : "Restore token to scanner lists") : "Delete this false catch from dashboard lists"}"
    >${label}</button>
  `;
}

function normalizeDex(value) {
  return String(value || "").trim().toLowerCase().replaceAll("_", "-");
}

function activeDexAllowlist() {
  const configured = state.report?.config?.dex_allowlist;
  const values = Array.isArray(configured) && configured.length
    ? configured
    : [...MIGRATED_PUMPFUN_DEX_ALLOWLIST];
  return new Set(values.map(normalizeDex).filter(Boolean));
}

function isPumpfunPool(pool = {}) {
  return activeDexAllowlist().has(normalizeDex(pool.dex));
}

function narrativeTone(narrative) {
  return narrative?.source === "scanner_token_intel" ? "good" : "warn";
}

function classChips(classes = {}) {
  return Object.entries(classes)
    .map(([name, count]) => chip(`${CLASS_LABELS[name] || name} ${count}`))
    .join("");
}

function tierMeta(tier) {
  return TIER_META[tier] || TIER_META.noise;
}

function tierChip(tier) {
  const meta = tierMeta(tier);
  return chip(meta.label, meta.tone);
}

function workflowMeta(workflow) {
  return WORKFLOW_META[workflow] || WORKFLOW_META.needs_attention;
}

function workflowChip(workflow) {
  const meta = workflowMeta(workflow);
  return chip(meta.label, meta.tone);
}

function alertEvidence(alert = {}) {
  const classes = alert.classes || {};
  const wave = alert.wave || {};
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
    + Number(alert.common_recipients?.length || 0);
  return {
    hardWallets: Number(hardWallets || 0),
    supportWallets: Number(supportWallets || 0),
    hardSol: Number(hardSol || 0),
    supportSol: Number(supportSol || 0),
    commonLinks,
    waveNetSol: Number(wave.net_buy_sol || 0),
    waveUniqueBuyers: Number(wave.unique_buyers || 0),
    waveStickySupplyPct: Number(wave.sticky_supply_pct || 0),
    waveStickyWallets: Number(wave.sticky_wallets || 0),
    waveStickyBoughtPct: Number(wave.sticky_bought_pct || 0),
    waveNetRetentionPct: Number(wave.net_token_retention_pct || 0),
    waveTopBuyerShare: Number(wave.top_buyer_share || 0),
    waveTop3BuyerShare: Number(wave.top3_buyer_share || 0),
    waveBalanceCoveragePct: finiteNumber(wave.balance_coverage_pct),
  };
}

function isHotReactivationAlert(alert = {}) {
  if (effectiveAlertLane(alert) !== "reactivation") return false;
  if (alert.signal_family !== "reactivation_wave") return false;
  if (alert.data_quality?.status === "partial") return false;
  const snapshot = alertMarketSnapshot(alert);
  const mcap = Number(snapshot.mcapUsd || alert.obs_mcap_usd || alert.pool?.mcap_usd || 0);
  const evidence = alertEvidence(alert);
  return mcap > 0
    && mcap <= 250_000
    && Number(alert.score || 0) >= 75
    && evidence.waveNetSol >= 25
    && evidence.waveUniqueBuyers >= 15
    && evidence.waveStickyWallets >= 8
    && evidence.waveStickySupplyPct >= 5
    && evidence.waveStickyBoughtPct >= 40
    && evidence.waveNetRetentionPct >= 50
    && evidence.waveTopBuyerShare <= 0.35
    && evidence.waveTop3BuyerShare <= 0.6
    && (
      evidence.waveBalanceCoveragePct === null
      || evidence.waveBalanceCoveragePct >= 80
    );
}

function deriveAlertTier(alert = {}) {
  const lane = effectiveAlertLane(alert);
  const snapshot = alertMarketSnapshot(alert);
  const mcap = Number(snapshot.mcapUsd || alert.obs_mcap_usd || alert.pool?.mcap_usd || 0);
  const volumeToMcap = snapshot.volume1hUsd && mcap ? snapshot.volume1hUsd / mcap : null;
  const evidence = alertEvidence(alert);
  const waveSignal = evidence.waveNetSol >= 25 && evidence.waveStickySupplyPct >= 3;
  const hardSignal = evidence.hardWallets >= 2 || evidence.hardSol >= 15 || evidence.commonLinks > 0 || waveSignal;
  const supportOnly = evidence.supportWallets > 0 && !evidence.hardWallets && !evidence.commonLinks;
  if (supportOnly) return "noise";
  if (!hardSignal) return Number(alert.score || 0) >= 60 ? "watch" : "noise";
  if (isHotReactivationAlert(alert)) return "hot_reactivation";
  if (volumeToMcap !== null && volumeToMcap > 1.5 && evidence.commonLinks === 0) return "late_chase";
  if (lane === "reactivation") {
    const ratio = snapshot.athMcapUsd && mcap ? mcap / snapshot.athMcapUsd : null;
    if (ratio !== null && ratio > 0.4) return "late_chase";
    if (ratio !== null && ratio <= 0.25 && evidence.waveStickySupplyPct >= 5) return "actionable";
    if (ratio !== null && ratio <= 0.25 && evidence.commonLinks && evidence.hardWallets >= 2) return "actionable";
    return "watch";
  }
  return "noise";
}

function alertTier(alert = {}) {
  if (alert.action_tier === "late_chase" && isHotReactivationAlert(alert)) {
    return "hot_reactivation";
  }
  return TIER_META[alert.action_tier] ? alert.action_tier : deriveAlertTier(alert);
}

function bestTier(tiers = []) {
  return tiers.reduce((best, tier) => {
    if (!best || tierMeta(tier).rank > tierMeta(best).rank) return tier;
    return best;
  }, "noise");
}

function workflowMatches(token, includeEventsOnly = false) {
  if (token.workflowStatus === "events_only") {
    return includeEventsOnly && state.workflow === "all";
  }
  return state.workflow === "all" || token.workflowStatus === state.workflow;
}

function signalMatches(token) {
  if (state.signal === "all") return true;
  return token.currentSignalTier === state.signal;
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

function sourceAlerts() {
  const current = (state.report?.alerts || [])
    .filter((alert) => ACTIVE_SCANNER_LANES.has(alert.lane))
    .map((alert) => ({ ...alert, _scope_source: "current" }));
  const history = (state.history || [])
    .filter((alert) => ACTIVE_SCANNER_LANES.has(alert.lane))
    .filter((alert) => ["actionable", "hot_reactivation", "watch"].includes(alertTier(alert)))
    .map((alert) => ({ ...alert, _scope_source: "history" }));
  return [...history, ...current];
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

function marketMetaForToken(key) {
  if (!key) return null;
  return (state.market || {})[key] || null;
}

function mergeMarketMeta(pool = {}, meta = null) {
  if (!meta) return pool || {};
  const enriched = { ...(pool || {}) };
  [
    "ath_mcap_usd",
    "ath_mcap_at",
    "ath_price_usd",
    "ath_pool_address",
    "ath_source",
    "ath_status",
    "ath_error",
    "ath_error_checked_at",
    "ath_current_ratio",
    "ath_drawdown_pct",
    "ath_filter_checked_at",
    "latest_mcap_usd",
    "latest_price_usd",
    "latest_liquidity_usd",
    "latest_seen_at",
    "scan_mcap_usd",
    "scan_price_usd",
    "scan_liquidity_usd",
    "scan_mcap_at",
  ].forEach((key) => {
    if (meta[key] !== undefined && meta[key] !== null && meta[key] !== "") enriched[key] = meta[key];
  });
  if (!enriched.mcap_usd && meta.latest_mcap_usd) enriched.mcap_usd = meta.latest_mcap_usd;
  if (!enriched.price_usd && meta.latest_price_usd) enriched.price_usd = meta.latest_price_usd;
  if (!enriched.liquidity_usd && meta.latest_liquidity_usd) enriched.liquidity_usd = meta.latest_liquidity_usd;
  return enriched;
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
  return entries.length ? entries.map(([name, count]) => chip(`${CLASS_LABELS[name] || name} ${count}`)).join(" ") : "-";
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

function bestWaveAlert(alerts = []) {
  return alerts
    .filter((alert) => alert.wave)
    .sort((a, b) => {
      const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
      if (scoreDiff) return scoreDiff;
      return new Date(b.window_start || b.created_at || 0) - new Date(a.window_start || a.created_at || 0);
    })[0] || null;
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
  const signalTheses = (state.report?.signal_theses || [])
    .filter((thesis) => thesis && typeof thesis === "object");
  const thesesByKey = new Map();
  signalTheses.forEach((thesis) => {
    [thesis.token_address, thesis.pool_address].filter(Boolean).forEach((key) => {
      const existing = thesesByKey.get(key);
      const thesisTime = new Date(
        thesis.updated_at || thesis.last_checked_at || thesis.signal_at || 0,
      ).getTime();
      const existingTime = new Date(
        existing?.updated_at || existing?.last_checked_at || existing?.signal_at || 0,
      ).getTime();
      if (!existing || thesisTime >= existingTime) thesesByKey.set(key, thesis);
    });
  });
  const cleanScannedTokenKeys = new Set(
    (state.report?.summaries || [])
      .filter((summary) => !summary?.error && !summary?.scan_failed)
      .map((summary) => tokenKeyFromPool(summary.pool || {}))
      .filter(Boolean),
  );
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
  signalTheses.forEach((thesis) => {
    const key = thesis.token_address || thesis.pool_address;
    if (!key || groups.has(key)) return;
    const pool = {
      token_address: thesis.token_address || "",
      pool_address: thesis.pool_address || "",
      symbol: thesis.symbol || "",
      name: thesis.name || "",
      dex: thesis.dex || "",
      url: thesis.url || "",
      pair_created_at: thesis.pair_created_at || 0,
      mcap_usd: Number(thesis.signal_mcap_usd || 0),
      price_usd: Number(thesis.signal_price_usd || 0),
      liquidity_usd: Number(thesis.signal_liquidity_usd || 0),
    };
    groups.set(key, {
      key,
      symbol: pool.symbol || pool.name || "Unknown",
      name: pool.name || "",
      token_address: pool.token_address,
      pool_address: pool.pool_address,
      url: pool.url,
      alerts: [{
        lane: "reactivation",
        first_obs_lane: "reactivation",
        action_tier: thesis.source_tier || "watch",
        signal_family: thesis.signal_family,
        created_at: thesis.signal_at,
        window_start: thesis.signal_window_start || thesis.signal_at,
        window_end: thesis.signal_window_end || thesis.signal_at,
        obs_mcap_usd: Number(thesis.signal_mcap_usd || 0),
        score: Number(thesis.source_score || 0),
        suspicious_wallets: Number(
          thesis.source_wallets || thesis.original_wallets || 0,
        ),
        suspicious_sol: Number(thesis.source_flow_sol || 0),
        hard_wallets: Number(thesis.source_hard_wallets || 0),
        support_wallets: Number(thesis.source_support_wallets || 0),
        classes: {},
        events: [],
        pool,
        _scope_source: "thesis",
        _thesis_only: true,
      }],
    });
  });

  const tokens = [...groups.values()].map((token) => {
    token.alerts.sort((a, b) => (
      new Date(a.created_at || a.window_end || a.window_start)
      - new Date(b.created_at || b.window_end || b.window_start)
    ));
    token.alerts.forEach((alert) => {
      alert.baseFilterLane = baseAlertLane(alert);
      alert.filterLane = alert.baseFilterLane;
      alert.filterInferred = !INFERRABLE_FILTERS.includes(alert.lane);
    });
    const first = token.alerts[0];
    const last = token.alerts[token.alerts.length - 1];
    const marketMeta = marketMetaForToken(token.key);
    const latestPool = mergeMarketMeta(currentPools.get(token.key) || last.pool || first.pool || {}, marketMeta);
    token.signalThesis = thesesByKey.get(token.key)
      || thesesByKey.get(latestPool.pool_address)
      || thesesByKey.get(token.pool_address)
      || null;
    token.tokenIntel = [...token.alerts].reverse().find((alert) => alert.token_intel)?.token_intel || null;
    token.imageUrl = token.tokenIntel?.dex?.image || "";
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
    token.alerts.forEach((alert) => {
      const observedAt = alert.created_at || alert.window_end || alert.window_start || "";
      (alert.wave?.top_buyers || []).forEach((buyer) => {
        const owner = buyer.owner;
        if (!owner) return;
        const existing = walletMap.get(owner);
        if (existing?.aggregate_at && new Date(existing.aggregate_at) > new Date(observedAt)) return;
        walletMap.set(owner, {
          owner,
          signer_examples: existing?.signer_examples || new Set(),
          classes: {
            [buyer.wallet_class || "wave_buyer"]: 1,
          },
          buys: Number(buyer.buy_count || 0),
          sells: Number(buyer.sell_count || 0),
          sol_in: Number(buyer.buy_sol || 0),
          sol_out: Number(buyer.sell_sol || 0),
          tokens_bought: Number(buyer.token_bought || 0),
          tokens_sold: Number(buyer.token_sold || 0),
          retained_tokens: Number(buyer.retained_from_wave || 0),
          current_balance: Number(buyer.current_balance || 0),
          routed: 0,
          first_time: buyer.first_buy_time || alert.window_start,
          aggregate_at: observedAt,
          pnl_basis: "wave aggregate",
        });
      });
    });
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
          sells: 0,
          sol_in: 0,
          sol_out: 0,
          tokens_bought: 0,
          tokens_sold: 0,
          retained_tokens: 0,
          routed: 0,
          first_time: event.time || alert.window_start,
          pnl_basis: "buy events only",
        });
      }
      const row = walletMap.get(owner);
      if (event.signer) row.signer_examples.add(event.signer);
      row.classes[event.wallet_class || "unknown"] = (row.classes[event.wallet_class || "unknown"] || 0) + 1;
      if (row.aggregate_at) {
        row.routed += event.routed ? 1 : 0;
        return;
      }
      row.buys += 1;
      row.sol_in += Number(event.sol_amount || 0);
      row.tokens_bought += Number(event.token_amount || event.token_recipient_amount || 0);
      row.retained_tokens = row.tokens_bought;
      row.routed += event.routed ? 1 : 0;
      if (new Date(event.time || alert.window_start) < new Date(row.first_time)) row.first_time = event.time || alert.window_start;
    });
    const thesisCohortWallets = Array.isArray(token.signalThesis?.cohort_wallets)
      ? token.signalThesis.cohort_wallets
      : [];
    thesisCohortWallets.forEach((cohortWallet) => {
      const owner = String(cohortWallet?.owner || "").trim();
      if (!owner) return;
      if (!walletMap.has(owner)) {
        walletMap.set(owner, {
          owner,
          signer_examples: new Set(),
          classes: {},
          buys: 0,
          sells: 0,
          sol_in: 0,
          sol_out: 0,
          tokens_bought: 0,
          tokens_sold: 0,
          retained_tokens: null,
          routed: 0,
          first_time: cohortWallet.first_buy_time || token.signalThesis?.signal_at,
          pnl_basis: "thesis cohort",
        });
      }
      const row = walletMap.get(owner);
      const walletClass = cohortWallet.wallet_class || "signal_wallet";
      const holderMinPct = Math.max(
        0,
        Number(token.signalThesis?.holder_min_pct ?? 10),
      );
      if (!Object.keys(row.classes).length) row.classes[walletClass] = 1;
      const attributedTokens = Math.max(0, Number(cohortWallet.attributed_tokens || 0));
      const buySol = Math.max(0, Number(cohortWallet.buy_sol || 0));
      if (!row.tokens_bought && attributedTokens) row.tokens_bought = attributedTokens;
      if (!row.sol_in && buySol) row.sol_in = buySol;
      if (!row.buys && attributedTokens) row.buys = 1;
      row.thesis_attributed_tokens = attributedTokens || row.tokens_bought;
      row.retention_checked_at = cohortWallet.checked_at || null;
      const hasCheckedRetention = Boolean(
        row.retention_checked_at
        && cohortWallet.current_retained_tokens !== null
        && cohortWallet.current_retained_tokens !== undefined
        && Number.isFinite(Number(cohortWallet.current_retained_tokens)),
      );
      if (hasCheckedRetention) {
        const currentRetained = Math.max(0, Number(cohortWallet.current_retained_tokens));
        row.retained_tokens = Math.min(
          currentRetained,
          row.thesis_attributed_tokens || currentRetained,
        );
        row.current_balance = cohortWallet.current_balance === null
          || cohortWallet.current_balance === undefined
          ? null
          : Math.max(0, Number(cohortWallet.current_balance));
        const calculatedRetentionPct = row.thesis_attributed_tokens
          ? row.retained_tokens / row.thesis_attributed_tokens * 100
          : 0;
        row.is_signal_holder = typeof cohortWallet.is_holder === "boolean"
          ? cohortWallet.is_holder
          : calculatedRetentionPct >= holderMinPct;
        row.retention_unavailable = false;
        row.pnl_basis = "current balance check";
      } else {
        row.retained_tokens = null;
        row.is_signal_holder = null;
        row.retention_unavailable = true;
        row.pnl_basis = "balance check pending";
      }
    });
    if (token.signalThesis?.last_checked_at && !thesisCohortWallets.length) {
      walletMap.forEach((row) => {
        row.retained_tokens = null;
        row.retention_unavailable = true;
        row.pnl_basis = "per-wallet balance unavailable";
      });
    }
    const wallets = [...walletMap.values()].map((row) => {
      row.avg_entry_native = row.tokens_bought ? row.sol_in / row.tokens_bought : null;
      const retentionBasis = Number(row.thesis_attributed_tokens || row.tokens_bought || 0);
      const hasBalanceCheck = Boolean(
        row.retention_checked_at
        && row.retained_tokens !== null
        && row.retained_tokens !== undefined,
      );
      if (row.retention_unavailable) {
        row.realized_pnl_sol = null;
        row.current_value_sol = null;
        row.unrealized_pnl_sol = null;
        row.pnl_sol = null;
        row.pnl_pct = null;
        row.retained_pct = null;
      } else if (hasBalanceCheck) {
        row.retained_pct = retentionBasis
          ? row.retained_tokens / retentionBasis * 100
          : null;
        const isOpenHolder = row.is_signal_holder !== false
          && row.retained_tokens > 0;
        row.current_value_sol = currentNative !== null && currentNative !== undefined
          ? currentNative * row.retained_tokens
          : null;
        row.unrealized_pnl_sol = row.current_value_sol !== null && row.avg_entry_native
          ? row.current_value_sol - (row.avg_entry_native * row.retained_tokens)
          : null;
        row.realized_pnl_sol = null;
        row.pnl_sol = isOpenHolder ? row.unrealized_pnl_sol : null;
        row.pnl_pct = isOpenHolder && row.avg_entry_native && currentNative
          ? ((currentNative / row.avg_entry_native) - 1) * 100
          : null;
        row.pnl_basis = isOpenHolder
          ? "open position only"
          : row.retained_tokens > 0
            ? "dust below holder threshold"
            : "tokens left tracked wallet";
      } else {
        const soldCost = row.avg_entry_native
          ? Math.min(row.tokens_sold, row.tokens_bought) * row.avg_entry_native
          : 0;
        const retainedCost = row.avg_entry_native
          ? Math.min(row.retained_tokens, Math.max(0, row.tokens_bought - row.tokens_sold)) * row.avg_entry_native
          : 0;
        row.realized_pnl_sol = row.sol_out - soldCost;
        row.current_value_sol = currentNative && row.retained_tokens
          ? currentNative * row.retained_tokens
          : null;
        row.unrealized_pnl_sol = row.current_value_sol !== null
          ? row.current_value_sol - retainedCost
          : null;
        row.pnl_sol = row.unrealized_pnl_sol !== null
          ? row.realized_pnl_sol + row.unrealized_pnl_sol
          : null;
        row.pnl_pct = row.pnl_sol !== null && row.sol_in
          ? row.pnl_sol / row.sol_in * 100
          : null;
        row.retained_pct = row.tokens_bought
          ? row.retained_tokens / row.tokens_bought * 100
          : null;
      }
      row.class_label = Object.entries(row.classes).sort((a, b) => b[1] - a[1]).map(([name, count]) => `${CLASS_LABELS[name] || name} ${count}`).join(", ");
      row.signer_count = row.signer_examples.size;
      return row;
    }).sort((a, b) => {
      const holderRank = (wallet) => {
        if (wallet.is_signal_holder === true) return 3;
        if (!wallet.retention_checked_at) return 2;
        return Number(wallet.retained_tokens || 0) > 0 ? 1 : 0;
      };
      const rankDiff = holderRank(b) - holderRank(a);
      if (rankDiff) return rankDiff;
      const retentionDiff = Number(b.retained_pct || 0) - Number(a.retained_pct || 0);
      if (retentionDiff) return retentionDiff;
      return Number(b.sol_in || 0) - Number(a.sol_in || 0);
    });
    const walletPnls = wallets.map((row) => row.pnl_pct).filter((value) => Number.isFinite(value));
    token.latestPool = latestPool;
    token.firstSignalAt = first.created_at || first.window_end || first.window_start;
    token.lastSignalAt = last.created_at || last.window_end || last.window_start;
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
      || first.created_at
      || first.window_end
      || first.window_start
      || null;
    const createdAt = poolCreatedAt(latestPool) || poolCreatedAt(firstPool);
    const reportedAge = latestPool.age_hours ?? firstPool.age_hours;
    token.tokenAgeHours = reportedAge !== null && reportedAge !== undefined
      ? Number(reportedAge)
      : createdAt
        ? Math.max(0, (Date.now() - createdAt.getTime()) / 3_600_000)
        : null;
    token.tokenCreatedAt = createdAt ? createdAt.toISOString() : null;
    const trustedAth = ["gmgn", "solana_tracker", "ohlcv_high"].includes(latestPool.ath_source) && Number(latestPool.ath_mcap_usd || 0) > 0;
    token.athMcapUsd = trustedAth ? Number(latestPool.ath_mcap_usd || 0) : null;
    token.athMcapAt = trustedAth ? latestPool.ath_mcap_at || null : null;
    token.athSource = trustedAth ? latestPool.ath_source : "missing";
    token.athStatus = trustedAth ? latestPool.ath_status || "ready" : latestPool.ath_status || (latestPool.ath_error ? "error" : "pending");
    token.athError = latestPool.ath_error || "";
    token.athLabel = trustedAth ? `${athSourceLabel(latestPool.ath_source)} ATH` : "GMGN ATH";
    token.scanMcapUsd = Number(latestPool.scan_mcap_usd || latestPool.latest_mcap_usd || latestObservation?.mcap_usd || token.currentMcap || token.firstMcap || 0);
    token.scanMcapAt = latestPool.scan_mcap_at || latestPool.latest_seen_at || latestObservation?.at || token.lastSignalAt || null;
    token.liquidityUsd = Number(latestPool.liquidity_usd || 0);
    token.historicalMaxScore = Math.max(...token.alerts.map((alert) => Number(alert.score || 0)));
    token.caughtScore = Number(
      token.signalThesis?.source_score
      ?? first.score
      ?? 0,
    );
    token.uniqueEvents = uniqueEvents;
    token.rawEventCount = rawEvents;
    token.uniqueEventCount = uniqueEvents.length;
    token.duplicateEventCount = Math.max(0, rawEvents - uniqueEvents.length);
    token.hardEvents = uniqueEvents.filter((event) => HARD_WALLET_CLASSES.has(event.wallet_class));
    token.supportEvents = uniqueEvents.filter((event) => SUPPORT_WALLET_CLASSES.has(event.wallet_class));
    token.totalSuspiciousSol = sumAlertField(token.alerts, "suspicious_sol") || sumEventSol(uniqueEvents);
    token.hardFlowSol = sumAlertField(token.alerts, "hard_sol") || sumEventSol(token.hardEvents);
    token.supportFlowSol = sumAlertField(token.alerts, "support_sol") || sumEventSol(token.supportEvents);
    token.bestWaveAlert = bestWaveAlert(token.alerts);
    token.bestWave = token.bestWaveAlert?.wave || null;
    token.hasWaveSignal = Boolean(token.bestWave);
    token.hardSignalCount = sumAlertField(token.alerts, "hard_wallets") || token.hardEvents.length;
    token.supportSignalCount = sumAlertField(token.alerts, "support_wallets") || token.supportEvents.length;
    token.walletClassCounts = Object.keys(aggregateAlertClasses(token.alerts)).length ? aggregateAlertClasses(token.alerts) : eventClassCounts(uniqueEvents);
    token.routedBuyCount = uniqueEvents.filter((event) => event.routed).length;
    token.commonFunders = aggregateCommonEntries(token.alerts, "common_funders", ["source", "funder", "wallet"], "wallets");
    token.commonRecipients = aggregateCommonEntries(token.alerts, "common_recipients", ["recipient", "wallet"], "txs");
    token.commonExecutors = aggregateCommonEntries(token.alerts, "common_executors", ["executor", "wallet"], "wallets");
    token.alertCount = token.alerts.length;
    token.wallets = wallets;
    token.uniqueWallets = wallets.length
      || Number(token.signalThesis?.original_wallets || 0);
    token.bestWalletPnl = walletPnls.length ? Math.max(...walletPnls) : null;
    token.medianWalletPnl = median(walletPnls);
    const fitsReactivationNow = tokenFitsReactivationBucket(token);
    token.alerts.forEach((alert) => {
      const baseLane = alert.baseFilterLane || baseAlertLane(alert);
      if (baseLane === "reactivation") {
        alert.filterLane = "reactivation";
      } else if (baseLane === "legacy" && fitsReactivationNow) {
        alert.filterLane = "reactivation";
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
    token.filterCategories = [token.currentFilter];
    token.primaryFilter = token.currentFilter;
    token.lanes = token.filterCategories;
    token.reactivationStage = [...token.alerts]
      .reverse()
      .find((alert) => alert.reactivation_stage)?.reactivation_stage || null;
    token.hasObservedFilterDrift = token.observedFilters.some((name) => name !== token.caughtFilter);
    token.hasFilterDrift = token.currentFilter !== token.caughtFilter;
    token.currentScanAlerts = token.alerts.filter((alert) => alert._scope_source === "current");
    token.currentScanAlertCount = token.currentScanAlerts.length;
    token.scannedCleanThisRun = cleanScannedTokenKeys.has(token.key);
    token.latestUpdateAt = token.signalThesis?.last_checked_at
      || token.signalThesis?.updated_at
      || token.lastSignalAt;
    token.hasInferredFilters = token.alerts.some((alert) => alert.filterInferred || alert.filterLane !== alert.baseFilterLane);
    token.alertTiers = token.alerts.map(alertTier);
    token.historicalTier = bestTier(token.alertTiers);
    const reportAgeHours = reportFreshness(state.report?.generated_at).ageHours;
    const scannerFailed = state.scanStatus?.status === "failed";
    const scannerOperational = !scannerFailed && reportAgeHours !== null && reportAgeHours <= 2;
    token.currentSignalAlerts = scannerOperational ? token.currentScanAlerts : [];
    token.currentSignalTier = token.currentSignalAlerts.length
      ? bestTier(token.currentSignalAlerts.map(alertTier))
      : null;
    token.currentScore = token.currentSignalAlerts.length
      ? Math.max(...token.currentSignalAlerts.map((alert) => Number(alert.score || 0)))
      : null;
    const lastSignalAgeHours = token.lastSignalAt
      ? Math.max(0, (Date.now() - new Date(token.lastSignalAt).getTime()) / 3_600_000)
      : Number.POSITIVE_INFINITY;
    const thesisStatus = token.signalThesis?.status || "missing";
    const thesisNextCheckAt = token.signalThesis?.next_check_at || null;
    const thesisGraceMs = Number(
      state.report?.config?.signal_thesis_recheck_grace_minutes ?? 15,
    ) * 60_000;
    const thesisCheckDue = !token.signalThesis
      || thesisStatus === "unknown"
      || !thesisNextCheckAt
      || new Date(thesisNextCheckAt).getTime() + thesisGraceMs <= Date.now();
    token.lifecycleStatus = thesisStatus === "invalidated"
      ? "closed"
      : thesisStatus === "weakening"
        ? "weakening"
        : thesisStatus === "intact"
          ? "holding"
          : "pending";
    token.dataStatus = !scannerOperational
      ? "scanner_stale"
      : thesisCheckDue
        ? "check_needed"
        : "current";
    const currentWorkingSignal = ["actionable", "hot_reactivation", "watch"].includes(token.currentSignalTier);
    if (token.lifecycleStatus === "closed") {
      token.workflowStatus = "closed";
    } else if (token.lifecycleStatus === "weakening" || token.currentSignalTier === "late_chase") {
      token.workflowStatus = "needs_attention";
    } else if (currentWorkingSignal) {
      token.workflowStatus = "new_signals";
    } else if (token.dataStatus === "check_needed") {
      token.workflowStatus = "needs_attention";
    } else if (token.lifecycleStatus === "holding") {
      token.workflowStatus = "holding";
    } else {
      token.workflowStatus = token.currentSignalTier === "noise"
        ? "events_only"
        : "needs_attention";
    }
    token.signalLifecycle = {
      scannerOperational,
      currentConfirmed: token.currentSignalAlerts.length > 0,
      scannedCleanThisRun: token.scannedCleanThisRun,
      lastSignalAgeHours,
      historicalTier: token.historicalTier,
      currentSignalTier: token.currentSignalTier,
      workflowStatus: token.workflowStatus,
      thesisStatus,
      thesisCheckDue,
      thesisNextCheckAt,
      dataStatus: token.dataStatus,
    };
    token.currentQualityReasons = aggregateAlertLabels(token.currentSignalAlerts, "quality_reasons");
    token.currentQualityPenalties = aggregateAlertLabels(token.currentSignalAlerts, "quality_penalties");
    token.historicalQualityReasons = aggregateAlertLabels(token.alerts, "quality_reasons");
    token.historicalQualityPenalties = aggregateAlertLabels(token.alerts, "quality_penalties");
    const currentHeats = token.currentSignalAlerts.map(socialHeat);
    const disabledSocial = token.currentSignalAlerts
      .map((alert) => alert.social)
      .find((social) => social?.enabled === false);
    token.currentSocialHeat = currentHeats.includes("hot")
      ? "hot"
      : currentHeats.includes("warming")
        ? "warming"
        : currentHeats.includes("quiet")
          ? "quiet"
          : currentHeats.includes("disabled")
            ? "disabled"
            : currentHeats.includes("none")
              ? "none"
              : "unchecked";
    token.currentSocialReason = disabledSocial?.reason || "";
    token.narrative = choosePrimaryNarrative(token);
    token.narratives = [token.narrative.primary];
    token.hidden = isTokenHidden(token);
    return token;
  });

  return tokens.sort(compareFilterTokens);
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
  if (state.heat !== "all" && token.currentSocialHeat !== state.heat) return false;
  if (state.lane !== "all" && !token.filterCategories.includes(state.lane)) return false;
  if (state.minScore > 0 && Number(token.currentScore ?? -1) < state.minScore) return false;
  return true;
}

function filteredTokens(tokens) {
  return tokens.filter((token) => (
    tokenMatchesBaseFilters(token)
    && workflowMatches(token)
    && signalMatches(token)
  ));
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
    ? ` <span class="muted-inline">Enable Show deleted to review ${esc(state.hiddenTokenKeys.size)} locally deleted token${state.hiddenTokenKeys.size === 1 ? "" : "s"}.</span>`
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

function convexUrl() {
  const raw = String(window.SOLANA_RADAR_CONVEX_URL || "").trim();
  return raw.replace(/\/+$/, "");
}

async function fetchConvexQuery(path, args = {}) {
  const baseUrl = convexUrl();
  if (!baseUrl) return null;
  const response = await fetch(`${baseUrl}/api/query`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "accept": "application/json",
    },
    body: JSON.stringify({ path, args, format: "json" }),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || payload?.status !== "success") {
    const message = payload?.errorMessage || `${path} ${response.status}`;
    throw new Error(message);
  }
  return payload.value || {};
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
  const [report, deletedTokens, scannerStatus, discoveryStatus] = await Promise.all([
    fetchJson("data/latest_report.json"),
    fetchJson("data/deleted_tokens.json", true),
    fetchJson("data/scanner_status.json", true),
    fetchJson("data/discovery_status.json", true),
  ]);
  applyDeletedTokenList(deletedTokens || {});
  const extrasReady = state.staticExtrasLoadedFor === report.generated_at;
  return {
    report,
    history: extrasReady ? state.history : [],
    market: extrasReady ? state.market : {},
    scan_status: {
      ...(scannerStatus || {}),
      running: scannerStatus?.status === "running",
      source: "github_actions",
      static_mode: true,
      finished_at: scannerStatus?.last_attempt_at || report.generated_at,
      returncode: scannerStatus?.status === "failed" ? 1 : 0,
    },
    discovery_status: discoveryStatus || {},
  };
}

async function loadConvexData() {
  const payload = await fetchConvexQuery("radar:dashboardData", { historyLimit: 40 });
  if (!payload?.report?.generated_at) return null;
  return payload;
}

async function loadStaticExtras(reportGeneratedAt) {
  if (!reportGeneratedAt) return;
  if (state.staticExtrasLoadedFor === reportGeneratedAt) return;
  if (state.staticExtrasLoadingFor === reportGeneratedAt) return;
  state.staticExtrasLoadingFor = reportGeneratedAt;
  renderStatus();
  try {
    const [alertsText, scannerState] = await Promise.all([
      fetchText("data/alerts.jsonl", true),
      fetchJson("data/state.json", true),
    ]);
    if (state.report?.generated_at !== reportGeneratedAt) return;
    state.history = parseJsonl(alertsText);
    state.market = scannerState?.market || {};
    state.staticExtrasLoadedFor = reportGeneratedAt;
    const tokens = buildTokenSignals();
    const visibleTokens = filteredTokens(tokens);
    if (!state.selectedTokenKey && visibleTokens.length) state.selectedTokenKey = visibleTokens[0].key;
    if (!state.showHidden && isTokenHidden(state.selectedTokenKey)) {
      state.selectedTokenKey = visibleTokens[0]?.key || null;
    }
    render();
  } finally {
    if (state.staticExtrasLoadingFor === reportGeneratedAt) {
      state.staticExtrasLoadingFor = null;
      renderStatus();
    }
  }
}

async function loadData() {
  let payload;
  state.staticMode = false;
  const staticHost = window.location.hostname.endsWith("github.io")
    || window.location.protocol === "file:"
    || ["127.0.0.1", "localhost"].includes(window.location.hostname);
  if (convexUrl()) {
    try {
      payload = await loadConvexData();
      state.staticMode = false;
    } catch (error) {
      console.warn("Convex load failed, falling back to static data", error);
      payload = null;
    }
  }
  if (!payload && staticHost) {
    payload = await loadStaticData();
    state.staticMode = true;
  } else if (!payload) {
    try {
      payload = await fetchJson("/api/report");
    } catch {
      payload = await loadStaticData();
      state.staticMode = true;
    }
  }
  state.report = payload.report || {};
  applyDeletedTokenList(payload.deleted_tokens || {});
  if (state.staticMode) {
    if (state.staticExtrasLoadedFor !== state.report.generated_at) {
      state.history = payload.history || [];
      state.market = payload.market || {};
    }
  } else {
    state.history = payload.history || [];
    state.market = payload.market || {};
  }
  state.scanStatus = payload.scan_status || {};
  state.discoveryStatus = payload.discovery_status || {};
  const tokens = buildTokenSignals();
  const visibleTokens = filteredTokens(tokens);
  if (!state.selectedTokenKey && visibleTokens.length) state.selectedTokenKey = visibleTokens[0].key;
  if (!state.showHidden && isTokenHidden(state.selectedTokenKey)) {
    state.selectedTokenKey = visibleTokens[0]?.key || null;
  }
  render();
  if (state.staticMode) {
    loadStaticExtras(state.report.generated_at).catch((error) => {
      els.statusRow.innerHTML += `<span class="status-pill freshness-bad">history load failed: ${esc(error.message)}</span>`;
    });
  }
}

function renderStatus() {
  const report = state.report || {};
  const status = state.scanStatus || {};
  const discoveryStatus = state.discoveryStatus || {};
  const running = Boolean(status.running);
  const failed = status.status === "failed";
  const freshness = reportFreshness(report.generated_at);
  const failedHealth = failed && status.scan_health && Object.keys(status.scan_health).length
    ? status.scan_health
    : null;
  const scanHealth = failedHealth || report.stats?.scan_health || {};
  const healthStatus = scanHealth.status || "unknown";
  const healthTone = healthStatus === "healthy" ? "good" : healthStatus === "degraded" ? "warn" : "bad";
  const healthReason = (scanHealth.reasons || []).join("; ")
    || (failed ? status.error : "")
    || "No scanner health diagnostics in this report";
  const athProvider = report.stats?.gmgn_ath || report.stats?.solana_tracker_ath || {};
  const rpcProviders = scanHealth.rpc_providers || report.stats?.rpc_providers || {};
  const rpcProviderEntries = ["chainstack", "alchemy", "helius"]
    .filter((name) => rpcProviders[name])
    .map((name) => [name, rpcProviders[name]]);
  const rpcLabels = { chainstack: "Chainstack", alchemy: "Alchemy", helius: "Helius" };
  const activeRpcProviders = rpcProviderEntries
    .filter(([, provider]) => ["active", "ready"].includes(provider.status))
    .map(([name]) => rpcLabels[name] || name);
  const blockedRpcProviders = rpcProviderEntries
    .filter(([, provider]) => provider.status === "blocked")
    .map(([name]) => rpcLabels[name] || name);
  const rpcTitle = rpcProviderEntries.map(([name, provider]) => {
    const calls = Object.values(provider.calls || {}).reduce((sum, value) => sum + Number(value || 0), 0);
    return `${rpcLabels[name] || name}: ${provider.status || "unknown"}, ${calls} calls`;
  }).join("; ");
  if (els.showHiddenInput) els.showHiddenInput.checked = state.showHidden;
  if (els.workflowFilter) els.workflowFilter.value = state.workflow;
  const signalRelevant = state.workflow === "new_signals" || state.workflow === "all";
  if (els.signalFilter) {
    els.signalFilter.value = state.signal;
    els.signalFilter.disabled = !signalRelevant;
  }
  if (els.heatFilter) els.heatFilter.disabled = !signalRelevant;
  if (els.scoreInput) els.scoreInput.disabled = !signalRelevant;
  document.querySelectorAll(".current-signal-control").forEach((control) => {
    control.classList.toggle("is-disabled", !signalRelevant);
  });
  const reportLanes = (report.lanes_scanned || []).filter((name) => FILTER_ORDER.includes(name) && name !== "legacy");
  const laneText = status.lane || status.mode || (reportLanes.length > 1 ? `${reportLanes.length} lanes` : reportLanes[0]) || report.mode || "-";
  const discoveryAt = discoveryStatus.last_success_at
    || discoveryStatus.last_attempt_at
    || discoveryStatus.updatedAt;
  const discoveryFreshness = discoveryAt
    ? reportFreshness(discoveryAt)
    : null;
  const discoveryFailed = discoveryStatus.status === "failed";
  els.subtitle.textContent = report.generated_at
    ? `Latest successful scan ${dateLabel(report.generated_at)} - ${report.profile || report.lane || report.mode || "unknown"}`
    : "No scan report yet";
  els.runScan.disabled = running || state.staticMode;
  els.runScan.title = state.staticMode ? "Scanner runs by GitHub Actions schedule" : "";
  els.statusRow.innerHTML = [
    `<span class="status-pill"><span class="dot ${running ? "warn" : ""}"></span>${running ? "scan running" : "idle"}</span>`,
    failed ? `<span class="status-pill freshness-bad" title="${esc(status.error || "Scanner failed")}"><span class="dot bad"></span>last attempt failed ${esc(dateLabel(status.last_attempt_at))}</span>` : "",
    `<span class="status-pill freshness-${freshness.tone}"><span class="dot ${freshness.tone === "good" ? "" : freshness.tone}"></span>${esc(freshness.label)}</span>`,
    `<span class="status-pill freshness-${healthTone}" title="${esc(healthReason)}"><span class="dot ${healthTone === "good" ? "" : healthTone}"></span>scan ${esc(healthStatus)}</span>`,
    discoveryFailed
      ? `<span class="status-pill freshness-bad" title="${esc(discoveryStatus.error || "Discovery pulse failed")}"><span class="dot bad"></span>discovery failed</span>`
      : discoveryFreshness
        ? `<span class="status-pill freshness-${discoveryFreshness.tone}" title="${esc(dateLabel(discoveryAt))}">discovery ${esc(discoveryFreshness.label)}</span>`
        : "",
    activeRpcProviders.length ? `<span class="status-pill" title="${esc(rpcTitle)}">RPC ${esc(activeRpcProviders.join(" + "))}</span>` : "",
    blockedRpcProviders.length ? `<span class="status-pill freshness-warn" title="${esc(rpcTitle)}">${esc(blockedRpcProviders.join(" + "))} blocked</span>` : "",
    athProvider.status && athProvider.status !== "ok" ? `<span class="status-pill freshness-bad" title="${esc(athProvider.error || "GMGN unavailable")}">ATH source ${esc(athProvider.status)}</span>` : "",
    `<span class="status-pill">lane ${esc(laneText)}</span>`,
    state.staticMode && state.hiddenTokenKeys.size ? `<button class="status-action" id="syncDeleted" type="button">Sync deleted</button>` : "",
    state.staticExtrasLoadingFor === report.generated_at ? `<span class="status-pill freshness-warn">loading history</span>` : "",
    status.next_scan_at ? `<span class="status-pill">next auto ${esc(dateLabel(status.next_scan_at))}</span>` : "",
  ].filter(Boolean).join("");
  document.querySelector("#syncDeleted")?.addEventListener("click", () => {
    syncLocalDeletedTokens().catch((error) => {
      els.statusRow.innerHTML += `<span class="status-pill freshness-bad">delete sync failed: ${esc(error.message)}</span>`;
    });
  });
}

function renderMetrics(tokens) {
  const report = state.report || {};
  const stats = report.stats || {};
  const baseTokens = buildTokenSignals().filter(tokenMatchesBaseFilters);
  const workflowCount = (workflow) => baseTokens.filter((token) => token.workflowStatus === workflow).length;
  const outcomes = stats.signal_outcomes || {};
  const outcomeSamples = Number(outcomes.with_24h || 0);
  const outcomeMedian = outcomes.median_return_24h_pct;
  const outcomePositive = outcomes.positive_24h_pct;
  els.metrics.innerHTML = [
    metric("New signals", workflowCount("new_signals")),
    metric("Holding", workflowCount("holding")),
    metric("Needs attention", workflowCount("needs_attention")),
    metric("Closed", workflowCount("closed")),
    metric("Universe", stats.universe_pools ?? 0),
    metric("Scanned pools", stats.scanned_pools ?? 0),
    metric(
      "24h median",
      outcomeSamples && outcomeMedian !== null && outcomeMedian !== undefined
        ? pct(outcomeMedian)
        : "warming",
    ),
    metric(
      "24h positive",
      outcomeSamples && outcomePositive !== null && outcomePositive !== undefined
        ? `${Number(outcomePositive).toFixed(0)}% / ${outcomeSamples}`
        : "warming",
    ),
    metric("Data age", reportFreshness(report.generated_at).label),
  ].join("");
}

function currentSignalChip(token) {
  if (!token.currentSignalTier || token.currentSignalTier === "noise") return "";
  return tierChip(token.currentSignalTier);
}

function primaryStatusChip(token, compact = false) {
  if (token.workflowStatus === "new_signals" && token.currentSignalTier) {
    return tierChip(token.currentSignalTier);
  }
  if (compact && token.workflowStatus === "needs_attention") {
    return chip("Review", "warn");
  }
  return workflowChip(token.workflowStatus);
}

function attentionReason(token) {
  if (token.currentSignalTier === "late_chase") return "late entry";
  if (token.lifecycleStatus === "weakening") return "cohort reduced";
  if (token.dataStatus === "check_needed") return "wallet check needed";
  if (token.dataStatus === "scanner_stale") return "scanner data is stale";
  if (token.lifecycleStatus === "closed") return "accumulation closed";
  return "";
}

function positiveSocialChip(token) {
  if (!["warming", "hot"].includes(token.currentSocialHeat)) return "";
  return chip(socialLabel(token.currentSocialHeat, token.currentSocialReason), "good");
}

function renderTokenRow(token) {
  const selected = token.key === state.selectedTokenKey ? " is-selected" : "";
  const hidden = token.hidden ? " is-hidden" : "";
  const athText = token.athMcapUsd
    ? `${token.athLabel} ${money(token.athMcapUsd)}`
    : `${token.athLabel} ${athStatusLabel(token.athStatus)}`;
  const gmgnUrl = gmgnTokenUrl(token);
  return `
    <article class="token-row${selected}${hidden}" data-token-key="${esc(token.key)}">
      <div class="token-main">
        <div class="token-row-heading">
          ${tokenAvatar(token, true)}
          <div>
            <div class="symbol-line">
              <span class="symbol">${esc(token.symbol)}</span>
              <span class="muted">${esc(token.name)}</span>
              ${token.hidden ? chip("deleted", "warn") : ""}
            </div>
            <div class="meta">
              <span>caught ${esc(dateLabel(token.firstSignalAt))}</span>
              <span>${esc(durationLabel(token.tokenAgeHours))} old</span>
              <span>${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)} caught mcap</span>
              <span>${esc(athText)}</span>
              <span>${money(token.liquidityUsd)} liq</span>
              <span>${token.uniqueWallets} wallets</span>
            </div>
          </div>
        </div>
        <div class="chips">
          ${workflowChip(token.workflowStatus)}
          ${currentSignalChip(token)}
          ${chip(`${token.narrative.primary} - ${token.narrative.tilt}`, narrativeTone(token.narrative))}
          ${token.narrative.secondary.slice(0, 1).map((name) => chip(`${name} flavor`)).join("")}
          ${token.hasFilterDrift ? chip(`caught ${filterMeta(token.caughtFilter).label}`, "warn") : ""}
          ${positiveSocialChip(token)}
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
        <small>wallet PnL est.</small>
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
      const hidden = button.dataset.hidden !== "true";
      const token = buildTokenSignals().find((item) => item.key === key);
      setTokenHidden(key, hidden);
      persistTokenDeletion(token, hidden)
        .then(() => render())
        .catch((error) => {
          els.statusRow.innerHTML += `<span class="status-pill freshness-bad">delete sync failed: ${esc(error.message)}</span>`;
        });
      render();
    });
  });
}

function walletHeldLabel(wallet) {
  if (wallet.retained_pct === null || wallet.retained_pct === undefined) {
    return wallet.retention_unavailable ? "not available" : "pending";
  }
  const retainedPct = Math.max(0, Number(wallet.retained_pct));
  if (!retainedPct) return "0% retained";
  const formatted = retainedPct < 0.1
    ? "<0.1"
    : retainedPct < 1
      ? retainedPct.toFixed(1)
      : retainedPct.toFixed(0);
  return wallet.is_signal_holder === false
    ? `${formatted}% dust`
    : `${formatted}% held`;
}

function walletHeldClass(wallet) {
  if (wallet.retained_pct === null || wallet.retained_pct === undefined) return "";
  return wallet.is_signal_holder === true ? "good" : "bad";
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
            <th>Held</th>
            <th>Open return</th>
            <th>Open PnL</th>
          </tr>
        </thead>
        <tbody>
          ${token.wallets.slice(0, 16).map((wallet) => `
            <tr>
              <td><code>${esc(short(wallet.owner))}</code></td>
              <td>${esc(wallet.class_label || "-")}${wallet.routed ? ` ${chip(`routed ${wallet.routed}`, "warn")}` : ""}</td>
              <td>${esc(wallet.buys)}</td>
              <td>${sol(wallet.sol_in)}</td>
              <td class="${walletHeldClass(wallet)}" title="${esc(wallet.retention_checked_at ? `balance checked ${dateLabel(wallet.retention_checked_at)}` : wallet.pnl_basis)}">${esc(walletHeldLabel(wallet))}</td>
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
  return basisChips;
}

function renderFilterLine(token) {
  const caught = token.caughtFilter || token.primaryFilter || "legacy";
  const current = token.currentFilter || caught;
  const chips = current !== caught
    ? chip("historical lane remapped", "warn")
    : "";
  const inferred = token.hasInferredFilters ? ` ${chip("inferred from snapshot", "warn")}` : "";
  const primary = filterMeta(current);
  return `${chips}${inferred} <span class="muted-inline">${esc(primary.criteria)}</span>`;
}

function signalRankScore(token) {
  return Number(token.currentScore || 0) * 10
    + Number(token.totalSuspiciousSol || 0)
    + Math.max(0, Number(token.profitPct || 0)) / 10;
}

function tokenFreshnessMs(token) {
  const latestScanMs = token.currentScanAlerts.reduce((best, alert) => {
    const value = new Date(alert.window_start || alert.created_at || 0).getTime();
    return Number.isNaN(value) ? best : Math.max(best, value);
  }, 0);
  const lastSignalMs = new Date(token.lastSignalAt || 0).getTime();
  const firstSignalMs = new Date(token.firstSignalAt || 0).getTime();
  return latestScanMs || (Number.isNaN(lastSignalMs) ? 0 : lastSignalMs) || (Number.isNaN(firstSignalMs) ? 0 : firstSignalMs);
}

function compareFilterTokens(a, b) {
  const workflowDiff = workflowMeta(b.workflowStatus).rank - workflowMeta(a.workflowStatus).rank;
  if (workflowDiff) return workflowDiff;

  if (a.workflowStatus === "new_signals" && b.workflowStatus === "new_signals") {
    const tierDiff = tierMeta(b.currentSignalTier).rank - tierMeta(a.currentSignalTier).rank;
    if (tierDiff) return tierDiff;
    const scoreDiff = Number(b.currentScore || 0) - Number(a.currentScore || 0);
    if (scoreDiff) return scoreDiff;
  }

  if (a.workflowStatus === "needs_attention" && b.workflowStatus === "needs_attention") {
    const attentionPriority = (token) => {
      if (token.currentSignalTier === "late_chase") return 3;
      if (token.lifecycleStatus === "weakening") return 2;
      if (token.dataStatus === "check_needed") return 1;
      return 0;
    };
    const attentionDiff = attentionPriority(b) - attentionPriority(a);
    if (attentionDiff) return attentionDiff;
  }

  if (a.workflowStatus === "holding" && b.workflowStatus === "holding") {
    const heldDiff = Number(heldSupplyMetric(b).pct || 0) - Number(heldSupplyMetric(a).pct || 0);
    if (heldDiff) return heldDiff;
  }

  const freshnessDiff = tokenFreshnessMs(b) - tokenFreshnessMs(a);
  if (freshnessDiff) return freshnessDiff;

  const caughtDiff = new Date(b.firstSignalAt || 0).getTime() - new Date(a.firstSignalAt || 0).getTime();
  if (caughtDiff) return caughtDiff;

  return signalRankScore(b) - signalRankScore(a);
}

function renderSignalQuality(token) {
  const duplicateChip = token.duplicateEventCount
    ? ` ${chip(`${token.duplicateEventCount} duplicate rows collapsed`, "warn")}`
    : "";
  const currentWave = bestWaveAlert(token.currentSignalAlerts || [])?.wave;
  if (token.currentSignalTier && currentWave) {
    return [
      `current score ${esc(token.currentScore)}`,
      `wave net ${sol(currentWave.net_buy_sol)}`,
      `${esc(currentWave.unique_buyers || 0)} buyers`,
      `${(Number(currentWave.sticky_supply_pct || 0)).toFixed(1)}% held supply`,
    ].join(" / ") + duplicateChip;
  }
  if (token.currentSignalTier) {
    const currentAlerts = token.currentSignalAlerts || [];
    return [
      `current score ${esc(token.currentScore)}`,
      `${esc(sumAlertField(currentAlerts, "hard_wallets"))} hard wallets`,
      `${esc(sumAlertField(currentAlerts, "support_wallets"))} support wallets`,
    ].join(" / ") + duplicateChip;
  }
  return [
    `caught score ${esc(token.caughtScore)}`,
    "no fresh signal in the latest scan",
  ].join(" / ") + duplicateChip;
}

function renderSignalTier(token) {
  if (!token.currentSignalTier) {
    return `<span class="muted-inline">No fresh signal in the latest successful scan.</span>`;
  }
  const reasons = token.currentQualityReasons.length
    ? token.currentQualityReasons.slice(0, 4).map((item) => chip(item)).join(" ")
    : `<span class="muted-inline">${esc(tierMeta(token.currentSignalTier).summary)}</span>`;
  const penalties = token.currentQualityPenalties.length
    ? ` ${token.currentQualityPenalties.slice(0, 4).map((item) => chip(item, item.includes("late") || item.includes("blowoff") || item.includes("only") ? "bad" : "warn")).join(" ")}`
    : "";
  return `${tierChip(token.currentSignalTier)} ${reasons}${penalties}`;
}

function thesisTier(thesis) {
  if (!thesis) return "recheck_due";
  if (thesis.status === "intact") return "holding";
  if (thesis.status === "weakening") return "weakening";
  if (thesis.status === "invalidated") return "inactive";
  return "recheck_due";
}

function originalSignalTier(token) {
  const sourceTier = token.signalThesis?.source_tier;
  if (sourceTier && TIER_META[sourceTier]) return sourceTier;
  const originalAlert = token.alerts?.[0];
  return originalAlert ? alertTier(originalAlert) : "watch";
}

function renderCaughtSignal(token) {
  const thesis = token.signalThesis;
  const originalAlert = token.alerts?.[0] || {};
  const tier = originalSignalTier(token);
  const caughtAt = thesis?.signal_at
    || originalAlert.created_at
    || originalAlert.window_end
    || originalAlert.window_start
    || token.firstSignalAt;
  const mcap = Number(
    thesis?.signal_mcap_usd
    || originalAlert.obs_mcap_usd
    || originalAlert.pool?.mcap_usd
    || token.firstObsMcapUsd
    || 0,
  );
  const score = Number(
    thesis?.source_score
    ?? originalAlert.score
    ?? Number.NaN,
  );
  const flow = Number(
    thesis?.source_flow_sol
    ?? originalAlert.suspicious_sol
    ?? Number.NaN,
  );
  const wallets = Number(
    thesis?.source_wallets
    ?? originalAlert.suspicious_wallets
    ?? Number.NaN,
  );
  const metrics = [
    caughtAt ? dateLabel(caughtAt) : "",
    mcap > 0 ? `${money(mcap)} mcap` : "",
    Number.isFinite(score) ? `score ${score.toFixed(0)}` : "",
    Number.isFinite(flow) ? `${sol(flow)} flow` : "",
    Number.isFinite(wallets) ? `${wallets.toFixed(0)} wallets` : "",
  ].filter(Boolean).map(esc).join(" / ");
  return [
    tierChip(tier),
    metrics,
    `<span class="muted-inline">${esc(tierMeta(tier).summary)}</span>`,
  ].filter(Boolean).join(" ");
}

function renderThesisSummary(token) {
  const thesis = token.signalThesis;
  if (!thesis) {
    return `${tierChip("recheck_due")} <span class="muted-inline">original buyer cohort has not been persisted yet</span>`;
  }
  const retention = thesis.token_retention_pct === null
    || thesis.token_retention_pct === undefined
    ? null
    : Number(thesis.token_retention_pct);
  const holders = Number(thesis.holders_remaining || 0);
  const originalWallets = Number(thesis.original_wallets || 0);
  const checked = thesis.last_checked_at
    ? `checked ${dateLabel(thesis.last_checked_at)}`
    : "not checked";
  return [
    tierChip(thesisTier(thesis)),
    Number.isFinite(retention) ? `${retention.toFixed(0)}% of signal tokens retained` : "",
    originalWallets ? `${holders}/${originalWallets} tracked wallets still holding` : "",
    checked,
  ].filter(Boolean).join(" / ");
}

function renderThesisDetails(token) {
  const thesis = token.signalThesis;
  if (!thesis) {
    return `<div class="kv"><span>Original cohort</span><span>${renderThesisSummary(token)}</span></div>`;
  }
  const retainedSupply = thesis.current_retained_supply_pct === null
    || thesis.current_retained_supply_pct === undefined
    ? null
    : Number(thesis.current_retained_supply_pct);
  const walletCoverage = thesis.balance_coverage_pct === null
    || thesis.balance_coverage_pct === undefined
    ? null
    : Number(thesis.balance_coverage_pct);
  const tokenCoverage = thesis.token_balance_coverage_pct === null
    || thesis.token_balance_coverage_pct === undefined
    ? null
    : Number(thesis.token_balance_coverage_pct);
  const cohortWalletCoverage = thesis.cohort_wallet_coverage_pct === null
    || thesis.cohort_wallet_coverage_pct === undefined
    ? null
    : Number(thesis.cohort_wallet_coverage_pct);
  const cohortTokenCoverage = thesis.cohort_token_coverage_pct === null
    || thesis.cohort_token_coverage_pct === undefined
    ? null
    : Number(thesis.cohort_token_coverage_pct);
  const coverage = [
    Number.isFinite(walletCoverage) ? `${walletCoverage.toFixed(0)}% wallets` : "",
    Number.isFinite(tokenCoverage) ? `${tokenCoverage.toFixed(0)}% tracked tokens` : "",
  ].filter(Boolean).join(" / ");
  const cohortCoverage = [
    Number.isFinite(cohortWalletCoverage) ? `${cohortWalletCoverage.toFixed(0)}% signal wallets` : "",
    Number.isFinite(cohortTokenCoverage) ? `${cohortTokenCoverage.toFixed(0)}% signal tokens` : "",
  ].filter(Boolean).join(" / ");
  return `
    <div class="kv"><span>Original cohort</span><span>${renderThesisSummary(token)}</span></div>
    <div class="kv"><span>Retained supply</span><span>${Number.isFinite(retainedSupply) ? `${retainedSupply.toFixed(2)}%` : "-"}${thesis.reason ? ` <span class="muted-inline">${esc(thesis.reason)}</span>` : ""}</span></div>
    <div class="kv"><span>Verification</span><span>${esc(coverage || "balance coverage unavailable")}${cohortCoverage ? ` / cohort ${esc(cohortCoverage)}` : ""}${thesis.next_check_at ? ` / next ${esc(dateLabel(thesis.next_check_at))}` : ""}</span></div>
  `;
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
  token.commonExecutors.forEach((item) => {
    parts.push(chip(`common executor ${short(item.key)} -> ${item.count} wallets`, "warn"));
  });
  if (token.routedBuyCount) parts.push(chip(`routed buys ${token.routedBuyCount}`, "warn"));
  return parts.length ? parts.join(" ") : "-";
}

function renderWalletSummary(token) {
  const thesis = token.signalThesis;
  const retention = thesis && Number.isFinite(Number(thesis.holders_remaining))
    ? `${Number(thesis.holders_remaining)}/${Number(thesis.original_wallets || token.uniqueWallets)} still holding`
    : `${token.uniqueWallets} tracked wallets`;
  const pnl = token.medianWalletPnl === null
    ? "open return unavailable"
    : `open median ${pct(token.medianWalletPnl)} / best ${pct(token.bestWalletPnl)}`;
  return `${esc(retention)} / ${esc(pnl)}`;
}

function detailMetric(label, value, sub = "", valueClass = "", subClass = "") {
  return `
    <div class="detail-metric">
      <span>${esc(label)}</span>
      <strong class="${esc(valueClass)}">${esc(value)}</strong>
      ${sub ? `<small class="${esc(subClass)}">${esc(sub)}</small>` : ""}
    </div>
  `;
}

function renderTopWalletPreview(token) {
  if (!token.wallets.length) return `<div class="detail-empty-line">No noticed wallet events.</div>`;
  return `
    <div class="wallet-preview">
      ${token.wallets.slice(0, 3).map((wallet) => `
        <div class="wallet-preview-row">
          <code>${esc(short(wallet.owner))}</code>
          <span>${esc(wallet.class_label || "-")}</span>
          <strong>${sol(wallet.sol_in)}</strong>
          <strong class="${walletHeldClass(wallet)}">${esc(walletHeldLabel(wallet))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function renderScannerReason(token) {
  const parts = [];
  if (token.alerts.every((alert) => alert._thesis_only)) {
    const thesis = token.signalThesis || {};
    const wallets = Number(thesis.source_wallets || thesis.original_wallets || 0);
    const flow = Number(thesis.source_flow_sol || 0);
    const mcap = Number(thesis.signal_mcap_usd || 0);
    const source = thesis.signal_family === "reactivation_wave"
      ? "formed a retained market-wide buy wave"
      : "formed a classified-wallet accumulation cluster";
    return [
      wallets ? `${wallets} buyers ${source}` : `Buyers ${source}`,
      flow ? `${sol(flow)} net flow` : "",
      mcap ? `at ${money(mcap)} mcap` : "",
      Number.isFinite(Number(thesis.source_score)) ? `source score ${Number(thesis.source_score).toFixed(0)}` : "",
    ].filter(Boolean).join(" / ");
  }
  if (token.bestWave) {
    parts.push(
      `market-wide wave: ${sol(token.bestWave.buy_sol)} buys vs ${sol(token.bestWave.sell_sol)} sells, ${sol(token.bestWave.net_buy_sol)} net, ${Number(token.bestWave.sticky_supply_pct || 0).toFixed(1)}% supply still on checked buyers`
    );
  }
  if (token.hardSignalCount || token.hardFlowSol) {
    parts.push(`${token.hardSignalCount} hard wallets bought ${sol(token.hardFlowSol)}`);
  }
  if (token.supportSignalCount || token.supportFlowSol) {
    parts.push(`${token.supportSignalCount} support wallets bought ${sol(token.supportFlowSol)}`);
  }
  const linkCount = token.commonFunders.length
    + token.commonRecipients.length
    + token.commonExecutors.length
    + Number(token.routedBuyCount || 0);
  if (linkCount) parts.push(`${linkCount} wallet-link flag${linkCount === 1 ? "" : "s"}`);
  const freshUpdates = token.currentSignalAlerts.length;
  if (freshUpdates) parts.push(`${freshUpdates} update${freshUpdates === 1 ? "" : "s"} in latest scan`);
  if (!parts.length) return "Scanner caught this from score-based evidence, but hard wallet evidence is thin.";
  return `${parts.join("; ")}.`;
}

function renderRiskFlags(token) {
  const flags = [];
  if (token.currentSignalTier === "late_chase") flags.push(chip("late entry", "bad"));
  if (token.currentSignalTier === "noise") flags.push(chip("low-confidence event", "bad"));
  if (token.lifecycleStatus === "weakening") flags.push(chip("original buyers are reducing holdings", "warn"));
  if (token.dataStatus === "check_needed") flags.push(chip("wallet balances need refresh", "warn"));
  if (token.lifecycleStatus === "closed") flags.push(chip("original accumulation closed", "bad"));
  if (!token.hardSignalCount && !token.hasWaveSignal) flags.push(chip("no hard wallets", "warn"));
  if (!token.athMcapUsd) flags.push(chip(athStatusLabel(token.athStatus, token.athError), "warn"));
  token.currentQualityPenalties.slice(0, 4).forEach((item) => {
    flags.push(chip(item, item.includes("late") || item.includes("blowoff") || item.includes("only") ? "bad" : "warn"));
  });
  if (token.hasFilterDrift) flags.push(chip(`moved from ${filterMeta(token.caughtFilter).label}`, "warn"));
  if (!flags.length) return chip("no major risk flags", "good");
  const visible = flags.slice(0, 7).join(" ");
  const hiddenCount = flags.length - 7;
  return `${visible}${hiddenCount > 0 ? ` ${chip(`+${hiddenCount} more`, "warn")}` : ""}`;
}

function renderDetailSection(title, summary, body, open = false) {
  return `
    <details class="detail-section" ${open ? "open" : ""}>
      <summary>
        <span>${esc(title)}</span>
        ${summary ? `<small>${esc(summary)}</small>` : ""}
      </summary>
      <div class="detail-section-body">${body}</div>
    </details>
  `;
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
    ? "above preferred reactivation zone"
    : "";
  return `
    <div class="kv">
      <span>Market phase</span>
      <span>${chip(phase.label, phase.tone)} ${esc(phase.detail)}${caution ? ` <span class="muted-inline">${esc(caution)}</span>` : ""}</span>
    </div>
  `;
}

function renderMarketPhaseLine(token) {
  const phase = marketPhase(token);
  if (!phase) return "-";
  const reactivationHighRange = token.filterCategories.includes("reactivation")
    && (phase.label === "Upper range" || phase.label === "Near ATH");
  const caution = reactivationHighRange ? "above preferred reactivation zone" : "";
  return `${chip(phase.label, phase.tone)} ${esc(phase.detail)}${caution ? ` <span class="muted-inline">${esc(caution)}</span>` : ""}`;
}

function renderWaveLine(token) {
  const wave = token.bestWave;
  if (!wave) return "";
  return `
    <div class="kv">
      <span>Buy wave</span>
      <span>
        ${sol(wave.buy_sol)} buys / ${sol(wave.sell_sol)} sells / ${sol(wave.net_buy_sol)} net;
        ${esc(wave.effective_unique_buyers || wave.unique_buyers || 0)} effective buyers
        (${esc(wave.unique_buyers || 0)} addresses), ${esc(wave.large_buyers || 0)} large;
        ${Number(wave.sticky_supply_pct || 0).toFixed(1)}% supply sticky
        ${Number(wave.max_linked_cluster_share || 0) > 0
          ? `; ${(Number(wave.max_linked_cluster_share) * 100).toFixed(0)}% largest linked cluster`
          : ""}
      </span>
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

function renderTimeline(token) {
  return `
    <div class="timeline">
      ${[...token.alerts].reverse().map((alert) => `
        <div class="timeline-item">
          <strong>${esc(dateLabel(alert.created_at || alert.window_end || alert.window_start))} ${tierChip(alertTier(alert))}${alert._scope_source === "current" ? ` ${chip("latest scan", "good")}` : ""}</strong>
          <span>OBS ${moneyMaybe(alert.obs_mcap_usd || alert.pool?.mcap_usd)} / score ${esc(alert.score)} / hard ${sol(alert.hard_sol || 0)} / support ${sol(alert.support_sol || 0)} / ${esc(alert.filterLane || effectiveAlertLane(alert))}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function detailTabButton(id, label, count = null) {
  const selected = state.detailTab === id;
  const suffix = count === null ? "" : `<span>${esc(count)}</span>`;
  return `<button class="detail-tab${selected ? " is-active" : ""}" type="button" role="tab" aria-selected="${selected}" data-detail-tab="${esc(id)}">${esc(label)}${suffix}</button>`;
}

function latestTokenSocial(token) {
  return [...(token.currentSignalAlerts || [])].reverse().map((alert) => alert.social).find(Boolean) || {};
}

function renderOverviewTab(token) {
  return `
    <section class="detail-block">
      <div class="detail-block-title">Decision</div>
      <div class="kv"><span>Caught as</span><span>${renderCaughtSignal(token)}</span></div>
      <div class="kv"><span>Why caught</span><span>${esc(renderScannerReason(token))}</span></div>
      <div class="kv"><span>Cohort now</span><span>${renderThesisSummary(token)}</span></div>
      ${renderWaveLine(token)}
      <div class="kv"><span>Market phase</span><span>${renderMarketPhaseLine(token)}</span></div>
      <div class="kv"><span>Risk flags</span><span>${renderRiskFlags(token)}</span></div>
      <div class="kv"><span>Token age</span><span>${esc(durationLabel(token.tokenAgeHours))}${token.tokenCreatedAt ? ` / launched ${esc(dateLabel(token.tokenCreatedAt))}` : ""}</span></div>
      ${renderLaunchContext(token)}
    </section>
    <section class="detail-block">
      <div class="detail-block-title">Narrative</div>
      <div class="kv"><span>Primary</span><span>${renderNarrativeLine(token)}</span></div>
      <div class="kv"><span>Thesis</span><span>${renderLoreProof(token)}</span></div>
      <div class="kv"><span>Proof basis</span><span>${renderEvidenceLine(token)}</span></div>
    </section>
  `;
}

function renderWalletsTab(token) {
  return `
    <section class="detail-block">
      <div class="detail-block-title">Wallet signal</div>
      <div class="kv"><span>Fresh signal</span><span>${renderSignalTier(token)}</span></div>
      ${renderThesisDetails(token)}
      <div class="kv"><span>Wallet setup</span><span>${renderWalletCluster(token)}</span></div>
      <div class="kv"><span>Wallet PnL</span><span>${renderWalletSummary(token)}</span></div>
      ${renderTopWalletPreview(token)}
    </section>
    <section class="detail-block">
      <div class="detail-block-title">Noticed wallets</div>
      ${renderWalletRows(token)}
    </section>
  `;
}

function renderSocialTab(token) {
  const social = latestTokenSocial(token);
  const callers = tokenCallerGraph(token);
  const failures = Array.isArray(social.failures) ? social.failures.length : 0;
  return `
    <section class="detail-block">
      <div class="detail-block-title">Social pulse</div>
      <div class="social-metrics">
        ${detailMetric("Current status", socialLabel(token.currentSocialHeat, token.currentSocialReason), failures ? `${failures} provider errors` : "latest successful scan")}
        ${detailMetric("X posts", compact(social.x_posts || social.results?.length || 0), `${compact(social.unique_authors || 0)} unique authors`)}
        ${detailMetric("Callers", compact(callers.length), callers.length ? "token-matched accounts" : "none resolved")}
      </div>
      <div class="kv"><span>Primary narrative</span><span>${renderNarrativeLine(token)}</span></div>
    </section>
    <section class="detail-block">
      <div class="detail-block-title">Caller network</div>
      ${renderCallerRows(token)}
    </section>
  `;
}

function renderEvidenceTab(token, gmgnUrl, sourceLinks) {
  return `
    <section class="detail-block">
      <div class="detail-block-title">Signal evidence</div>
      <div class="kv"><span>Quality</span><span>${renderSignalQuality(token)}</span></div>
      <div class="kv"><span>Scanner rule</span><span>${renderFilterLine(token)}</span></div>
      <div class="kv"><span>Risk flags</span><span>${renderRiskFlags(token)}</span></div>
      ${renderWaveLine(token)}
    </section>
    <section class="detail-block">
      <div class="detail-block-title">Signal timeline</div>
      ${renderTimeline(token)}
    </section>
    <section class="detail-block">
      <div class="detail-block-title">Sources</div>
      <div class="kv"><span>Links</span><span>${sourceLinks || "-"}</span></div>
      <div class="kv"><span>Token</span><span><code>${esc(token.token_address)}</code></span></div>
      <div class="kv"><span>Terminal</span><span>${gmgnUrl ? `<a href="${esc(gmgnUrl)}" target="_blank" rel="noreferrer">Open GMGN</a>` : `<code>${esc(token.pool_address)}</code>`}</span></div>
    </section>
  `;
}

function renderTokenDetail(token) {
  if (!token) return `<aside class="detail token-detail"><div class="empty compact">No token selected.</div></aside>`;
  const gmgnUrl = gmgnTokenUrl(token);
  const phase = marketPhase(token);
  const athValue = token.athMcapUsd
    ? `${token.athMcapAt ? `${esc(dateLabel(token.athMcapAt))} / ` : ""}${money(token.athMcapUsd)}`
    : athStatusLabel(token.athStatus, token.athError);
  const athSub = token.athMcapUsd
    ? `${phase ? `${esc(phase.detail)} / ` : ""}${athSourceLabel(token.athSource)}`
    : "GMGN ATH not ready";
  const marketNowSub = [
    `${money(token.liquidityUsd)} liq`,
    token.scanMcapAt ? dateLabel(token.scanMcapAt) : "",
  ].filter(Boolean).join(" / ");
  const sourceLinks = renderTokenSourceLinks(token);
  const tabContent = state.detailTab === "wallets"
    ? renderWalletsTab(token)
    : state.detailTab === "social"
      ? renderSocialTab(token)
      : state.detailTab === "evidence"
        ? renderEvidenceTab(token, gmgnUrl, sourceLinks)
        : renderOverviewTab(token);
  return `
    <aside class="detail token-detail">
      <button class="detail-back" type="button">Back to tokens</button>
      <div class="detail-head">
        <div class="detail-identity">
          ${tokenAvatar(token)}
          <div>
            <h2>${esc(token.symbol)} <span class="muted">${esc(token.name)}</span>${token.hidden ? ` ${chip("deleted", "warn")}` : ""}</h2>
            <div class="detail-head-chips">
              ${workflowChip(token.workflowStatus)}
              ${currentSignalChip(token)}
              ${token.hasFilterDrift ? chip(`caught ${filterMeta(token.caughtFilter).label}`, "warn") : ""}
            </div>
          </div>
        </div>
        <div class="detail-actions">
          ${gmgnUrl ? `<a class="secondary-action detail-link" href="${esc(gmgnUrl)}" target="_blank" rel="noreferrer">Open GMGN</a>` : ""}
          ${renderHiddenAction(token)}
        </div>
      </div>
      <div class="decision-grid">
        ${detailMetric("Caught", `${esc(dateLabel(token.firstSignalAt))} / ${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)}`, `${pct(token.profitPct)} since caught`, "", pClass(token.profitPct))}
        ${detailMetric("Market now", `${moneyMaybe(token.scanMcapUsd || token.currentMcap)} mcap`, marketNowSub)}
        ${detailMetric(token.athLabel, athValue, athSub, token.athMcapUsd ? "" : "bad")}
        ${detailMetric("Noticed flow", sol(token.totalSuspiciousSol), `${token.currentScore === null ? "caught" : "current"} score ${esc(token.currentScore ?? token.caughtScore)} / ${esc(token.uniqueWallets)} wallets`)}
      </div>
      <div class="detail-tabs" role="tablist" aria-label="Token research sections">
        ${detailTabButton("overview", "Overview")}
        ${detailTabButton("wallets", "Wallets", token.uniqueWallets)}
        ${detailTabButton("social", "Social", tokenCallerGraph(token).length)}
        ${detailTabButton("evidence", "Evidence", token.alertCount)}
      </div>
      <div class="detail-tab-panel" role="tabpanel">${tabContent}</div>
    </aside>
  `;
}

function bindDetailControls() {
  document.querySelectorAll(".detail-tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.detailTab = button.dataset.detailTab || "overview";
      render();
    });
  });
  document.querySelector(".detail-back")?.addEventListener("click", () => {
    state.mobileDetailOpen = false;
    render();
  });
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
    <div class="grid token-grid${state.mobileDetailOpen ? " is-detail-open" : ""}">
      <div class="list">${tokens.map(renderTokenRow).join("")}</div>
      ${renderTokenDetail(token)}
    </div>
  `;
  document.querySelectorAll(".token-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTokenKey = row.dataset.tokenKey;
      state.detailTab = "overview";
      state.mobileDetailOpen = true;
      render();
    });
  });
  document.querySelectorAll(".token-terminal-link").forEach((link) => {
    link.addEventListener("click", (event) => event.stopPropagation());
  });
  bindTokenHideActions();
  bindDetailControls();
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
          peakHistoricalScore: 0,
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
      group.peakHistoricalScore = Math.max(group.peakHistoricalScore, token.historicalMaxScore || 0);
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
    group.tokens.sort(compareFilterTokens);
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
        <small>${esc([token.name || token.narrative.primary, token.hidden ? "deleted" : ""].filter(Boolean).join(" / "))}</small>
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
      <div class="kv"><span>Signal history</span><span>${esc(group.alerts)} signals / peak historical score ${esc(group.peakHistoricalScore)} / ${sol(group.totalSol)}</span></div>
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
      state.detailTab = "overview";
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

function heldSupplyMetric(token) {
  const currentWave = bestWaveAlert(token.currentScanAlerts || [])?.wave;
  const currentWavePct = currentWave?.sticky_supply_pct;
  if (
    currentWavePct !== null
    && currentWavePct !== undefined
    && Number.isFinite(Number(currentWavePct))
  ) {
    return {
      pct: Math.max(0, Number(currentWavePct)),
      basis: "current wave",
    };
  }

  const trackedPct = token.signalThesis?.current_retained_supply_pct;
  if (
    trackedPct !== null
    && trackedPct !== undefined
    && Number.isFinite(Number(trackedPct))
  ) {
    return {
      pct: Math.max(0, Number(trackedPct)),
      basis: "tracked cohort",
    };
  }

  const caughtWavePct = token.bestWave?.sticky_supply_pct;
  if (
    caughtWavePct !== null
    && caughtWavePct !== undefined
    && Number.isFinite(Number(caughtWavePct))
  ) {
    return {
      pct: Math.max(0, Number(caughtWavePct)),
      basis: "caught wave",
    };
  }

  return {
    pct: null,
    basis: "pending",
  };
}

function tokenFilterSubtitle(token) {
  const parts = [];
  const attention = attentionReason(token);
  if (attention && token.workflowStatus === "needs_attention") parts.push(attention);
  if (token.currentSignalAlerts.length) parts.push(`${token.currentSignalAlerts.length} latest`);
  const wave = bestWaveAlert(token.currentSignalAlerts || [])?.wave || token.bestWave;
  if (wave) {
    const buyers = Number(
      wave.effective_unique_buyers
      || wave.unique_buyers
      || 0,
    );
    if (buyers) parts.push(`${buyers} buyers`);
  } else if (token.hardSignalCount) {
    parts.push(`${token.hardSignalCount} hard`);
  } else if (token.supportSignalCount) {
    parts.push(`${token.supportSignalCount} support`);
  }
  const phase = marketPhase(token);
  if (phase?.label) parts.push(phase.label);
  if (token.hidden) parts.push("deleted");
  return parts.join(" / ");
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
          currentSignals: 0,
          firstSignalAt: null,
          latestSignalAt: null,
          currentMaxScore: null,
          wallets: new Set(),
          rawEvents: 0,
          uniqueEvents: 0,
          noticedWallets: 0,
        });
      }
      const group = groups.get(name);
      if (!group.tokenKeys.has(token.key)) {
        group.tokens.push(token);
        group.tokenKeys.add(token.key);
      }
      const groupAlerts = token.alerts;
      const groupEvents = uniqueAlertEvents(groupAlerts);
      group.alerts += groupAlerts.length;
      group.rawEvents += rawEventCount(groupAlerts);
      group.uniqueEvents += groupEvents.length;
      group.noticedWallets += sumAlertField(groupAlerts, "suspicious_wallets") || groupEvents.length;
      group.totalSol += sumAlertField(groupAlerts, "suspicious_sol") || sumEventSol(groupEvents);
      group.currentSignals += token.currentSignalAlerts.length;
      if (token.currentScore !== null) {
        group.currentMaxScore = Math.max(group.currentMaxScore ?? 0, token.currentScore);
      }
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
    group.tokens.sort(compareFilterTokens);
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
    const heldSupply = heldSupplyMetric(token);
    const selected = token.key === state.selectedTokenKey ? " is-selected" : "";
    const hidden = token.hidden ? " is-hidden" : "";
    return `
      <button class="filter-token-row${selected}${hidden}" type="button" data-token-key="${esc(token.key)}">
        <span class="filter-token-name">
          ${tokenAvatar(token, true)}
          <span class="filter-token-copy">
            <span class="filter-token-title">
              <strong>${esc(token.symbol)}</strong>
              ${primaryStatusChip(token, true)}
            </span>
            <small>${esc(tokenFilterSubtitle(token) || token.name || token.narrative.primary || "-")}</small>
          </span>
        </span>
        <span class="filter-token-value filter-token-caught">
          <strong>${moneyMaybe(token.firstObsMcapUsd || token.firstMcap)}</strong>
          <small>mcap</small>
        </span>
        <span class="filter-token-value filter-token-pnl ${pClass(token.profitPct)}">
          <strong>${pct(token.profitPct)}</strong>
          <small>since catch</small>
        </span>
        <span class="filter-token-value filter-token-held">
          <strong>${heldSupply.pct === null ? "-" : `${heldSupply.pct.toFixed(1)}%`}</strong>
          <small>${esc(heldSupply.basis)}</small>
        </span>
        <span class="filter-token-value filter-token-flow">
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
          <span class="section-eyebrow">Active lane</span>
          <h2>${esc(group.meta.label)}</h2>
          <p>${esc(group.meta.criteria)}</p>
        </div>
        <div class="filter-panel-kpis">
          <span><strong>${esc(group.tokens.length)}</strong><small>tokens</small></span>
          <span><strong>${esc(group.currentSignals)}</strong><small>fresh signals</small></span>
          <span><strong>${esc(group.noticedWallets)}</strong><small>tracked wallets</small></span>
          <span><strong>${sol(group.totalSol)}</strong><small>noticed flow</small></span>
        </div>
      </div>
      <div class="filter-panel-note">
        <span>${esc(group.meta.thesis)}</span>
        <small>${esc(dateLabel(group.firstSignalAt))} -> ${esc(dateLabel(group.latestSignalAt))} / ${group.currentMaxScore === null ? "no fresh score" : `current score ${esc(group.currentMaxScore)}`}</small>
      </div>
      <div class="filter-token-head">
        <span>Token</span>
        <span class="filter-token-caught">Caught</span>
        <span class="filter-token-pnl">PnL</span>
        <span class="filter-token-held">Held supply</span>
        <span class="filter-token-flow">Flow</span>
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
    <div class="filter-workspace${state.mobileDetailOpen ? " is-detail-open" : ""}">
      ${renderFilterTokenPanel(group)}
      ${renderTokenDetail(token)}
    </div>
  `;
  document.querySelectorAll(".filter-token-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTokenKey = row.dataset.tokenKey;
      state.detailTab = "overview";
      state.mobileDetailOpen = true;
      render();
    });
  });
  document.querySelectorAll(".token-terminal-link").forEach((link) => {
    link.addEventListener("click", (event) => event.stopPropagation());
  });
  bindTokenHideActions();
  bindDetailControls();
}

function alertMatches(alert) {
  const pool = alert.pool || {};
  const key = tokenKeyFromPool(pool);
  if (!state.showHidden && isTokenHidden(key)) return false;
  const token = buildTokenSignals().find((item) => item.key === key);
  const effectiveTier = alertTier(alert);
  if (token && !workflowMatches(token, true)) return false;
  if (!token && state.workflow !== "all") return false;
  if (state.signal !== "all" && effectiveTier !== state.signal) return false;
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
  const effectiveTier = alertTier(alert);
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
        <div class="chips">${tierChip(effectiveTier)}${alert._scope_source === "history" ? chip("historical") : ""}${(alert.quality_reasons || []).slice(0, 3).map((item) => chip(item)).join("")}${(alert.quality_penalties || []).slice(0, 3).map((item) => chip(item, "warn")).join("")}</div>
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
  els.runScan.disabled = true;
  await fetch("/api/scan?lane=reactivation", { method: "POST" });
  await loadData();
}

els.refresh.addEventListener("click", loadData);
els.runScan.addEventListener("click", runScan);
els.filterToggle?.addEventListener("click", () => {
  const open = els.filters?.classList.toggle("is-open") || false;
  els.filterToggle.setAttribute("aria-expanded", String(open));
});
els.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  render();
});
els.workflowFilter.addEventListener("change", (event) => {
  state.workflow = event.target.value;
  if (!["new_signals", "all"].includes(state.workflow)) {
    state.signal = "all";
    state.heat = "all";
    state.minScore = 0;
    els.signalFilter.value = "all";
    els.heatFilter.value = "all";
    els.scoreInput.value = "0";
  }
  state.selectedTokenKey = null;
  render();
});
els.signalFilter.addEventListener("change", (event) => {
  state.signal = event.target.value;
  state.selectedTokenKey = null;
  render();
});
els.heatFilter.addEventListener("change", (event) => {
  state.heat = event.target.value;
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
    state.mobileDetailOpen = false;
    render();
  });
});

loadData().catch((error) => {
  els.content.innerHTML = `<div class="empty">Dashboard API is not ready: ${esc(error.message)}</div>`;
});

setInterval(() => {
  loadData().catch(() => {});
}, 60000);
