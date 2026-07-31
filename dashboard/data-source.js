export function payloadTimestamp(payload) {
  const generatedAt = payload?.report?.generated_at || payload?.report_source_updated_at;
  const timestamp = new Date(generatedAt || 0).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function chooseDashboardPayload({ staticPayload, convexPayload }) {
  if (staticPayload && (!convexPayload || payloadTimestamp(staticPayload) >= payloadTimestamp(convexPayload))) {
    return {
      payload: staticPayload,
      source: "static",
      fallbackReason: convexPayload ? "static_snapshot_is_newer_or_equal" : "convex_unavailable",
    };
  }
  if (convexPayload) {
    return {
      payload: convexPayload,
      source: "convex",
      fallbackReason: null,
    };
  }
  return {
    payload: null,
    source: "none",
    fallbackReason: "no_valid_source",
  };
}
