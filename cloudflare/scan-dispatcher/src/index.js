const DEFAULT_OWNER = "gmirash-debug";
const DEFAULT_REPO = "solana-radar";
const DEFAULT_WORKFLOW = "scan-and-pages.yml";
const DEFAULT_DISCOVERY_WORKFLOW = "discovery-pulse.yml";
const DEFAULT_REF = "main";
const DELETED_TOKENS_PATH = "data/deleted_tokens.json";
const ACCESS_CERT_CACHE_TTL_MS = 60 * 60 * 1000;
const DISCOVERY_CRON = "*/5 * * * *";
const DEEP_SCAN_CRON = "7 * * * *";
const D1_IN_PARAMETER_LIMIT = 100;
let accessCertCache = { expiresAt: 0, keys: new Map() };

function corsHeaders(request, env) {
  const origin = request.headers.get("origin");
  const allowedOrigin = normalizeId(env.ALLOWED_ORIGIN);
  if (!origin || !allowedOrigin || origin !== allowedOrigin) return {};
  return {
    "access-control-allow-origin": allowedOrigin,
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type,cf-access-jwt-assertion",
    "access-control-allow-credentials": "true",
    vary: "Origin",
  };
}

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers,
    },
  });
}

function requireEnv(env, name) {
  const value = env[name];
  if (!value) {
    throw new Error(`Missing required secret/env: ${name}`);
  }
  return value;
}

function normalizeId(value) {
  const text = String(value || "").trim();
  return text || null;
}

function uniqueSorted(values) {
  return [...new Set((values || []).map(normalizeId).filter(Boolean))].sort();
}

function defaultDeletedTokens() {
  return {
    tokens: [],
    pools: [],
    entries: {},
    updated_at: null,
  };
}

