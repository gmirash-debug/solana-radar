import { mutation, query } from "./_generated/server";
import { v } from "convex/values";
import { paginationOptsValidator } from "convex/server";

declare const process: { env: Record<string, string | undefined> };

const DEFAULT_DELETED_TOKENS = {
  tokens: [],
  pools: [],
  entries: {},
  updated_at: null,
};

function requireIngestSecret(secret: string) {
  const expected = process.env.CONVEX_INGEST_SECRET;
  if (!expected) {
    throw new Error("CONVEX_INGEST_SECRET is not configured");
  }
  if (secret !== expected) {
    throw new Error("Unauthorized Convex ingest");
  }
}

function asString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed || undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function objectField(value: unknown, key: string): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return (value as Record<string, unknown>)[key];
}

function alertIdentity(alert: unknown, fallbackGeneratedAt: string) {
  const pool = objectField(alert, "pool");
  const poolAddress = asString(objectField(pool, "pool_address"));
  const tokenAddress = asString(objectField(pool, "token_address"));
  const symbol = asString(objectField(pool, "symbol"));
  const lane = asString(objectField(alert, "lane"));
  const score = asNumber(objectField(alert, "score"));
  const tier = asString(objectField(alert, "action_tier"));
  const windowStart = asString(objectField(alert, "window_start"));
  const windowEnd = asString(objectField(alert, "window_end"));
  const createdAt = asString(objectField(alert, "created_at"));
  const generatedAt = createdAt || windowEnd || fallbackGeneratedAt;
  const tokenKey = tokenAddress || poolAddress || symbol || "unknown";
  const alertKey = [
    generatedAt,
    tokenKey,
    poolAddress || "",
    lane || "",
    windowStart || "",
    windowEnd || "",
    String(score ?? ""),
  ].join("|");
  return { alertKey, generatedAt, tokenKey, poolAddress, tokenAddress, symbol, lane, score, tier };
}

async function upsertStateDoc(ctx: any, key: string, payload: unknown, now: string) {
  const existing = await ctx.db.query("stateDocs").withIndex("by_key", (q: any) => q.eq("key", key)).first();
  const doc = { key, payload, updatedAt: now };
  if (existing) {
    await ctx.db.patch(existing._id, doc);
  } else {
    await ctx.db.insert("stateDocs", doc);
  }
}

async function upsertScanRun(ctx: any, report: any, now: string) {
  const generatedAt = asString(report?.generated_at) || now;
  const runKey = generatedAt;
  const existing = await ctx.db.query("scanRuns").withIndex("by_run_key", (q: any) => q.eq("runKey", runKey)).first();
  const doc = {
    runKey,
    generatedAt,
    lane: asString(report?.lane),
    profile: asString(report?.profile),
    lanesScanned: Array.isArray(report?.lanes_scanned) ? report.lanes_scanned.filter((item: unknown) => typeof item === "string") : [],
    stats: report?.stats ?? {},
    laneStats: report?.lane_stats ?? {},
    createdAt: now,
  };
  if (existing) {
    await ctx.db.patch(existing._id, doc);
  } else {
    await ctx.db.insert("scanRuns", doc);
  }
}

async function upsertAlert(ctx: any, alert: unknown, fallbackGeneratedAt: string, now: string) {
  const identity = alertIdentity(alert, fallbackGeneratedAt);
  const existing = await ctx.db
    .query("alerts")
    .withIndex("by_alert_key", (q: any) => q.eq("alertKey", identity.alertKey))
    .first();
  const doc = {
    ...identity,
    payload: alert,
    updatedAt: now,
  };
  if (existing) {
    await ctx.db.patch(existing._id, doc);
  } else {
    await ctx.db.insert("alerts", doc);
  }
}

async function upsertDeletedIndex(ctx: any, deletedTokens: any, now: string) {
  const entries = deletedTokens?.entries && typeof deletedTokens.entries === "object" ? deletedTokens.entries : {};
  const tokens = Array.isArray(deletedTokens?.tokens) ? deletedTokens.tokens : [];
  const pools = Array.isArray(deletedTokens?.pools) ? deletedTokens.pools : [];
  const rows: Array<Record<string, unknown>> = [];

  for (const token of tokens) {
    const tokenAddress = asString(token);
    if (!tokenAddress) continue;
    const entry = entries[tokenAddress] || {};
    rows.push({
      key: `token:${tokenAddress}`,
      kind: "token",
      tokenAddress,
      poolAddress: asString(entry.pool_address),
      symbol: asString(entry.symbol),
      name: asString(entry.name),
      active: true,
      deletedAt: asString(entry.deleted_at) || now,
      updatedAt: now,
    });
  }

  for (const pool of pools) {
    const poolAddress = asString(pool);
    if (!poolAddress) continue;
    rows.push({
      key: `pool:${poolAddress}`,
      kind: "pool",
      poolAddress,
      active: true,
      deletedAt: now,
      updatedAt: now,
    });
  }

  for (const row of rows) {
    const existing = await ctx.db.query("deletedTokens").withIndex("by_key", (q: any) => q.eq("key", row.key)).first();
    if (existing) {
      await ctx.db.patch(existing._id, row);
    } else {
      await ctx.db.insert("deletedTokens", row);
    }
  }
}

