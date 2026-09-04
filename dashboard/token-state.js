export const MARKET_SNAPSHOT_MAX_AGE_MS = 90 * 60 * 1000;
export const DEFAULT_WORKFLOW = "tracked";

export function matchesWorkflowFilter(status, filter = DEFAULT_WORKFLOW, includeNoise = false) {
  if (status === "noise") return includeNoise && filter === "all";
  if (filter === "all") return true;
  const confirmed = ["active", "hot", "watch", "weakening"];
  if (filter === "active") return confirmed.includes(status);
  if (filter === "tracked") return [...confirmed, "candidate", "recheck_due"].includes(status);
  return status === filter;
}

export function isTrackedAlertTier(tier) {
  return ["candidate", "actionable", "hot_reactivation", "watch", "late_chase"].includes(tier);
}

export function resolveWorkflowStatus({ lifecycle, dataStatus, currentTier, currentConfirmed = false, thesisConfirmed = false }) {
  if (lifecycle === "closed") return "inactive";
  if (dataStatus !== "current") return "recheck_due";
  if (lifecycle === "weakening") return thesisConfirmed ? "weakening" : "candidate";
  if (currentConfirmed && ["actionable", "hot_reactivation"].includes(currentTier)) return "hot";
  if (currentConfirmed && currentTier === "watch") return "watch";
  if (thesisConfirmed && lifecycle === "holding") return "active";
  return currentTier === "noise" ? "noise" : "candidate";
}

function positiveNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return number;
  }
  return null;
}

