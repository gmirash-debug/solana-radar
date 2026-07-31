import { mutation, query } from "./_generated/server";
import type { MutationCtx } from "./_generated/server";
import { v } from "convex/values";
import { paginationOptsValidator } from "convex/server";

declare const process: { env: Record<string, string | undefined> };

const DEFAULT_DELETED_TOKENS = {
  tokens: [],
  pools: [],
  entries: {},
  updated_at: null,
};
const ALERT_RETENTION_DAYS = 7;
const SCAN_RUN_RETENTION_DAYS = 30;
const DISCOVERY_RETENTION_DAYS = 2;
const INACTIVE_DELETE_RETENTION_DAYS = 30;

type UnknownRecord = Record<string, unknown>;
type DeletedTokenRow = {
  key: string;
  kind: "token" | "pool";
  tokenAddress?: string;
  poolAddress?: string;
  symbol?: string;
  name?: string;
  active: boolean;
  deletedAt?: string;
  updatedAt: string;
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

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : {};
}

function isNewerOrEqual(incoming: string, existing?: string) {
  const incomingTime = Date.parse(incoming);
  const existingTime = Date.parse(existing || "");
  if (!Number.isFinite(incomingTime)) return false;
  if (!Number.isFinite(existingTime)) return true;
  return incomingTime >= existingTime;
}

function alertIdentity(alert: unknown, fallbackGeneratedAt: string) {
  const pool = objectField(alert, "pool");
  const poolAddress = asString(objectField(pool, "pool_address"));
  const tokenAddress = asString(objectField(pool, "token_address"));
  const symbol = asString(objectField(pool, "symbol"));
  const lane = asString(objectField(alert, "lane"));
  const score = asNumber(objectField(alert, "score"));
  const tier = asString(objectField(alert, "action_tier"));
  const signalFamily = asString(objectField(alert, "signal_family"));
  const windowStart = asString(objectField(alert, "window_start"));
  const windowEnd = asString(objectField(alert, "window_end"));
  const createdAt = asString(objectField(alert, "created_at"));
  const generatedAt = createdAt || windowEnd || fallbackGeneratedAt;
  const episodeAt = windowStart || createdAt || fallbackGeneratedAt;
  const tokenKey = tokenAddress || poolAddress || symbol || "unknown";
  const alertKey = [
    tokenKey,
    poolAddress || "",
    lane || "",
    signalFamily || "classified_wallets",
    episodeAt,
  ].join("|");
  return { alertKey, generatedAt, tokenKey, poolAddress, tokenAddress, symbol, lane, signalFamily, score, tier };
}

async function upsertStateDoc(
  ctx: MutationCtx,
  key: string,
  payload: unknown,
  now: string,
  sourceUpdatedAt = now,
) {
  const existing = await ctx.db.query("stateDocs").withIndex("by_key", (q) => q.eq("key", key)).first();
  if (existing && !isNewerOrEqual(sourceUpdatedAt, existing.sourceUpdatedAt || existing.updatedAt)) {
    return false;
  }
  const doc = { key, payload, sourceUpdatedAt, updatedAt: now };
  if (existing) {
    await ctx.db.patch(existing._id, doc);
  } else {
    await ctx.db.insert("stateDocs", doc);
  }
  return true;
}

async function upsertScanRun(ctx: MutationCtx, report: unknown, now: string) {
  const generatedAt = asString(objectField(report, "generated_at")) || now;
  const runKey = generatedAt;
  const existing = await ctx.db.query("scanRuns").withIndex("by_run_key", (q) => q.eq("runKey", runKey)).first();
  const doc = {
    runKey,
    generatedAt,
    lane: asString(objectField(report, "lane")),
    profile: asString(objectField(report, "profile")),
    lanesScanned: Array.isArray(objectField(report, "lanes_scanned"))
      ? (objectField(report, "lanes_scanned") as unknown[]).filter((item) => typeof item === "string")
      : [],
    stats: objectField(report, "stats") ?? {},
    laneStats: objectField(report, "lane_stats") ?? {},
    createdAt: now,
  };
  if (existing) {
    await ctx.db.patch(existing._id, doc);
  } else {
    await ctx.db.insert("scanRuns", doc);
  }
}

