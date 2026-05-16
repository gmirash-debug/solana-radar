const DEFAULT_OWNER = "gmirash-debug";
const DEFAULT_REPO = "solana-radar";
const DEFAULT_WORKFLOW = "scan-and-pages.yml";
const DEFAULT_REF = "main";
const DELETED_TOKENS_PATH = "data/deleted_tokens.json";

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "content-type,x-dispatch-secret",
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

function requireDispatchSecret(request, env) {
  const expectedSecret = env.DISPATCH_SECRET;
  if (!expectedSecret) return null;
  const url = new URL(request.url);
  const providedSecret = request.headers.get("x-dispatch-secret") || url.searchParams.get("secret");
  if (providedSecret !== expectedSecret) {
    return json({ ok: false, error: "Unauthorized" }, 401);
  }
  return null;
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
    throw new Error(`GitHub write failed: ${result.response.status} ${result.text.slice(0, 500)}`);
  }
  return result.body;
}

async function updateDeletedToken(env, payload) {
  const action = payload.action || "delete";
  const tokenAddress = normalizeId(payload.token_address || payload.token_key);
  const poolAddress = normalizeId(payload.pool_address);
  if (!tokenAddress && !poolAddress) {
    return { ok: false, error: "token_address_or_pool_address_required" };
  }

  const { data, sha } = await readDeletedTokensFromGitHub(env);
  const tokens = new Set(uniqueSorted(data.tokens));
  const pools = new Set(uniqueSorted(data.pools));
  const entryKey = tokenAddress || poolAddress;

  if (action === "restore") {
    if (tokenAddress) tokens.delete(tokenAddress);
    if (poolAddress) pools.delete(poolAddress);
    delete data.entries[entryKey];
  } else if (action === "delete") {
    if (tokenAddress) tokens.add(tokenAddress);
    if (poolAddress) pools.add(poolAddress);
    data.entries[entryKey] = {
      token_address: tokenAddress,
      pool_address: poolAddress,
      symbol: payload.symbol || "",
      name: payload.name || "",
      deleted_at: new Date().toISOString(),
    };
  } else {
    return { ok: false, error: "invalid_action", actions: ["delete", "restore"] };
  }

  data.tokens = [...tokens].sort();
  data.pools = [...pools].sort();
  data.updated_at = new Date().toISOString();
  const commit = await writeDeletedTokensToGitHub(
    env,
    data,
    sha,
    `${action === "delete" ? "Delete" : "Restore"} scanner token ${payload.symbol || tokenAddress || poolAddress}`,
  );
  return {
    ok: true,
    action,
    deleted_tokens: data,
    commit_sha: commit?.commit?.sha || null,
  };
}

async function dispatchScan(env, source) {
  const token = requireEnv(env, "GITHUB_TOKEN");
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
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
    dispatched_at: new Date().toISOString(),
  };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatchScan(env, "cloudflare-cron"));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,OPTIONS",
          "access-control-allow-headers": "content-type,x-dispatch-secret",
        },
      });
    }

    if (url.pathname === "/health") {
      return json({
        ok: true,
        worker: "solana-radar-scan-dispatcher",
        checked_at: new Date().toISOString(),
      });
    }

    if (url.pathname === "/deleted-token") {
      if (request.method !== "POST") {
        return json({ ok: false, error: "POST required" }, 405);
      }
      const unauthorized = requireDispatchSecret(request, env);
      if (unauthorized) return unauthorized;
      try {
        const payload = await request.json();
        const response = await updateDeletedToken(env, payload || {});
        return json(response, response.ok ? 200 : 400);
      } catch (error) {
        return json({ ok: false, error: error.message }, 500);
      }
    }

    if (url.pathname !== "/dispatch") {
      return json({ ok: false, error: "Use /health, /dispatch, or /deleted-token" }, 404);
    }

    if (request.method !== "POST") {
      return json({ ok: false, error: "POST required" }, 405);
    }

    const unauthorized = requireDispatchSecret(request, env);
    if (unauthorized) return unauthorized;

    try {
      return json(await dispatchScan(env, "manual-http"));
    } catch (error) {
      return json({ ok: false, error: error.message }, 500);
    }
  },
};