export function timestampMs(value) {
  const parsed = new Date(value || 0).getTime();
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

export function compareTokensByCatchNewest(a = {}, b = {}) {
  return timestampMs(b.firstSignalAt) - timestampMs(a.firstSignalAt);
}

function signalTime(signal = {}) {
  return signal.created_at || signal.window_end || signal.window_start || null;
}

function alertSnapshot(alert = {}) {
  const pool = alert.pool || {};
  return {
    at: alert.obs_mcap_at || signalTime(alert),
    mcapUsd: positiveNumber(alert.obs_mcap_usd, pool.mcap_usd),
    priceUsd: positiveNumber(alert.obs_price_usd, pool.price_usd),
    liquidityUsd: positiveNumber(alert.obs_liquidity_usd, pool.liquidity_usd),
    lane: alert.first_obs_lane || alert.obs_lane || alert.lane || null,
    tier: alert.action_tier || null,
    score: Number.isFinite(Number(alert.score)) ? Number(alert.score) : null,
    flowSol: positiveNumber(alert.suspicious_sol),
    wallets: positiveNumber(alert.suspicious_wallets),
  };
}

export function resolveSignalEpisodes({ alerts = [], market = {}, thesis = null } = {}) {
  const orderedAlerts = [...alerts].sort((a, b) => timestampMs(signalTime(a)) - timestampMs(signalTime(b)));
  const firstAlert = orderedAlerts[0] || {};
  const latestAlert = orderedAlerts[orderedAlerts.length - 1] || firstAlert;
  const firstAlertSnapshot = alertSnapshot(firstAlert);

  const originalCatch = {
    at: market.first_signal_at || market.first_obs_mcap_at || firstAlertSnapshot.at || null,
    mcapUsd: positiveNumber(market.first_obs_mcap_usd, market.caught_obs_mcap_usd, firstAlertSnapshot.mcapUsd),
    priceUsd: positiveNumber(market.first_obs_price_usd, market.caught_obs_price_usd, firstAlertSnapshot.priceUsd),
    liquidityUsd: positiveNumber(market.first_obs_liquidity_usd, market.caught_obs_liquidity_usd, firstAlertSnapshot.liquidityUsd),
    lane: market.first_obs_lane || firstAlertSnapshot.lane || null,
    score: Number.isFinite(Number(market.first_obs_score)) ? Number(market.first_obs_score) : firstAlertSnapshot.score,
    tier: firstAlertSnapshot.tier,
    source: market.first_signal_at || market.first_obs_mcap_at ? "market_history" : "alert_history",
  };

  const latestSnapshot = alertSnapshot(latestAlert);
  const thesisAt = thesis?.signal_at || thesis?.signal_window_start || thesis?.signal_window_end || null;
  const hasActiveEpisode = Boolean(thesisAt || thesis?.source_score || thesis?.source_tier);
  const activeEpisode = hasActiveEpisode ? {
    at: thesisAt || latestSnapshot.at || null,
    mcapUsd: positiveNumber(thesis?.signal_mcap_usd, latestSnapshot.mcapUsd),
    priceUsd: positiveNumber(thesis?.signal_price_usd, latestSnapshot.priceUsd),
    liquidityUsd: positiveNumber(thesis?.signal_liquidity_usd, latestSnapshot.liquidityUsd),
    lane: "reactivation",
    tier: thesis?.source_tier || latestSnapshot.tier || "watch",
    score: Number.isFinite(Number(thesis?.source_score)) ? Number(thesis.source_score) : latestSnapshot.score,
    flowSol: positiveNumber(thesis?.source_flow_sol, latestSnapshot.flowSol),
    wallets: positiveNumber(thesis?.source_wallets, thesis?.original_wallets, latestSnapshot.wallets),
    source: "signal_thesis",
  } : null;

  return {
    originalCatch,
    activeEpisode,
    displayCatch: activeEpisode || originalCatch,
  };
}

export function resolveCurrentMarket({ pool = {}, latestObservation = null, now = Date.now(), maxAgeMs = MARKET_SNAPSHOT_MAX_AGE_MS } = {}) {
  const directReportSnapshot = ["universe", "active", "summary"].includes(pool._snapshot_source);
  const observedAt = directReportSnapshot
    ? pool._observed_at || null
    : pool.current_market_verified_at
      || pool.latest_seen_at
      || pool.market_snapshot_at
      || pool.scan_mcap_at
      || latestObservation?.at
      || null;
  const observedMs = timestampMs(observedAt);
  const explicitlyStale = !directReportSnapshot && pool.market_snapshot_stale === true;
  const hasValue = Boolean(directReportSnapshot
    ? positiveNumber(pool.mcap_usd)
    : positiveNumber(pool.latest_mcap_usd, pool.mcap_usd, pool.scan_mcap_usd, latestObservation?.mcap_usd));
  const staleByAge = !observedMs || Math.max(0, Number(now) - observedMs) > maxAgeMs;
  const stale = explicitlyStale || staleByAge || !hasValue;
  const staleReason = explicitlyStale
    ? pool.market_snapshot_error || "refresh failed"
    : !hasValue
      ? "market value missing"
      : staleByAge
        ? "snapshot expired"
        : null;

  return {
    isFresh: !stale,
    observedAt,
    observedMs,
    staleReason,
    source: pool._snapshot_source || pool.market_source || pool.scan_source || latestObservation?.source || null,
    mcapUsd: stale ? null : directReportSnapshot
      ? positiveNumber(pool.mcap_usd)
      : positiveNumber(pool.latest_mcap_usd, pool.mcap_usd, pool.scan_mcap_usd, latestObservation?.mcap_usd),
    priceUsd: stale ? null : directReportSnapshot
      ? positiveNumber(pool.price_usd)
      : positiveNumber(pool.latest_price_usd, pool.price_usd, pool.scan_price_usd, latestObservation?.price_usd),
    liquidityUsd: stale ? null : directReportSnapshot
      ? positiveNumber(pool.liquidity_usd)
      : positiveNumber(pool.latest_liquidity_usd, pool.liquidity_usd, pool.scan_liquidity_usd),
    lastVerifiedMcapUsd: directReportSnapshot
      ? positiveNumber(pool.mcap_usd)
      : positiveNumber(pool.latest_mcap_usd, pool.mcap_usd, pool.scan_mcap_usd, latestObservation?.mcap_usd),
    lastVerifiedLiquidityUsd: directReportSnapshot
      ? positiveNumber(pool.liquidity_usd)
      : positiveNumber(pool.latest_liquidity_usd, pool.liquidity_usd, pool.scan_liquidity_usd),
  };
}

export function resolveAthContext({ market = {}, currentMarket = {} } = {}) {
  const source = market.ath_source || "missing";
  const value = positiveNumber(market.ath_mcap_usd);
  const suspect = market.ath_status === "suspect" || market.ath_validation_status === "suspect";
  const sourceAllowed = ["gmgn", "ohlcv_high", "solana_tracker"].includes(source);
  const trusted = Boolean(value && sourceAllowed && !suspect);
  const legacy = trusted && source === "solana_tracker" && market.ath_validation_status !== "valid";
  return {
    mcapUsd: trusted ? value : null,
    at: trusted ? market.ath_mcap_at || null : null,
    source: trusted ? source : "missing",
    status: trusted ? (legacy ? "legacy" : market.ath_status || "ready") : market.ath_status || (market.ath_error ? "error" : "pending"),
    error: market.ath_error || "",
    verifiedAt: market.ath_verified_at || market.ath_latest_checked_at || market.ath_checked_at || null,
    poolAddress: market.ath_pool_address || null,
    ratio: trusted && currentMarket.isFresh && currentMarket.mcapUsd
      ? currentMarket.mcapUsd / value
      : null,
  };
}