function decodeBase64Utf8(value) {
  const binary = atob(String(value || "").replace(/\s/g, ""));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function encodeBase64Utf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

function decodeBase64UrlJson(value) {
  const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  return JSON.parse(decodeBase64Utf8(padded));
}

function base64UrlBytes(value) {
  const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function accessAudienceMatches(value, audience) {
  if (Array.isArray(value)) return value.includes(audience);
  return value === audience;
}

async function cloudflareAccessKeys(env) {
  const teamDomain = normalizeId(env.CLOUDFLARE_ACCESS_TEAM_DOMAIN);
  if (!teamDomain) throw new Error("CLOUDFLARE_ACCESS_TEAM_DOMAIN is not configured");
  if (accessCertCache.expiresAt > Date.now() && accessCertCache.keys.size) {
    return accessCertCache.keys;
  }
  const response = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`);
  const body = await response.json().catch(() => null);
  if (!response.ok || !Array.isArray(body?.keys)) {
    throw new Error(`Cloudflare Access certificate fetch failed: ${response.status}`);
  }
  const keys = new Map(body.keys.filter((key) => key?.kid).map((key) => [key.kid, key]));
  accessCertCache = {
    keys,
    expiresAt: Date.now() + ACCESS_CERT_CACHE_TTL_MS,
  };
  return keys;
}

async function requireCloudflareAccess(request, env) {
  const audience = normalizeId(env.CLOUDFLARE_ACCESS_AUD);
  if (!audience || !normalizeId(env.CLOUDFLARE_ACCESS_TEAM_DOMAIN)) {
    return { ok: false, status: 503, error: "delete_access_not_configured" };
  }
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return { ok: false, status: 401, error: "Cloudflare Access login required" };
  const [encodedHeader, encodedPayload, encodedSignature] = token.split(".");
  if (!encodedHeader || !encodedPayload || !encodedSignature) {
    return { ok: false, status: 401, error: "Invalid Cloudflare Access token" };
  }
  try {
    const header = decodeBase64UrlJson(encodedHeader);
    const claims = decodeBase64UrlJson(encodedPayload);
    if (header.alg !== "RS256" || !header.kid) {
      return { ok: false, status: 401, error: "Unsupported Cloudflare Access token" };
    }
    if (!accessAudienceMatches(claims.aud, audience) || Number(claims.exp || 0) <= Math.floor(Date.now() / 1000)) {
      return { ok: false, status: 401, error: "Expired or mismatched Cloudflare Access token" };
    }
    const key = (await cloudflareAccessKeys(env)).get(header.kid);
    if (!key) return { ok: false, status: 401, error: "Unknown Cloudflare Access signing key" };
    const cryptoKey = await crypto.subtle.importKey(
      "jwk",
      key,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      cryptoKey,
      base64UrlBytes(encodedSignature),
      new TextEncoder().encode(`${encodedHeader}.${encodedPayload}`),
    );
    return valid
      ? { ok: true }
      : { ok: false, status: 401, error: "Invalid Cloudflare Access signature" };
  } catch (error) {
    return { ok: false, status: 401, error: `Cloudflare Access verification failed: ${error.message}` };
  }
}

async function githubContentsRequest(env, path, options = {}) {
  const token = requireEnv(env, "GITHUB_TOKEN");
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  const url = `https://api.github.com/repos/${owner}/${repo}/contents/${path}`;
  const response = await fetch(url, {
    ...options,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "user-agent": "solana-radar-scan-dispatcher",
      "x-github-api-version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  return { response, body, text };
}

async function readDeletedTokensFromGitHub(env) {
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const { response, body, text } = await githubContentsRequest(env, `${DELETED_TOKENS_PATH}?ref=${encodeURIComponent(ref)}`, {
    method: "GET",
  });
  if (response.status === 404) {
    return { data: defaultDeletedTokens(), sha: null };
  }
  if (!response.ok) {
    throw new Error(`GitHub read failed: ${response.status} ${text.slice(0, 500)}`);
  }
  let data = defaultDeletedTokens();
  try {
    data = JSON.parse(decodeBase64Utf8(body.content));
  } catch {
    data = defaultDeletedTokens();
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) data = defaultDeletedTokens();
  data.tokens = Array.isArray(data.tokens) ? data.tokens : [];
  data.pools = Array.isArray(data.pools) ? data.pools : [];
  data.entries = data.entries && typeof data.entries === "object" && !Array.isArray(data.entries) ? data.entries : {};
  data.updated_at = data.updated_at || null;
  return { data, sha: body.sha };
}

async function writeDeletedTokensToGitHub(env, data, sha, message) {
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const body = {
    message,
    content: encodeBase64Utf8(JSON.stringify(data, null, 2) + "\n"),
    branch: ref,
  };
  if (sha) body.sha = sha;
  const result = await githubContentsRequest(env, DELETED_TOKENS_PATH, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  if (!result.response.ok) {
    const error = new Error(`GitHub write failed: ${result.response.status} ${result.text.slice(0, 500)}`);
    error.status = result.response.status;
    throw error;
  }
  return result.body;
}

function hasRadarDb(env) {
  return Boolean(env.RADAR_DB && typeof env.RADAR_DB.prepare === "function");
}

function serializePayload(value) {
  return JSON.stringify(value ?? {});
}

function parsePayload(value, fallback = {}) {
  try {
    const parsed = JSON.parse(value || "");
    return parsed && typeof parsed === "object" ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function ingestAccess(request, env) {
  const expected = normalizeId(env.RADAR_INGEST_SECRET);
  if (!expected) return { ok: false, status: 503, error: "ingest_not_configured" };
  const supplied = normalizeId(request.headers.get("x-radar-ingest-secret"));
  if (!supplied || supplied !== expected) return { ok: false, status: 401, error: "unauthorized" };
  return { ok: true };
}

function isoNow() {
  return new Date().toISOString();
}

function alertIdentity(alert = {}, fallbackGeneratedAt = "") {
  const pool = alert.pool || {};
  const tokenKey = normalizeId(pool.token_address) || normalizeId(pool.pool_address) || normalizeId(pool.symbol) || "unknown";
  const episodeAt = normalizeId(alert.window_start) || normalizeId(alert.created_at) || fallbackGeneratedAt;
  return {
    alertKey: [tokenKey, normalizeId(pool.pool_address) || "", normalizeId(alert.lane) || "", normalizeId(alert.signal_family) || "classified_wallets", episodeAt].join("|"),
    tokenKey,
    poolAddress: normalizeId(pool.pool_address),
    tokenAddress: normalizeId(pool.token_address),
    symbol: normalizeId(pool.symbol),
    lane: normalizeId(alert.lane),
    signalFamily: normalizeId(alert.signal_family),
    generatedAt: normalizeId(alert.created_at) || normalizeId(alert.window_end) || fallbackGeneratedAt,
    score: Number.isFinite(Number(alert.score)) ? Number(alert.score) : null,
    tier: normalizeId(alert.action_tier),
  };
}

async function upsertStateDoc(db, key, payload, sourceUpdatedAt, now = isoNow()) {
  const result = await db.prepare(`
    INSERT INTO state_docs (key, payload_json, source_updated_at, updated_at)
    VALUES (?1, ?2, ?3, ?4)
    ON CONFLICT(key) DO UPDATE SET
      payload_json = excluded.payload_json,
      source_updated_at = excluded.source_updated_at,
      updated_at = excluded.updated_at
    WHERE excluded.source_updated_at >= state_docs.source_updated_at
  `).bind(key, serializePayload(payload), sourceUpdatedAt || now, now).run();
  return Number(result?.meta?.changes || 0) > 0;
}

async function upsertScanRun(db, report, now) {
  const generatedAt = normalizeId(report?.generated_at) || now;
  await db.prepare(`
    INSERT INTO scan_runs (run_key, generated_at, lane, profile, lanes_scanned_json, stats_json, lane_stats_json, created_at)
    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
    ON CONFLICT(run_key) DO UPDATE SET
      lane = excluded.lane,
      profile = excluded.profile,
      lanes_scanned_json = excluded.lanes_scanned_json,
      stats_json = excluded.stats_json,
      lane_stats_json = excluded.lane_stats_json
  `).bind(
    generatedAt,
    generatedAt,
    normalizeId(report?.lane),
    normalizeId(report?.profile),
    serializePayload(Array.isArray(report?.lanes_scanned) ? report.lanes_scanned : []),
    serializePayload(report?.stats || {}),
    serializePayload(report?.lane_stats || {}),
    now,
  ).run();
}

async function runD1Batch(db, statements) {
  for (let index = 0; index < statements.length; index += 100) {
    await db.batch(statements.slice(index, index + 100));
  }
}

async function upsertAlerts(db, history, fallbackGeneratedAt, now) {
  const statements = [];
  for (const alert of history || []) {
    const identity = alertIdentity(alert, fallbackGeneratedAt);
    statements.push(db.prepare(`
      INSERT INTO alerts (alert_key, generated_at, token_key, pool_address, token_address, symbol, lane, signal_family, score, tier, payload_json, updated_at)
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
      ON CONFLICT(alert_key) DO UPDATE SET
        generated_at = excluded.generated_at,
        score = excluded.score,
        tier = excluded.tier,
        payload_json = excluded.payload_json,
        updated_at = excluded.updated_at
    `).bind(
      identity.alertKey,
      identity.generatedAt,
      identity.tokenKey,
      identity.poolAddress,
      identity.tokenAddress,
      identity.symbol,
      identity.lane,
      identity.signalFamily,
      identity.score,
      identity.tier,
      serializePayload(alert),
      now,
    ));
  }
  await runD1Batch(db, statements);
  return statements.length;
}

async function upsertDeletedTokenIndex(db, deletedTokens, now = isoNow()) {
  const source = deletedTokens && typeof deletedTokens === "object" ? deletedTokens : defaultDeletedTokens();
  const entries = source.entries && typeof source.entries === "object" ? source.entries : {};
  const rows = [];
  for (const token of uniqueSorted(source.tokens)) {
    const entry = entries[token] || {};
    rows.push({
      key: `token:${token}`,
      kind: "token",
      tokenAddress: token,
      poolAddress: normalizeId(entry.pool_address),
      symbol: normalizeId(entry.symbol),
      name: normalizeId(entry.name),
      deletedAt: normalizeId(entry.deleted_at) || now,
    });
  }
  for (const pool of uniqueSorted(source.pools)) {
    rows.push({ key: `pool:${pool}`, kind: "pool", tokenAddress: null, poolAddress: pool, symbol: null, name: null, deletedAt: now });
  }
  const statements = [
    db.prepare("UPDATE deleted_tokens SET active = 0, restored_at = ?1, updated_at = ?1 WHERE active = 1").bind(now),
  ];
  for (const row of rows) {
    statements.push(db.prepare(`
      INSERT INTO deleted_tokens (key, kind, token_address, pool_address, symbol, name, active, deleted_at, restored_at, updated_at)
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7, NULL, ?8)
      ON CONFLICT(key) DO UPDATE SET
        token_address = excluded.token_address,
        pool_address = excluded.pool_address,
        symbol = excluded.symbol,
        name = excluded.name,
        active = 1,
        deleted_at = excluded.deleted_at,
        restored_at = NULL,
        updated_at = excluded.updated_at
    `).bind(row.key, row.kind, row.tokenAddress, row.poolAddress, row.symbol, row.name, row.deletedAt, now));
  }
  await runD1Batch(db, statements);
  return rows.length;
}

async function pruneRadarData(db, now = isoNow()) {
  const cutoff = (days) => new Date(Date.parse(now) - days * 24 * 60 * 60 * 1000).toISOString();
  await db.prepare("DELETE FROM alerts WHERE generated_at < ?1").bind(cutoff(7)).run();
  await db.prepare("DELETE FROM scan_runs WHERE generated_at < ?1").bind(cutoff(30)).run();
  await db.prepare("DELETE FROM discovery_state WHERE updated_at < ?1").bind(cutoff(2)).run();
  await db.prepare("DELETE FROM deleted_tokens WHERE active = 0 AND updated_at < ?1").bind(cutoff(30)).run();
}

async function ingestDashboardSnapshot(env, payload) {
  if (!hasRadarDb(env)) throw new Error("radar_db_not_configured");
  const report = payload?.report && typeof payload.report === "object" ? payload.report : {};
  const history = Array.isArray(payload?.history) ? payload.history : [];
  const market = payload?.market && typeof payload.market === "object" ? payload.market : {};
  const deletedTokens = payload?.deleted_tokens || defaultDeletedTokens();
  const now = isoNow();
  const generatedAt = normalizeId(report.generated_at) || now;
  const reportJson = serializePayload(report);
  if (new TextEncoder().encode(reportJson).byteLength > 1_500_000) {
    throw new Error("compact_report_exceeds_1_5mb");
  }
  const accepted = await upsertStateDoc(env.RADAR_DB, "latest_report", report, generatedAt, now);
  if (!accepted) return { ok: true, ignored: "stale_snapshot", generated_at: generatedAt, alerts_synced: 0 };
  await upsertStateDoc(env.RADAR_DB, "deleted_tokens", deletedTokens, normalizeId(deletedTokens.updated_at) || now, now);
  await upsertScanRun(env.RADAR_DB, report, now);
  const alertsSynced = await upsertAlerts(env.RADAR_DB, history, generatedAt, now);
  const marketRows = Object.entries(market).slice(0, 250).map(([tokenKey, item]) => ({
    tokenKey,
    poolAddress: normalizeId(item?.pool_address),
    market: item,
    updatedAt: normalizeId(item?.current_market_verified_at)
      || normalizeId(item?.latest_seen_at)
      || generatedAt,
  }));
  const marketSynced = await upsertDiscoveryStateRows(env.RADAR_DB, marketRows);
  await upsertDeletedTokenIndex(env.RADAR_DB, deletedTokens, now);
  await pruneRadarData(env.RADAR_DB, now);
  return { ok: true, generated_at: generatedAt, alerts_synced: alertsSynced, market_synced: marketSynced };
}

function decodeCursor(value) {
  if (!value) return null;
  try {
    return decodeBase64UrlJson(value);
  } catch {
    return null;
  }
}

function encodeCursor(value) {
  return encodeBase64Utf8(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function scanStatusPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
  return payload.status && typeof payload.status === "object" && !Array.isArray(payload.status)
    ? payload.status
    : payload;
}

function discoveryRow(row) {
  return {
    tokenKey: row.token_key,
    poolAddress: row.pool_address || null,
    market: parsePayload(row.market_json, null),
    baseline: parsePayload(row.baseline_json, null),
    queue: parsePayload(row.queue_json, null),
    outcome: parsePayload(row.outcome_json, null),
    updatedAt: row.updated_at,
  };
}

function dashboardTokenKeys(report, history) {
  const keys = [];
  const seen = new Set();
  const add = (value) => {
    const key = normalizeId(value);
    if (key && !seen.has(key) && keys.length < 250) {
      seen.add(key);
      keys.push(key);
    }
  };
  const addPool = (pool) => add(pool?.token_address);
  for (const thesis of report?.signal_theses || []) add(thesis?.token_address);
  for (const alert of [...(report?.alerts || []), ...(history || [])]) addPool(alert?.pool);
  for (const summary of report?.summaries || []) addPool(summary?.pool);
  return keys;
}

async function discoveryStateForTokens(db, tokenKeys) {
  const rows = [];
  for (let start = 0; start < tokenKeys.length; start += D1_IN_PARAMETER_LIMIT) {
    const keys = tokenKeys.slice(start, start + D1_IN_PARAMETER_LIMIT);
    const placeholders = keys.map((_, index) => `?${index + 1}`).join(", ");
    const result = await db.prepare(`
      SELECT token_key, pool_address, market_json, baseline_json, queue_json, outcome_json, updated_at
      FROM discovery_state
      WHERE token_key IN (${placeholders})
    `).bind(...keys).all();
    rows.push(...(result.results || []));
  }
  return rows;
}

async function discoveryStatePage(env, limit, cursor) {
  if (!hasRadarDb(env)) throw new Error("radar_db_not_configured");
  const pageSize = Math.max(1, Math.min(100, Number(limit) || 50));
  const decoded = decodeCursor(cursor);
  const query = decoded?.updatedAt && decoded?.tokenKey
    ? env.RADAR_DB.prepare(`
        SELECT token_key, pool_address, market_json, baseline_json, queue_json, outcome_json, updated_at
        FROM discovery_state
        WHERE updated_at < ?1 OR (updated_at = ?1 AND token_key > ?2)
        ORDER BY updated_at DESC, token_key ASC
        LIMIT ?3
      `).bind(decoded.updatedAt, decoded.tokenKey, pageSize + 1)
    : env.RADAR_DB.prepare(`
        SELECT token_key, pool_address, market_json, baseline_json, queue_json, outcome_json, updated_at
        FROM discovery_state
        ORDER BY updated_at DESC, token_key ASC
        LIMIT ?1
      `).bind(pageSize + 1);
  const results = (await query.all()).results || [];
  const hasMore = results.length > pageSize;
  const rows = results.slice(0, pageSize).map(discoveryRow);
  const last = rows.at(-1);
  return { ok: true, rows, is_done: !hasMore, next_cursor: hasMore && last ? encodeCursor(last) : null };
}

async function upsertDiscoveryStateRows(db, rows) {
  const safeRows = Array.isArray(rows) ? rows.slice(0, 250) : [];
  const statements = [];
  for (const row of safeRows) {
    const tokenKey = normalizeId(row?.tokenKey);
    const updatedAt = normalizeId(row?.updatedAt);
    if (!tokenKey || !updatedAt) continue;
    statements.push(db.prepare(`
      INSERT INTO discovery_state (token_key, pool_address, market_json, baseline_json, queue_json, outcome_json, updated_at)
      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
      ON CONFLICT(token_key) DO UPDATE SET
        pool_address = COALESCE(excluded.pool_address, discovery_state.pool_address),
        market_json = COALESCE(excluded.market_json, discovery_state.market_json),
        baseline_json = COALESCE(excluded.baseline_json, discovery_state.baseline_json),
        queue_json = COALESCE(excluded.queue_json, discovery_state.queue_json),
        outcome_json = COALESCE(excluded.outcome_json, discovery_state.outcome_json),
        updated_at = excluded.updated_at
      WHERE excluded.updated_at >= discovery_state.updated_at
    `).bind(
      tokenKey,
      normalizeId(row.poolAddress),
      row.market === null || row.market === undefined ? null : serializePayload(row.market),
      row.baseline === null || row.baseline === undefined ? null : serializePayload(row.baseline),
      row.queue === null || row.queue === undefined ? null : serializePayload(row.queue),
      row.outcome === null || row.outcome === undefined ? null : serializePayload(row.outcome),
      updatedAt,
    ));
  }
  await runD1Batch(db, statements);
  return statements.length;
}

async function ingestDiscoveryState(env, rows) {
  if (!hasRadarDb(env)) throw new Error("radar_db_not_configured");
  const rowsSynced = await upsertDiscoveryStateRows(env.RADAR_DB, Array.isArray(rows) ? rows.slice(0, 50) : []);
  return { ok: true, rows_synced: rowsSynced };
}

async function dashboardData(env, historyLimit = 40) {
  if (!hasRadarDb(env)) throw new Error("radar_db_not_configured");
  const limit = Math.max(1, Math.min(250, Number(historyLimit) || 40));
  const [reportDoc, deletedDoc, discoveryStatusDoc, scanStatusDoc, alertsResult] = await Promise.all([
    env.RADAR_DB.prepare("SELECT payload_json, source_updated_at, updated_at FROM state_docs WHERE key = 'latest_report'").first(),
    env.RADAR_DB.prepare("SELECT payload_json FROM state_docs WHERE key = 'deleted_tokens'").first(),
    env.RADAR_DB.prepare("SELECT payload_json FROM state_docs WHERE key = 'discovery_status'").first(),
    env.RADAR_DB.prepare("SELECT payload_json FROM state_docs WHERE key = 'scan_status'").first(),
    env.RADAR_DB.prepare("SELECT payload_json FROM alerts ORDER BY generated_at DESC LIMIT ?1").bind(limit).all(),
  ]);
  const report = parsePayload(reportDoc?.payload_json, {});
  const history = (alertsResult.results || []).map((row) => parsePayload(row.payload_json, {}));
  const tokenKeys = dashboardTokenKeys(report, history);
  const discoveryRows = tokenKeys.length
    ? await discoveryStateForTokens(env.RADAR_DB, tokenKeys)
    : [];
  const market = {};
  for (const row of discoveryRows) {
    const item = discoveryRow(row);
    if (item.market) market[item.tokenKey] = item.market;
  }
  return {
    ok: true,
    report,
    history,
    market,
    deleted_tokens: parsePayload(deletedDoc?.payload_json, defaultDeletedTokens()),
    discovery_status: parsePayload(discoveryStatusDoc?.payload_json, {}),
    scan_status: parsePayload(scanStatusDoc?.payload_json, {
      running: false,
      source: "cloudflare_d1",
      static_mode: false,
      finished_at: report.generated_at || reportDoc?.updated_at || null,
      returncode: 0,
    }),
    report_source_updated_at: reportDoc?.source_updated_at || reportDoc?.updated_at || null,
  };
}

async function syncDeletedTokensToD1(env, data) {
  if (!hasRadarDb(env)) return { enabled: false, ok: false, error: "radar_db_not_configured" };
  try {
    const now = isoNow();
    await upsertStateDoc(env.RADAR_DB, "deleted_tokens", data, normalizeId(data?.updated_at) || now, now);
    const count = await upsertDeletedTokenIndex(env.RADAR_DB, data, now);
    return { enabled: true, ok: true, rows_synced: count };
  } catch (error) {
    return { enabled: true, ok: false, error: error.message };
  }
}

function applyDeletedTokenUpdate(source, payload, now) {
  const action = payload.action || "delete";
  const tokenAddress = normalizeId(payload.token_address || payload.token_key);
  const poolAddress = normalizeId(payload.pool_address);
  if (!tokenAddress && !poolAddress) {
    return { ok: false, error: "token_address_or_pool_address_required" };
  }
  const data = JSON.parse(JSON.stringify(source || defaultDeletedTokens()));
  const tokens = new Set(uniqueSorted(data.tokens));
  const pools = new Set(uniqueSorted(data.pools));
  const entryKey = tokenAddress || poolAddress;

  if (action === "restore") {
    if (tokenAddress) tokens.delete(tokenAddress);
    if (poolAddress) pools.delete(poolAddress);
    Object.entries(data.entries).forEach(([key, entry]) => {
      if (
        key === entryKey
        || key === tokenAddress
        || key === poolAddress
        || entry?.token_address === tokenAddress
        || entry?.pool_address === poolAddress
      ) {
        delete data.entries[key];
      }
    });
  } else if (action === "delete") {
    if (tokenAddress) tokens.add(tokenAddress);
    if (poolAddress) pools.add(poolAddress);
    data.entries[entryKey] = {
      token_address: tokenAddress,
      pool_address: poolAddress,
      symbol: payload.symbol || "",
      name: payload.name || "",
      deleted_at: now,
    };
  } else {
    return { ok: false, error: "invalid_action", actions: ["delete", "restore"] };
  }

  data.tokens = [...tokens].sort();
  data.pools = [...pools].sort();
  data.updated_at = now;
  return { ok: true, action, data, tokenAddress, poolAddress };
}

async function updateDeletedToken(env, payload) {
  let mutation;
  let commit;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const { data, sha } = await readDeletedTokensFromGitHub(env);
    mutation = applyDeletedTokenUpdate(data, payload, new Date().toISOString());
    if (!mutation.ok) return mutation;
    try {
      commit = await writeDeletedTokensToGitHub(
        env,
        mutation.data,
        sha,
        `${mutation.action === "delete" ? "Delete" : "Restore"} scanner token ${payload.symbol || mutation.tokenAddress || mutation.poolAddress}`,
      );
      break;
    } catch (error) {
      if (!([409, 422].includes(error.status)) || attempt === 2) throw error;
    }
  }
  if (!commit || !mutation) throw new Error("GitHub deleted-token update did not complete");
  const d1Sync = await syncDeletedTokensToD1(env, mutation.data).catch((error) => ({
    enabled: true,
    ok: false,
    error: error.message,
  }));
  return {
    ok: true,
    action: mutation.action,
    deleted_tokens: mutation.data,
    commit_sha: commit?.commit?.sha || null,
    d1_sync: d1Sync,
  };
}

function schedulerKindForCron(cron) {
  if (cron === DISCOVERY_CRON) return "discovery";
  if (cron === DEEP_SCAN_CRON) return "deep_scan";
  return "deep_scan";
}

function schedulerEnabled(env) {
  const value = normalizeId(env.SCHEDULER_ENABLED);
  if (!value) return true;
  return !["0", "false", "off", "disabled"].includes(value.toLowerCase());
}

function schedulerBucket(kind, now = new Date()) {
  const timestamp = new Date(now);
  if (kind === "discovery") {
    timestamp.setUTCMinutes(Math.floor(timestamp.getUTCMinutes() / 5) * 5, 0, 0);
  } else {
    timestamp.setUTCMinutes(0, 0, 0);
  }
  return `${kind}:${timestamp.toISOString()}`;
}

async function claimDispatchBucket(env, kind, bucket) {
  const namespace = env.DISPATCH_BUCKETS;
  if (!namespace || typeof namespace.idFromName !== "function" || typeof namespace.get !== "function") {
    return { claimed: true, persistent: false };
  }
  const objectId = namespace.idFromName(bucket);
  const response = await namespace.get(objectId).fetch("https://dispatch-bucket/claim", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      bucket,
      expiresAt: Date.now() + (kind === "discovery" ? 15 * 60 * 1000 : 2 * 60 * 60 * 1000),
    }),
  });
  if (!response.ok) throw new Error(`dispatch bucket claim failed: ${response.status}`);
  const result = await response.json();
  return { claimed: Boolean(result.claimed), persistent: true };
}

export class DispatchBuckets {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405);
    const payload = await request.json().catch(() => null);
    const bucket = normalizeId(payload?.bucket);
    const expiresAt = Number(payload?.expiresAt || 0);
    if (!bucket || !Number.isFinite(expiresAt)) {
      return json({ ok: false, error: "invalid_bucket" }, 400);
    }
    const existing = await this.state.storage.get("claim");
    if (existing?.expiresAt > Date.now()) return json({ claimed: false });
    await this.state.storage.put("claim", { bucket, expiresAt });
    return json({ claimed: true });
  }
}

async function dispatchScan(env, source, options = {}) {
  const token = requireEnv(env, "GITHUB_TOKEN");
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  const kind = options.kind || "deep_scan";
  const workflow = options.workflow
    || (kind === "discovery"
      ? env.GITHUB_DISCOVERY_WORKFLOW || DEFAULT_DISCOVERY_WORKFLOW
      : env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW);
  const ref = env.GITHUB_REF || DEFAULT_REF;

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "user-agent": "solana-radar-scan-dispatcher",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({
      ref,
      inputs: {
        source,
        dispatch_bucket: options.bucket || "manual",
      },
    }),
  });

  if (response.status !== 204) {
    const body = await response.text();
    throw new Error(`GitHub dispatch failed: ${response.status} ${body.slice(0, 500)}`);
  }

  return {
    ok: true,
    source,
    owner,
    repo,
    workflow,
    ref,
    kind,
    dispatch_bucket: options.bucket || null,
    dispatched_at: new Date().toISOString(),
  };
}

