export function payloadTimestamp(payload) {
  const generatedAt = payload?.report?.generated_at || payload?.report_source_updated_at;
  const timestamp = new Date(generatedAt || 0).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function chooseDashboardPayload({ staticPayload, remotePayload }) {
  if (remotePayload && (!staticPayload || payloadTimestamp(remotePayload) >= payloadTimestamp(staticPayload))) {
    return {
      payload: remotePayload,
      source: "remote",
      fallbackReason: null,
    };
  }
  if (staticPayload) {
    return {
      payload: staticPayload,
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