export const ingestSnapshot = mutation({
  args: {
    secret: v.string(),
    report: v.any(),
    history: v.array(v.any()),
    deletedTokens: v.any(),
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    const now = new Date().toISOString();
    const generatedAt = asString(args.report?.generated_at) || now;

    await upsertStateDoc(ctx, "latest_report", args.report, now);
    await upsertStateDoc(ctx, "deleted_tokens", args.deletedTokens ?? DEFAULT_DELETED_TOKENS, now);
    await upsertScanRun(ctx, args.report, now);

    for (const alert of args.history) {
      await upsertAlert(ctx, alert, generatedAt, now);
    }
    await upsertDeletedIndex(ctx, args.deletedTokens, now);

    return {
      ok: true,
      generatedAt,
      alertsSynced: args.history.length,
    };
  },
});

export const syncDeletedTokens = mutation({
  args: {
    secret: v.string(),
    deletedTokens: v.any(),
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    const now = new Date().toISOString();
    await upsertStateDoc(ctx, "deleted_tokens", args.deletedTokens ?? DEFAULT_DELETED_TOKENS, now);
    await upsertDeletedIndex(ctx, args.deletedTokens, now);
    return {
      ok: true,
      updatedAt: now,
    };
  },
});

export const dashboardData = query({
  args: {
    historyLimit: v.optional(v.number()),
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    const reportDoc = await ctx.db.query("stateDocs").withIndex("by_key", (q) => q.eq("key", "latest_report")).first();
    const deletedDoc = await ctx.db.query("stateDocs").withIndex("by_key", (q) => q.eq("key", "deleted_tokens")).first();
    const discoveryStatusDoc = await ctx.db.query("stateDocs").withIndex("by_key", (q) => q.eq("key", "discovery_status")).first();
    const historyLimit = Math.max(1, Math.min(250, Math.floor(args.historyLimit ?? 100)));
    const alertDocs = await ctx.db.query("alerts").withIndex("by_generated_at").order("desc").take(historyLimit);
    const discoveryDocs = await ctx.db
      .query("discoveryState")
      .withIndex("by_updated_at")
      .order("desc")
      .take(500);
    const report = reportDoc?.payload ?? {};
    const finishedAt = asString(objectField(report, "generated_at"));
    const market: Record<string, unknown> = {};
    for (const doc of discoveryDocs) {
      if (doc.market !== undefined) {
        market[doc.tokenKey] = doc.market;
      }
    }

    return {
      report,
      history: alertDocs.map((doc) => doc.payload),
      market,
      deleted_tokens: deletedDoc?.payload ?? DEFAULT_DELETED_TOKENS,
      discovery_status: discoveryStatusDoc?.payload ?? {},
      scan_status: {
        running: false,
        source: "convex",
        static_mode: false,
        finished_at: finishedAt ?? reportDoc?.updatedAt ?? null,
        returncode: 0,
      },
    };
  },
});

export const ingestDiscoveryState = mutation({
  args: {
    secret: v.string(),
    rows: v.array(
      v.object({
        tokenKey: v.string(),
        poolAddress: v.optional(v.union(v.string(), v.null())),
        market: v.optional(v.any()),
        baseline: v.optional(v.any()),
        queue: v.optional(v.any()),
        outcome: v.optional(v.any()),
        updatedAt: v.string(),
      }),
    ),
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    let rowsSynced = 0;
    let latestUpdatedAt = new Date().toISOString();
    for (const row of args.rows) {
      latestUpdatedAt = row.updatedAt;
      const existing = await ctx.db
        .query("discoveryState")
        .withIndex("by_token_key", (q) => q.eq("tokenKey", row.tokenKey))
        .first();
      const doc = {
        tokenKey: row.tokenKey,
        ...(row.poolAddress ? { poolAddress: row.poolAddress } : {}),
        ...(row.market !== null && row.market !== undefined
          ? { market: row.market }
          : {}),
        ...(row.baseline !== null && row.baseline !== undefined
          ? { baseline: row.baseline }
          : {}),
        ...(row.queue !== null && row.queue !== undefined
          ? { queue: row.queue }
          : {}),
        ...(row.outcome !== null && row.outcome !== undefined
          ? { outcome: row.outcome }
          : {}),
        updatedAt: row.updatedAt,
      };
      if (existing) {
        await ctx.db.patch(existing._id, doc);
      } else {
        await ctx.db.insert("discoveryState", doc);
      }
      rowsSynced += 1;
    }
    await upsertStateDoc(
      ctx,
      "discovery_status",
      { status: "ok", rowsSynced, updatedAt: latestUpdatedAt },
      latestUpdatedAt,
    );
    const legacyMarketDoc = await ctx.db
      .query("stateDocs")
      .withIndex("by_key", (q) => q.eq("key", "market"))
      .first();
    if (legacyMarketDoc) {
      await ctx.db.delete(legacyMarketDoc._id);
    }
    return { ok: true, rowsSynced };
  },
});

export const ingestDiscoveryStatus = mutation({
  args: {
    secret: v.string(),
    status: v.any(),
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    const now = new Date().toISOString();
    await upsertStateDoc(ctx, "discovery_status", args.status ?? {}, now);
    return { ok: true, updatedAt: now };
  },
});

export const discoveryStatePage = query({
  args: {
    secret: v.string(),
    paginationOpts: paginationOptsValidator,
  },
  returns: v.any(),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    return await ctx.db
      .query("discoveryState")
      .withIndex("by_token_key")
      .paginate(args.paginationOpts);
  },
});