async function upsertAlert(ctx: MutationCtx, alert: unknown, fallbackGeneratedAt: string, now: string) {
  const identity = alertIdentity(alert, fallbackGeneratedAt);
  const existing = await ctx.db
    .query("alerts")
    .withIndex("by_alert_key", (q) => q.eq("alertKey", identity.alertKey))
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

async function upsertDeletedIndex(ctx: MutationCtx, deletedTokens: unknown, now: string) {
  const deleted = asRecord(deletedTokens);
  const entries = asRecord(deleted.entries);
  const tokens = Array.isArray(deleted.tokens) ? deleted.tokens : [];
  const pools = Array.isArray(deleted.pools) ? deleted.pools : [];
  const rows: DeletedTokenRow[] = [];

  for (const token of tokens) {
    const tokenAddress = asString(token);
    if (!tokenAddress) continue;
    const entry = asRecord(entries[tokenAddress]);
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
    const existing = await ctx.db.query("deletedTokens").withIndex("by_key", (q) => q.eq("key", row.key)).first();
    if (existing) {
      await ctx.db.patch(existing._id, { ...row, restoredAt: undefined });
    } else {
      await ctx.db.insert("deletedTokens", row);
    }
  }
  const activeKeys = new Set(rows.map((row) => row.key));
  const activeRows = await ctx.db
    .query("deletedTokens")
    .withIndex("by_active", (q) => q.eq("active", true))
    .take(1000);
  for (const activeRow of activeRows) {
    if (activeKeys.has(activeRow.key)) continue;
    await ctx.db.patch(activeRow._id, {
      active: false,
      restoredAt: now,
      updatedAt: now,
    });
  }
}

function retentionCutoff(days: number, now: string) {
  return new Date(Date.parse(now) - days * 24 * 60 * 60 * 1000).toISOString();
}

async function deleteExpiredDocs(
  ctx: MutationCtx,
  docs: Array<{ _id: any }>,
  dryRun: boolean,
) {
  if (!dryRun) {
    for (const doc of docs) await ctx.db.delete(doc._id);
  }
  return docs.length;
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
    const deletedUpdatedAt = asString(args.deletedTokens?.updated_at) || now;

    const reportAccepted = await upsertStateDoc(ctx, "latest_report", args.report, now, generatedAt);
    if (!reportAccepted) {
      return { ok: true, generatedAt, ignored: "stale_snapshot", alertsSynced: 0 };
    }
    const deletedAccepted = await upsertStateDoc(ctx, "deleted_tokens", args.deletedTokens ?? DEFAULT_DELETED_TOKENS, now, deletedUpdatedAt);
    await upsertScanRun(ctx, args.report, now);

    for (const alert of args.history) {
      await upsertAlert(ctx, alert, generatedAt, now);
    }
    if (deletedAccepted) {
      await upsertDeletedIndex(ctx, args.deletedTokens ?? DEFAULT_DELETED_TOKENS, now);
    }

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
    const accepted = await upsertStateDoc(
      ctx,
      "deleted_tokens",
      args.deletedTokens ?? DEFAULT_DELETED_TOKENS,
      now,
      asString(args.deletedTokens?.updated_at) || now,
    );
    if (accepted) {
      await upsertDeletedIndex(ctx, args.deletedTokens ?? DEFAULT_DELETED_TOKENS, now);
    }
    return {
      ok: true,
      updatedAt: now,
      ignored: !accepted,
    };
  },
});

export const cleanupExpiredData = mutation({
  args: {
    secret: v.string(),
    dryRun: v.optional(v.boolean()),
    batchLimit: v.optional(v.number()),
  },
  returns: v.object({
    ok: v.boolean(),
    dryRun: v.boolean(),
    deleted: v.object({
      alerts: v.number(),
      scanRuns: v.number(),
      discovery: v.number(),
      inactiveDeletes: v.number(),
    }),
  }),
  handler: async (ctx, args) => {
    requireIngestSecret(args.secret);
    const now = new Date().toISOString();
    const batchLimit = Math.max(1, Math.min(250, Math.floor(args.batchLimit ?? 100)));
    const dryRun = Boolean(args.dryRun);
    const expiredAlerts = await ctx.db
      .query("alerts")
      .withIndex("by_generated_at", (q) => q.lt("generatedAt", retentionCutoff(ALERT_RETENTION_DAYS, now)))
      .take(batchLimit);
    const expiredScanRuns = await ctx.db
      .query("scanRuns")
      .withIndex("by_generated_at", (q) => q.lt("generatedAt", retentionCutoff(SCAN_RUN_RETENTION_DAYS, now)))
      .take(batchLimit);
    const expiredDiscovery = await ctx.db
      .query("discoveryState")
      .withIndex("by_updated_at", (q) => q.lt("updatedAt", retentionCutoff(DISCOVERY_RETENTION_DAYS, now)))
      .take(batchLimit);
    const staleInactiveDeletes = (await ctx.db
      .query("deletedTokens")
      .withIndex("by_active", (q) => q.eq("active", false))
      .take(batchLimit))
      .filter((doc) => Date.parse(doc.updatedAt) < Date.parse(retentionCutoff(INACTIVE_DELETE_RETENTION_DAYS, now)));

    return {
      ok: true,
      dryRun,
      deleted: {
        alerts: await deleteExpiredDocs(ctx, expiredAlerts, dryRun),
        scanRuns: await deleteExpiredDocs(ctx, expiredScanRuns, dryRun),
        discovery: await deleteExpiredDocs(ctx, expiredDiscovery, dryRun),
        inactiveDeletes: await deleteExpiredDocs(ctx, staleInactiveDeletes, dryRun),
      },
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
      report_source_updated_at: reportDoc?.sourceUpdatedAt ?? reportDoc?.updatedAt ?? null,
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
    let latestUpdatedAt = new Date(0).toISOString();
    for (const row of args.rows) {
      if (isNewerOrEqual(row.updatedAt, latestUpdatedAt)) latestUpdatedAt = row.updatedAt;
      const existing = await ctx.db
        .query("discoveryState")
        .withIndex("by_token_key", (q) => q.eq("tokenKey", row.tokenKey))
        .first();
      if (existing && !isNewerOrEqual(row.updatedAt, existing.updatedAt)) {
        continue;
      }
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
    const sourceUpdatedAt = asString(args.status?.last_success_at)
      || asString(args.status?.last_attempt_at)
      || asString(args.status?.updatedAt)
      || now;
    await upsertStateDoc(ctx, "discovery_status", args.status ?? {}, now, sourceUpdatedAt);
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
