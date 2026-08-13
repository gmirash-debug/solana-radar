import { timestampMs } from "./token-state.js?v=20260807-wallet-edge-1";

// The dashboard keeps historical data for Wallet Edge, but operational lists
// start from the first completed scan that used the 1d-15d Reactivation rule.
export const DEFAULT_DASHBOARD_SIGNAL_EPOCH = "2026-08-13T01:01:00Z";

const CATCH_FIELDS = [
  "first_signal_at",
  "first_obs_mcap_usd",
  "first_obs_price_usd",
  "first_obs_liquidity_usd",
  "first_obs_mcap_at",
  "first_obs_source",
  "first_obs_lane",
  "first_obs_score",
  "caught_obs_mcap_usd",
  "caught_obs_price_usd",
  "caught_obs_liquidity_usd",
  "caught_obs_mcap_at",
];

function recordTokenKey(record = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : {};
  return String(
    record?.token_address
      || pool.token_address
      || record?.pool_address
      || pool.pool_address
      || "",
  );
}

function monitorOriginMs(record = {}, report = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : {};
  const source = String(record?.source || pool.source || record?.market_source || pool.market_source || "");
  if (source !== "signal_thesis_monitor") return null;

  const tokenKey = recordTokenKey(record);
  const candidates = [record, pool];
  for (const item of [
    ...(report?.summaries || []),
    ...(report?.active_pools || []),
    ...(report?.universe || []),
  ]) {
    if (recordTokenKey(item) !== tokenKey) continue;
    candidates.push(item, item?.pool);
  }
  for (const candidate of candidates) {
    const timestamp = timestampMs(
      candidate?.first_signal_at
        || candidate?.first_obs_mcap_at
        || candidate?.signal_at
        || null,
    );
    if (timestamp) return timestamp;
  }
  return 0;
}

export function signalTimestampMs(record = {}, report = {}) {
  const originalMonitorSignalMs = monitorOriginMs(record, report);
  if (originalMonitorSignalMs !== null) return originalMonitorSignalMs;
  return timestampMs(
    record.signal_at
      || record.window_start
      || record.created_at
      || record.captured_at
      || record.window_end
      || record.first_signal_at
      || record.first_obs_mcap_at
      || null,
  );
}

export function dashboardSignalEpochMs(report = {}) {
  return timestampMs(
    report?.config?.dashboard_signal_epoch || DEFAULT_DASHBOARD_SIGNAL_EPOCH,
  );
}

export function isCurrentFilterSignal(record = {}, report = {}) {
  const epochMs = dashboardSignalEpochMs(report);
  if (!epochMs) return true;
  const signalMs = signalTimestampMs(record, report);
  return Boolean(signalMs && signalMs >= epochMs);
}

export function marketWithCurrentFilterCatch(market = {}, report = {}) {
  const epochMs = dashboardSignalEpochMs(report);
  const catchMs = signalTimestampMs(market, report);
  if (!epochMs || (catchMs && catchMs >= epochMs)) return { ...market };

  const scoped = { ...market };
  CATCH_FIELDS.forEach((field) => delete scoped[field]);
  return scoped;
}