async function dispatchScheduledScan(event, env) {
  const kind = schedulerKindForCron(event?.cron);
  const bucket = schedulerBucket(kind, event?.scheduledTime || new Date());
  const claim = await claimDispatchBucket(env, kind, bucket);
  if (!claim.claimed) {
    return { ok: true, skipped: "duplicate_bucket", kind, bucket, persistent_dedupe: claim.persistent };
  }
  return {
    ...(await dispatchScan(env, `cloudflare-${kind}`, { kind, bucket })),
    persistent_dedupe: claim.persistent,
  };
}

export default {
  async scheduled(_event, env, ctx) {
    if (!schedulerEnabled(env)) return;
    ctx.waitUntil(dispatchScheduledScan(_event, env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      const headers = corsHeaders(request, env);
      if (!headers["access-control-allow-origin"]) {
        return json({ ok: false, error: "Origin not allowed" }, 403);
      }
      return new Response(null, {
        status: 204,
        headers,
      });
    }

    if (url.pathname === "/health") {
      let d1Healthy = false;
      let d1Error = null;
      if (hasRadarDb(env)) {
        try {
          await env.RADAR_DB.prepare("SELECT 1 AS ok").first();
          d1Healthy = true;
        } catch (error) {
          d1Error = error.message;
        }
      }
      return json({
        ok: d1Healthy,
        worker: "solana-radar-scan-dispatcher",
        checked_at: new Date().toISOString(),
        delete_access_configured: Boolean(env.CLOUDFLARE_ACCESS_AUD && env.CLOUDFLARE_ACCESS_TEAM_DOMAIN),
        ingest_secret_configured: Boolean(env.RADAR_INGEST_SECRET),
        scheduler_dedupe_configured: Boolean(env.DISPATCH_BUCKETS),
        d1_configured: hasRadarDb(env),
        d1_healthy: d1Healthy,
        d1_error: d1Error,
      }, 200, corsHeaders(request, env));
    }

    if (url.pathname === "/api/dashboard") {
      if (request.method !== "GET") {
        return json({ ok: false, error: "GET required" }, 405, corsHeaders(request, env));
      }
      try {
        return json(
          await dashboardData(env, url.searchParams.get("history_limit")),
          200,
          corsHeaders(request, env),
        );
      } catch (error) {
        return json({ ok: false, error: error.message }, 503, corsHeaders(request, env));
      }
    }

    if (url.pathname.startsWith("/api/")) {
      const access = ingestAccess(request, env);
      if (!access.ok) return json({ ok: false, error: access.error }, access.status, corsHeaders(request, env));
      try {
        if (url.pathname === "/api/ingest/snapshot") {
          if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
          return json(await ingestDashboardSnapshot(env, await request.json()), 200, corsHeaders(request, env));
        }
        if (url.pathname === "/api/discovery/state") {
          if (request.method === "GET") {
            return json(
              await discoveryStatePage(env, url.searchParams.get("limit"), url.searchParams.get("cursor")),
              200,
              corsHeaders(request, env),
            );
          }
          if (request.method === "POST") {
            const payload = await request.json();
            return json(await ingestDiscoveryState(env, payload?.rows), 200, corsHeaders(request, env));
          }
          return json({ ok: false, error: "GET or POST required" }, 405, corsHeaders(request, env));
        }
        if (url.pathname === "/api/discovery/status") {
          if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
          const payload = await request.json();
          const now = isoNow();
          await upsertStateDoc(env.RADAR_DB, "discovery_status", payload?.status || {}, normalizeId(payload?.status?.last_attempt_at) || now, now);
          return json({ ok: true, updated_at: now }, 200, corsHeaders(request, env));
        }
        if (url.pathname === "/api/scan/status") {
          if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
          const payload = await request.json();
          const now = isoNow();
          // Scanner clients send { status }, while an operator can safely replay
          // a raw scanner_status.json document during incident recovery.
          const status = scanStatusPayload(payload);
          await upsertStateDoc(
            env.RADAR_DB,
            "scan_status",
            status,
            normalizeId(status.last_attempt_at) || now,
            now,
          );
          return json({ ok: true, updated_at: now }, 200, corsHeaders(request, env));
        }
        if (url.pathname === "/api/deleted/sync") {
          if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
          const payload = await request.json();
          return json(await syncDeletedTokensToD1(env, payload?.deleted_tokens || payload), 200, corsHeaders(request, env));
        }
        return json({ ok: false, error: "unknown_api_path" }, 404, corsHeaders(request, env));
      } catch (error) {
        return json({ ok: false, error: error.message }, 500, corsHeaders(request, env));
      }
    }

    if (url.pathname === "/deleted-token") {
      if (request.method !== "POST") {
        return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
      }
      const access = await requireCloudflareAccess(request, env);
      if (!access.ok) return json({ ok: false, error: access.error }, access.status, corsHeaders(request, env));
      try {
        const payload = await request.json();
        const response = await updateDeletedToken(env, payload || {});
        return json(response, response.ok ? 200 : 400, corsHeaders(request, env));
      } catch (error) {
        return json({ ok: false, error: error.message }, 500, corsHeaders(request, env));
      }
    }

    if (url.pathname !== "/dispatch") {
      return json({ ok: false, error: "Use /health, /api/dashboard, /dispatch, or /deleted-token" }, 404, corsHeaders(request, env));
    }

    if (request.method !== "POST") {
      return json({ ok: false, error: "POST required" }, 405, corsHeaders(request, env));
    }

    const access = await requireCloudflareAccess(request, env);
    if (!access.ok) return json({ ok: false, error: access.error }, access.status, corsHeaders(request, env));

    try {
      return json(await dispatchScan(env, "manual-http"), 200, corsHeaders(request, env));
    } catch (error) {
      return json({ ok: false, error: error.message }, 500, corsHeaders(request, env));
    }
  },
};

export {
  applyDeletedTokenUpdate,
  claimDispatchBucket,
  corsHeaders,
  dispatchScheduledScan,
  requireCloudflareAccess,
  schedulerBucket,
  schedulerEnabled,
  schedulerKindForCron,
  scanStatusPayload,
  dashboardData,
  dashboardTokenKeys,
  discoveryStateForTokens,
  decodeCursor,
  discoveryStatePage,
  encodeCursor,
  ingestDashboardSnapshot,
  ingestDiscoveryState,
  ingestAccess,
  syncDeletedTokensToD1,
  updateDeletedToken,
};
