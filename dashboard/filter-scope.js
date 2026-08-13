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

export function signalTimestampMs(record = {}) {
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
  const signalMs = signalTimestampMs(record);
  return Boolean(signalMs && signalMs >= epochMs);
}

export function marketWithCurrentFilterCatch(market = {}, report = {}) {
  const epochMs = dashboardSignalEpochMs(report);
  const catchMs = signalTimestampMs(market);
  if (!epochMs || (catchMs && catchMs >= epochMs)) return { ...market };

  const scoped = { ...market };
  CATCH_FIELDS.forEach((field) => delete scoped[field]);
  return scoped;
}
