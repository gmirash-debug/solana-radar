const DEFAULT_DASHBOARD_SIGNAL_EPOCH = "2026-08-13T01:01:00Z";
function normalizeId(value) {
  const text = String(value || "").trim();
  return text || null;
}

function timestampMs(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function dashboardRecordTokenKey(record = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : {};
  return normalizeId(record?.token_address)
    || normalizeId(pool.token_address)
    || normalizeId(record?.pool_address)
    || normalizeId(pool.pool_address);
}

function dashboardMonitorOriginMs(record = {}, report = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : {};
  const source = String(record?.source || pool.source || record?.market_source || pool.market_source || "");
  if (source !== "signal_thesis_monitor") return null;

  const tokenKey = dashboardRecordTokenKey(record);
  const candidates = [record, pool];
  for (const item of [
    ...(report?.summaries || []),
    ...(report?.active_pools || []),
    ...(report?.universe || []),
  ]) {
    if (dashboardRecordTokenKey(item) !== tokenKey) continue;
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

function dashboardSignalTimestampMs(record = {}, report = {}) {
  const monitorOriginMs = dashboardMonitorOriginMs(record, report);
  if (monitorOriginMs !== null) return monitorOriginMs;
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

function dashboardSignalEpochMs(report = {}) {
  return timestampMs(
    report?.config?.dashboard_signal_epoch || DEFAULT_DASHBOARD_SIGNAL_EPOCH,
  );
}

function dashboardRecordAgeHours(record = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : {};
  const pairCreatedAt = Number(pool.pair_created_at ?? record?.pair_created_at);
  if (Number.isFinite(pairCreatedAt) && pairCreatedAt > 0) {
    const pairCreatedMs = pairCreatedAt > 10_000_000_000
      ? pairCreatedAt
      : pairCreatedAt * 1000;
    return Math.max(0, (Date.now() - pairCreatedMs) / 3_600_000);
  }
  const reportedAgeHours = Number(pool.age_hours ?? record?.age_hours);
  return Number.isFinite(reportedAgeHours) && reportedAgeHours >= 0
    ? reportedAgeHours
    : null;
}

function dashboardRecordMatchesAgeWindow(record = {}, report = {}) {
  const minAgeHours = Number(report?.config?.age_min_hours);
  const maxAgeHours = Number(report?.config?.age_max_hours);
  const hasMin = Number.isFinite(minAgeHours);
  const hasMax = Number.isFinite(maxAgeHours);
  if (!hasMin && !hasMax) return true;

  const ageHours = dashboardRecordAgeHours(record);
  if (ageHours === null) return false;
  if (hasMin && ageHours < minAgeHours) return false;
  if (hasMax && ageHours > maxAgeHours) return false;
  return true;
}

function isCurrentDashboardSignal(record = {}, report = {}) {
  const epochMs = dashboardSignalEpochMs(report);
  const signalMs = dashboardSignalTimestampMs(record, report);
  return Boolean(
    dashboardRecordMatchesAgeWindow(record, report)
    && (!epochMs || (signalMs && signalMs >= epochMs)),
  );
}

function isCurrentDashboardPool(record = {}, report = {}) {
  const pool = record?.pool && typeof record.pool === "object" ? record.pool : record;
  const source = String(record?.source || pool?.source || record?.market_source || pool?.market_source || "");
  if (!dashboardRecordMatchesAgeWindow(record, report)) return false;
  if (source !== "signal_thesis_monitor") return true;
  return isCurrentDashboardSignal({ pool }, report);
}

function dashboardTokenFromRecord(record = {}) {
  return dashboardRecordTokenKey(record);
}

function dashboardRecordMatchesToken(record, tokenKey) {
  return dashboardTokenFromRecord(record) === normalizeId(tokenKey);
}

function compactCoordinationEvidence(evidence) {
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) return evidence;
  return {...evidence, signals: (Array.isArray(evidence.signals) ? evidence.signals : []).filter(signal => signal && typeof signal === "object").map(signal => {
    const {members, ...summary} = signal;
    if (signal.detail && typeof signal.detail === "object") {
      const {source, ...detail} = signal.detail;
      summary.detail = detail;
    }
    return summary;
  })};
}

function compactDashboardAlert(alert = {}) {
  if (!alert || typeof alert !== "object" || Array.isArray(alert)) return {};
  const detailFields = new Set([
    "events",
    "coordination_events",
    "common_funders",
    "common_recipients",
    "common_executors",
  ]);
  const compact = Object.fromEntries(
    Object.entries(alert).filter(([key]) => !detailFields.has(key)),
  );
  for (const field of detailFields) {
    if (Array.isArray(alert[field])) compact[`${field}_count`] = alert[field].length;
  }
  if (alert.wave && typeof alert.wave === "object" && !Array.isArray(alert.wave)) {
    const { top_buyers: topBuyers, ...wave } = alert.wave;
    if (Array.isArray(topBuyers)) wave.top_buyers_count = topBuyers.length;
    compact.wave = wave;
  }
  if (alert.wallet_graph && typeof alert.wallet_graph === "object") {
    const {wallets, clusters, common_funders, common_executors, ...graph} = alert.wallet_graph;
    compact.wallet_graph = graph;
  }
  if (alert.supply_integrity && typeof alert.supply_integrity === "object") {
    const {
      top_owners: topOwners,
      linkage_groups: linkageGroups,
      linked_clusters: linkedClusters,
      limitations,
      errors,
      invariants,
      ...summary
    } = alert.supply_integrity;
    compact.supply_integrity = summary;
  }
  if (alert.coordinated_activity) compact.coordinated_activity = compactCoordinationEvidence(alert.coordinated_activity);
  return compact;
}

function compactDashboardThesis(thesis = {}) {
  if (!thesis || typeof thesis !== "object" || Array.isArray(thesis)) return {};
  const {
    cohort,
    cohort_wallets: cohortWallets,
    supply_integrity_history: supplyIntegrityHistory,
    coordination_inputs: coordinationInputs,
    ...compact
  } = thesis;
  if (thesis.supply_integrity && typeof thesis.supply_integrity === "object") {
    const {
      top_owners: topOwners,
      linkage_groups: linkageGroups,
      linked_clusters: linkedClusters,
      limitations,
      errors,
      invariants,
      ...summary
    } = thesis.supply_integrity;
    compact.supply_integrity = summary;
  }
  if (thesis.coordinated_activity) compact.coordinated_activity = compactCoordinationEvidence(thesis.coordinated_activity);
  return compact;
}

function compactDashboardReport(report = {}) {
  if (!report || typeof report !== "object" || Array.isArray(report)) return {};
  return {
    ...report,
    alerts: (report.alerts || [])
      .filter((alert) => isCurrentDashboardSignal(alert, report))
      .map(compactDashboardAlert),
    signal_theses: (report.signal_theses || [])
      .filter((thesis) => isCurrentDashboardSignal(thesis, report))
      .map(compactDashboardThesis),
    active_pools: (report.active_pools || [])
      .filter((item) => isCurrentDashboardPool(item, report)),
    universe: (report.universe || [])
      .filter((pool) => isCurrentDashboardPool(pool, report)),
    summaries: (report.summaries || [])
      .filter((summary) => isCurrentDashboardPool(summary, report)),
    remote_compact: true,
  };
}

export {dashboardRecordTokenKey, dashboardMonitorOriginMs, dashboardSignalTimestampMs, dashboardSignalEpochMs, dashboardRecordAgeHours, dashboardRecordMatchesAgeWindow, isCurrentDashboardSignal, isCurrentDashboardPool, dashboardTokenFromRecord, dashboardRecordMatchesToken, compactCoordinationEvidence, compactDashboardAlert, compactDashboardThesis, compactDashboardReport};
