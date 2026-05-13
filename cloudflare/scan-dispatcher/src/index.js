const DEFAULT_OWNER = "gmirash-debug";
const DEFAULT_REPO = "solana-radar";
const DEFAULT_WORKFLOW = "scan-and-pages.yml";
const DEFAULT_REF = "main";

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
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

    if (url.pathname === "/health") {
      return json({
        ok: true,
        worker: "solana-radar-scan-dispatcher",
        checked_at: new Date().toISOString(),
      });
    }

    if (url.pathname !== "/dispatch") {
      return json({ ok: false, error: "Use /health or /dispatch" }, 404);
    }

    if (request.method !== "POST") {
      return json({ ok: false, error: "POST required" }, 405);
    }

    const expectedSecret = env.DISPATCH_SECRET;
    if (expectedSecret) {
      const providedSecret = request.headers.get("x-dispatch-secret") || url.searchParams.get("secret");
      if (providedSecret !== expectedSecret) {
        return json({ ok: false, error: "Unauthorized" }, 401);
      }
    }

    try {
      return json(await dispatchScan(env, "manual-http"));
    } catch (error) {
      return json({ ok: false, error: error.message }, 500);
    }
  },
};
