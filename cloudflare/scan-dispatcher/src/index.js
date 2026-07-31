const DEFAULT_OWNER = "gmirash-debug";
const DEFAULT_REPO = "solana-radar";
const DEFAULT_WORKFLOW = "scan-and-pages.yml";
const DEFAULT_DISCOVERY_WORKFLOW = "discovery-pulse.yml";
const DEFAULT_REF = "main";
const DELETED_TOKENS_PATH = "data/deleted_tokens.json";
const ACCESS_CERT_CACHE_TTL_MS = 60 * 60 * 1000;
const DISCOVERY_CRON = "*/5 * * * *";
const DEEP_SCAN_CRON = "7 * * * *";
let accessCertCache = { expiresAt: 0, keys: new Map() };

function corsHeaders(request, env) {
  const origin = request.headers.get("origin");
  const allowedOrigin = normalizeId(env.ALLOWED_ORIGIN);
  if (!origin || !allowedOrigin || origin !== allowedOrigin) return {};
  return {
    "access-control-allow-origin": allowedOrigin,
    "access-control-allow-methods": "POST,OPTIONS",
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

async function syncDeletedTokensToConvex(env, data) {
  const convexUrl = normalizeId(env.CONVEX_URL)?.replace(/\/+$/, "");
  const secret = env.CONVEX_INGEST_SECRET;
  if (!convexUrl || !secret) {
    return { enabled: false };
  }
  const response = await fetch(`${convexUrl}/api/mutation`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify({
      path: "radar:syncDeletedTokens",
      args: {
        secret,
        deletedTokens: data,
      },
      format: "json",
    }),
  });
  const result = await response.json().catch(() => null);
  if (!response.ok || result?.status !== "success") {
    return {
      enabled: true,
      ok: false,
      error: result?.errorMessage || `Convex sync failed: ${response.status}`,
    };
  }
  return {
    enabled: true,
    ok: true,
    value: result.value || null,
  };
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
  const convexSync = await syncDeletedTokensToConvex(env, mutation.data).catch((error) => ({
    enabled: true,
    ok: false,
    error: error.message,
  }));
  return {
    ok: true,
    action: mutation.action,
    deleted_tokens: mutation.data,
    commit_sha: commit?.commit?.sha || null,
    convex_sync: convexSync,
  };
}

function schedulerKindForCron(cron) {
  if (cron === DISCOVERY_CRON) return "discovery";
  if (cron === DEEP_SCAN_CRON) return "deep_scan";
  return "deep_scan";
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
      return json({
        ok: true,
        worker: "solana-radar-scan-dispatcher",
        checked_at: new Date().toISOString(),
        delete_access_configured: Boolean(env.CLOUDFLARE_ACCESS_AUD && env.CLOUDFLARE_ACCESS_TEAM_DOMAIN),
        scheduler_dedupe_configured: Boolean(env.DISPATCH_BUCKETS),
      }, 200, corsHeaders(request, env));
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
      return json({ ok: false, error: "Use /health, /dispatch, or /deleted-token" }, 404, corsHeaders(request, env));
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
  schedulerKindForCron,
  updateDeletedToken,
};
