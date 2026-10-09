const lifecycleKey = row => String(row?.token_address || row?.pool?.token_address || "").replace(/^solana:/, "");
const lifecycleTime = row => Date.parse(row?.window_start || row?.signal_at || row?.caught_at || row?.captured_at || row?.created_at || "") || 0;

export function mergeRetirementMarkers(...sources) {
  const result = {};
  for (const source of sources) for (const [key, marker] of Object.entries(source || {})) {
    if (!marker?.retired_at || !Number.isFinite(Date.parse(marker.retired_at))) continue;
    const previous = result[key];
    const newer = !previous || Date.parse(marker.retired_at) > Date.parse(previous.retired_at);
    const activated = previous && marker.retired_at === previous.retired_at
      && Date.parse(marker.reactivated_at || "") > (Date.parse(previous.reactivated_at || "") || 0);
    if (newer || activated) result[key] = marker;
  }
  return result;
}

export function retirementRecordCurrent(row, markers) {
  const marker = markers?.[lifecycleKey(row)];
  return !marker || Boolean(marker.reactivated_at && lifecycleTime(row) > Date.parse(marker.retired_at));
}

export function applyRetirementFences(payload, markers) {
  if (!payload || !Object.keys(markers || {}).length) return payload;
  const keep = row => retirementRecordCurrent(row, markers);
  const result = {...payload, report: {...payload.report}, token_retirements: markers};
  for (const field of ["signal_theses", "alerts", "active"]) {
    if (Array.isArray(result.report[field])) result.report[field] = result.report[field].filter(keep);
  }
  for (const field of ["pools", "summaries"]) {
    if (Array.isArray(result.report[field])) result.report[field] = result.report[field].filter(row =>
      !markers[lifecycleKey(row)] || markers[lifecycleKey(row)].reactivated_at);
  }
  result.history = (payload.history || []).filter(keep);
  const visible = new Set([...(result.report.signal_theses || []), ...(result.report.alerts || []), ...result.history].map(lifecycleKey));
  result.market = Object.fromEntries(Object.entries(payload.market || {}).filter(([key]) =>
    !markers[key.replace(/^solana:/, "")] || visible.has(key.replace(/^solana:/, ""))));
  return result;
}

export function payloadTimestamp(payload) {
  const generatedAt = payload?.report?.generated_at || payload?.report_source_updated_at;
  const timestamp = new Date(generatedAt || 0).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function chooseDashboardPayload({ staticPayload, remotePayload }) {
  const markers = mergeRetirementMarkers(staticPayload?.token_retirements, remotePayload?.token_retirements);
  if (remotePayload && (!staticPayload || payloadTimestamp(remotePayload) >= payloadTimestamp(staticPayload))) {
    return {
      payload: applyRetirementFences(remotePayload, markers),
      source: "remote",
      fallbackReason: null,
    };
  }
  if (staticPayload) {
    return {
      payload: applyRetirementFences(staticPayload, markers),
      source: "static",
      fallbackReason: remotePayload ? "remote_snapshot_stale" : "remote_unavailable",
    };
  }
  return {
    payload: null,
    source: "none",
    fallbackReason: "no_valid_source",
  };
}
