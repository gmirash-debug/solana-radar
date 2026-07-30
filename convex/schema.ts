import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  stateDocs: defineTable({
    key: v.string(),
    payload: v.any(),
    updatedAt: v.string(),
  }).index("by_key", ["key"]),

  scanRuns: defineTable({
    runKey: v.string(),
    generatedAt: v.string(),
    lane: v.optional(v.string()),
    profile: v.optional(v.string()),
    lanesScanned: v.array(v.string()),
    stats: v.any(),
    laneStats: v.any(),
    createdAt: v.string(),
  })
    .index("by_run_key", ["runKey"])
    .index("by_generated_at", ["generatedAt"]),

  alerts: defineTable({
    alertKey: v.string(),
    generatedAt: v.string(),
    tokenKey: v.string(),
    poolAddress: v.optional(v.string()),
    tokenAddress: v.optional(v.string()),
    symbol: v.optional(v.string()),
    lane: v.optional(v.string()),
    score: v.optional(v.number()),
    tier: v.optional(v.string()),
    payload: v.any(),
    updatedAt: v.string(),
  })
    .index("by_alert_key", ["alertKey"])
    .index("by_generated_at", ["generatedAt"])
    .index("by_token", ["tokenKey"]),

  deletedTokens: defineTable({
    key: v.string(),
    kind: v.string(),
    tokenAddress: v.optional(v.string()),
    poolAddress: v.optional(v.string()),
    symbol: v.optional(v.string()),
    name: v.optional(v.string()),
    active: v.boolean(),
    deletedAt: v.optional(v.string()),
    restoredAt: v.optional(v.string()),
    updatedAt: v.string(),
  })
    .index("by_key", ["key"])
    .index("by_active", ["active"]),

  discoveryState: defineTable({
    tokenKey: v.string(),
    poolAddress: v.optional(v.string()),
    market: v.optional(v.any()),
    baseline: v.optional(v.any()),
    queue: v.optional(v.any()),
    outcome: v.optional(v.any()),
    updatedAt: v.string(),
  })
    .index("by_token_key", ["tokenKey"])
    .index("by_updated_at", ["updatedAt"]),
});
