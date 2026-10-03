// History processing is independent of dashboard publication; keep expensive
// derived wallet writes bounded within each background invocation.
import {stepClusterJob} from "./history-clusters.js";
const OUTBOX_BATCH_SIZE = 2;
const D1_IN_PARAMETER_LIMIT = 100;
const HISTORY_SCHEMA_VERSION = 1;
const OUTCOME_HORIZONS = {
  "1h": 60,
  "6h": 360,
  "24h": 1440,
  "72h": 4320,
  "7d": 10080,
};
const SCORE_VERSION = 1;
const PRIOR_STRENGTH = 8;

function text(value) {
  const normalized = String(value || "").trim();
  return normalized || null;
}

function number(value, fallback = null) {
  if (value === null) return null;
  if (value === undefined || typeof value === "boolean"
      || (typeof value === "string" && !value.trim())) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positive(value, fallback = null) {
  const parsed = number(value, null);
  return parsed !== null && parsed > 0 ? parsed : fallback;
}

const firstDefined = (...values) => values.find(value => value !== undefined);

function iso(value, fallback = null) {
  const raw = text(value);
  if (!raw) return fallback;
  const parsed = new Date(raw).getTime();
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : fallback;
}

function nowIso() {
  return new Date().toISOString();
}

function hash(value) {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ code, 0x85ebca6b);
  }
  return `${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

function median(values) {
  const rows = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!rows.length) return null;
  const middle = Math.floor(rows.length / 2);
  return rows.length % 2 ? rows[middle] : (rows[middle - 1] + rows[middle]) / 2;
}

function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

export function mcapBand(value) {
  const mcap = positive(value, null);
  if (mcap === null) return "unknown";
  if (mcap < 25_000) return "lt_25k";
  if (mcap < 50_000) return "25k_50k";
  if (mcap < 100_000) return "50k_100k";
  if (mcap < 250_000) return "100k_250k";
  if (mcap < 500_000) return "250k_500k";
  if (mcap < 1_000_000) return "500k_1m";
  return "1m_5m";
}

export function liquidityBand(value) {
  const liquidity = positive(value, null);
  if (liquidity === null) return "unknown";
  if (liquidity < 5_000) return "lt_5k";
  if (liquidity < 15_000) return "5k_15k";
  if (liquidity < 50_000) return "15k_50k";
  if (liquidity < 150_000) return "50k_150k";
  return "gte_150k";
}

export function ageBand(value) {
  const age = positive(value, null);
  if (age === null) return "unknown";
  if (age < 30) return "15d_30d";
  if (age < 90) return "30d_90d";
  return "90d_plus";
}

function parsePayload(value, fallback = {}) {
  try {
    const parsed = JSON.parse(value || "");
    return parsed && typeof parsed === "object" ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function serialize(value) {
  return JSON.stringify(value ?? {});
}

export function hasHistoryDb(env) {
  return Boolean(env?.RADAR_HISTORY_DB && typeof env.RADAR_HISTORY_DB.prepare === "function");
}

export function historyEventEffects(eventType) {
  const type = text(eventType) || "snapshot";
  const isSignalEvent = type === "signal";
  const isOutcomeEvent = type.startsWith("outcome_");
  const isClusterRepair = type === "cluster_repair";
  return {
    type,
    isSignalEvent,
    isOutcomeEvent,
    capturesPriorScore: isSignalEvent,
    updatesOutcomes: isSignalEvent || isOutcomeEvent,
    recordsClusterEdge: isSignalEvent,
    refreshesScores: isOutcomeEvent,
    refreshesClusters: isSignalEvent || isOutcomeEvent || isClusterRepair,
  };
}

function historyEventId(event) {
  const provided = text(event?.event_id);
  if (provided) return provided;
  const episode = text(event?.episode?.episode_id) || "unknown";
  const observedAt = iso(event?.event?.observed_at, "unknown");
  const type = text(event?.event?.event_type) || "snapshot";
  const observedEpoch = Math.max(0, Math.floor(new Date(observedAt).getTime() / 1000) || 0);
  return `history:${String(observedEpoch).padStart(10, "0")}:${hash(`${episode}|${type}|${observedAt}`)}`;
}

function episodeId(episode = {}) {
  const provided = text(episode.episode_id);
  if (provided) return provided;
  const token = text(episode.token_address) || text(episode.pool_address) || "unknown";
  const family = text(episode.signal_family) || "reactivation";
  const caughtAt = iso(episode.caught_at, "unknown");
  return `episode:${hash(`${token}|${family}|${caughtAt}`)}`;
}

function normalizedEpisode(raw = {}, fallbackNow = nowIso()) {
  const caughtAt = iso(raw.caught_at, fallbackNow);
  const lastSignalAt = iso(raw.last_signal_at, caughtAt);
  const caughtMcap = positive(raw.caught_mcap_usd);
  const caughtLiquidity = positive(raw.caught_liquidity_usd);
  const tokenAgeDays = positive(raw.token_age_days);
  const episode = {
    episode_id: episodeId(raw),
    token_address: text(raw.token_address) || text(raw.pool_address),
    pool_address: text(raw.pool_address),
    symbol: text(raw.symbol),
    name: text(raw.name),
    lane: text(raw.lane) || "reactivation",
    signal_family: text(raw.signal_family) || "reactivation_wave",
    caught_at: caughtAt,
    last_signal_at: lastSignalAt,
    closed_at: iso(raw.closed_at),
    caught_tier: text(raw.caught_tier),
    caught_score: number(raw.caught_score),
    caught_price_usd: positive(raw.caught_price_usd),
    caught_mcap_usd: caughtMcap,
    caught_liquidity_usd: caughtLiquidity,
    token_age_days: tokenAgeDays,
    ath_mcap_usd: positive(raw.ath_mcap_usd),
    ath_ratio: positive(raw.ath_ratio),
    market_stage: text(raw.market_stage),
    mcap_band: text(raw.mcap_band) || mcapBand(caughtMcap),
    liquidity_band: text(raw.liquidity_band) || liquidityBand(caughtLiquidity),
    age_band: text(raw.age_band) || ageBand(tokenAgeDays),
    source_run_key: text(raw.source_run_key),
    source_kind: text(raw.source_kind) || "live",
    data_quality_status: text(raw.data_quality_status) || "partial",
    schema_version: Number(raw.schema_version) || HISTORY_SCHEMA_VERSION,
    raw_object_key: text(raw.raw_object_key),
  };
  if (!episode.token_address) return null;
  return episode;
}

function normalizedWallet(raw = {}, episode = {}) {
  const wallet = text(raw.wallet_address) || text(raw.owner);
  if (!wallet) return null;
  const bought = number(firstDefined(raw.bought_tokens, raw.attributed_tokens, raw.token_bought));
  const balance = number(firstDefined(raw.current_token_balance, raw.current_balance));
  const retainedPct = number(firstDefined(raw.balance_retained_pct, raw.retention_pct),
    bought > 0 && balance !== null ? clamp(balance / bought * 100, 0, 100) : null);
  const rawBehavior = text(raw.behavior_status);
  const behavior = rawBehavior || (
    retainedPct === null ? "unknown" : retainedPct >= 99 ? "holding" : retainedPct > 0 ? "reduced_unverified" : "reduced_unverified"
  );
  return {
    wallet_address: wallet,
    cohort_role: text(raw.cohort_role) || "at_catch",
    wallet_class_at_signal: text(raw.wallet_class_at_signal) || text(raw.wallet_class),
    first_buy_at: iso(raw.first_buy_at ?? raw.first_buy_time),
    last_buy_at: iso(raw.last_buy_at ?? raw.first_buy_time),
    buy_count: Number.isFinite(Number(raw.buy_count ?? raw.buys)) ? Number(raw.buy_count ?? raw.buys) : 1,
    buy_sol: positive(raw.buy_sol ?? raw.sol_in, 0),
    bought_tokens: bought,
    average_entry_price: positive(raw.average_entry_price),
    entry_mcap_usd: positive(raw.entry_mcap_usd, episode.caught_mcap_usd),
    supply_pct_bought: number(raw.supply_pct_bought ?? raw.held_supply_pct_at_catch, null),
    held_tokens_at_catch: number(firstDefined(raw.held_tokens_at_catch, raw.initial_balance, raw.current_balance)),
    held_supply_pct_at_catch: number(raw.held_supply_pct_at_catch, null),
    // Retention is a live observation. It must never overwrite the at-catch
    // fact or turn a balance decrease into a claimed sale.
    retained_pct_at_catch: number(raw.retained_pct_at_catch, bought > 0 ? 100 : null),
    common_funder: text(raw.common_funder),
    common_executor: text(raw.common_executor),
    common_funder_kind: text(raw.common_funder_kind),
    common_executor_kind: text(raw.common_executor_kind),
    source_kind: text(raw.source_kind),
    supporting_only: raw.supporting_only === true,
    cluster_id_at_catch: text(raw.cluster_id_at_catch),
    evidence_status: text(raw.evidence_status) || "partial",
    raw_object_key: text(raw.raw_object_key),
    observation: {
      current_token_balance: balance,
      balance_retained_pct: retainedPct,
      additional_buy_tokens: number(raw.additional_buy_tokens),
      outbound_transfer_tokens: number(raw.outbound_transfer_tokens),
      behavior_status: behavior,
      estimated_pnl_pct: number(raw.estimated_pnl_pct ?? raw.pnl_pct),
      estimated_pnl_sol: number(raw.estimated_pnl_sol ?? raw.pnl_sol),
      coverage_status: text(raw.coverage_status) || (retainedPct === null ? "partial" : "complete"),
      raw_object_key: text(raw.raw_object_key),
    },
  };
}

function normalizedOutcome(raw = {}, episode = {}, now = nowIso()) {
  const horizons = raw?.horizons && typeof raw.horizons === "object" ? raw.horizons : {};
  const rows = [];
  for (const [name, minutes] of Object.entries(OUTCOME_HORIZONS)) {
    const checkpoint = horizons[name];
    const dueAt = new Date(new Date(episode.caught_at).getTime() + minutes * 60_000).toISOString();
    const hasCheckpoint = checkpoint && typeof checkpoint === "object";
    const delayMs = hasCheckpoint ? Date.parse(checkpoint.at) - Date.parse(checkpoint.target_at || dueAt) : NaN;
    const timely = hasCheckpoint && Number.isFinite(delayMs) && delayMs >= 0 && delayMs <= 3_600_000
      && !["delayed", "partial"].includes(checkpoint.quality_status);
    // Horizon values are frozen by the scanner when that horizon first becomes
    // due. Do not use the current all-time peak here: doing so would leak a
    // later pump into an earlier 1h/6h/24h outcome.
    const maxReturn = number(checkpoint?.max_return_pct, hasCheckpoint ? number(checkpoint.return_pct) : null);
    const maxDrawdown = number(checkpoint?.max_drawdown_pct, null);
    const endpointLiquidity = positive(checkpoint?.liquidity_usd);
    const liquidityFloor = Math.max(3_000, (episode.caught_liquidity_usd || 0) * 0.75);
    const hit2x = maxReturn !== null ? Number(maxReturn >= 100) : null;
    rows.push({
      horizon_minutes: minutes,
      due_at: dueAt,
      evaluated_at: hasCheckpoint ? iso(checkpoint.at, now) : null,
      endpoint_price_usd: positive(checkpoint?.price_usd),
      endpoint_mcap_usd: positive(checkpoint?.mcap_usd),
      endpoint_liquidity_usd: endpointLiquidity,
      return_pct: number(checkpoint?.return_pct),
      max_return_pct: maxReturn,
      max_drawdown_pct: maxDrawdown,
      time_to_1_5x_minutes: number(checkpoint?.time_to_1_5x_minutes),
      time_to_2x_minutes: number(checkpoint?.time_to_2x_minutes),
      time_to_5x_minutes: number(checkpoint?.time_to_5x_minutes),
      hit_1_5x: maxReturn !== null ? Number(maxReturn >= 50) : null,
      hit_2x: hit2x,
      hit_5x: maxReturn !== null ? Number(maxReturn >= 400) : null,
      tradable_2x: hit2x === null ? null : Number(Boolean(hit2x && endpointLiquidity && endpointLiquidity >= liquidityFloor)),
      market_data_coverage_pct: number(checkpoint?.market_data_coverage_pct ?? raw.market_data_coverage_pct),
      largest_gap_minutes: number(checkpoint?.largest_gap_minutes ?? raw.largest_gap_minutes),
      status: timely ? "complete" : hasCheckpoint ? "partial" : "pending",
      source: text(raw.source) || "scanner_market_snapshot",
      error: hasCheckpoint && !timely ? "horizon observation missing or more than 1h late" : text(raw.error),
      updated_at: now,
      entry_verified: raw.entry_evidence_version === 2 && iso(raw.caught_at) === episode.caught_at
        && Boolean(positive(raw.caught_price_usd) || positive(raw.caught_mcap_usd)) ? 1 : 0,
    });
  }
  return rows;
}

async function runBatch(db, statements, size = 75) {
  for (let index = 0; index < statements.length; index += size) {
    await db.batch(statements.slice(index, index + size));
  }
}

async function archiveEvent(env, event, now) {
  if (!env?.RADAR_ARCHIVE || typeof env.RADAR_ARCHIVE.put !== "function") return null;
  const episode = normalizedEpisode(event?.episode, now);
  if (!episode) return null;
  const date = episode.caught_at.slice(0, 10).replace(/-/g, "/");
  const key = `signals/${date}/${episode.episode_id}/${historyEventId(event)}.json`;
  await env.RADAR_ARCHIVE.put(key, serialize(event), {
    httpMetadata: { contentType: "application/json" },
  });
  return key;
}

async function existingPriorScores(db, wallets, observedAt) {
  const addresses = [...new Set(wallets.map((row) => row.wallet_address).filter(Boolean))];
  const scores = new Map();
  const cutoff = iso(observedAt, null);
  if (!cutoff) return scores;
  for (let index = 0; index < addresses.length; index += D1_IN_PARAMETER_LIMIT - 1) {
    const chunk = addresses.slice(index, index + D1_IN_PARAMETER_LIMIT - 1);
    const placeholders = chunk.map((_, item) => `?${item + 1}`).join(", ");
    const result = await db.prepare(`
      SELECT wallet_address, edge_score, confidence, eligible_episodes, computed_through
      FROM wallet_scores
      WHERE wallet_address IN (${placeholders})
        AND numeric_contract_version >= 2
        AND computed_through IS NOT NULL
        AND computed_through <= ?${chunk.length + 1}
    `).bind(...chunk, cutoff).all();
    for (const row of result.results || []) scores.set(row.wallet_address, row);
  }
  return scores;
}

async function upsertEpisode(db, episode, now) {
  await db.prepare(`
    INSERT INTO signal_episodes (
      episode_id, token_address, pool_address, symbol, name, lane, signal_family,
      caught_at, last_signal_at, closed_at, caught_tier, caught_score, caught_price_usd,
      caught_mcap_usd, caught_liquidity_usd, token_age_days, ath_mcap_usd, ath_ratio,
      market_stage, mcap_band, liquidity_band, age_band, source_run_key, source_kind,
      data_quality_status, schema_version, raw_object_key, created_at, updated_at
    ) VALUES (
      ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16,
      ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29
    ) ON CONFLICT(episode_id) DO UPDATE SET
      pool_address = COALESCE(excluded.pool_address, signal_episodes.pool_address),
      symbol = COALESCE(excluded.symbol, signal_episodes.symbol),
      name = COALESCE(excluded.name, signal_episodes.name),
      last_signal_at = CASE WHEN excluded.last_signal_at > signal_episodes.last_signal_at THEN excluded.last_signal_at ELSE signal_episodes.last_signal_at END,
      closed_at = COALESCE(excluded.closed_at, signal_episodes.closed_at),
      data_quality_status = CASE WHEN excluded.data_quality_status = 'complete' THEN excluded.data_quality_status ELSE signal_episodes.data_quality_status END,
      raw_object_key = COALESCE(excluded.raw_object_key, signal_episodes.raw_object_key),
      updated_at = excluded.updated_at
  `).bind(
    episode.episode_id, episode.token_address, episode.pool_address, episode.symbol, episode.name,
    episode.lane, episode.signal_family, episode.caught_at, episode.last_signal_at, episode.closed_at,
    episode.caught_tier, episode.caught_score, episode.caught_price_usd, episode.caught_mcap_usd,
    episode.caught_liquidity_usd, episode.token_age_days, episode.ath_mcap_usd, episode.ath_ratio,
    episode.market_stage, episode.mcap_band, episode.liquidity_band, episode.age_band,
    episode.source_run_key, episode.source_kind, episode.data_quality_status, episode.schema_version,
    episode.raw_object_key, now, now,
  ).run();
}

async function upsertEpisodeEvent(db, eventId, episode, event, raw, now) {
  await db.prepare(`
    INSERT OR IGNORE INTO signal_episode_events (
      event_id, episode_id, observed_at, event_type, tier, score, price_usd,
      mcap_usd, liquidity_usd, retained_supply_pct, cohort_retained_pct,
      thesis_status, data_quality_status, payload_json, raw_object_key, created_at
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)
  `).bind(
    eventId, episode.episode_id, iso(event.observed_at, now), text(event.event_type) || "snapshot",
    text(event.tier), number(event.score), positive(event.price_usd), positive(event.mcap_usd),
    positive(event.liquidity_usd), number(event.retained_supply_pct), number(event.cohort_retained_pct),
    text(event.thesis_status), text(event.data_quality_status), serialize(raw), text(event.raw_object_key), now,
  ).run();
}

async function upsertWallets(db, episode, wallets, observedAt, priorScores, now) {
  const statements = [];
  for (const wallet of wallets) {
    const prior = priorScores.get(wallet.wallet_address) || {};
    statements.push(db.prepare(`
      INSERT INTO signal_wallets (
        episode_id, wallet_address, cohort_role, wallet_class_at_signal, first_buy_at, last_buy_at,
        buy_count, buy_sol, bought_tokens, average_entry_price, entry_mcap_usd, supply_pct_bought,
        held_tokens_at_catch, held_supply_pct_at_catch, retained_pct_at_catch, common_funder,
        common_executor, cluster_id_at_catch, prior_edge_score, prior_edge_confidence,
        prior_episode_count, prior_score_computed_through, evidence_status, raw_object_key, created_at, updated_at,numeric_contract_version
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26,2)
      ON CONFLICT(episode_id, wallet_address, cohort_role) DO UPDATE SET
        common_funder = COALESCE(excluded.common_funder, signal_wallets.common_funder),
        common_executor = COALESCE(excluded.common_executor, signal_wallets.common_executor),
        evidence_status = CASE WHEN excluded.evidence_status = 'complete' THEN 'complete' ELSE signal_wallets.evidence_status END,
        raw_object_key = COALESCE(excluded.raw_object_key, signal_wallets.raw_object_key),
        updated_at = excluded.updated_at
    `).bind(
      episode.episode_id, wallet.wallet_address, wallet.cohort_role, wallet.wallet_class_at_signal,
      wallet.first_buy_at, wallet.last_buy_at, wallet.buy_count, wallet.buy_sol, wallet.bought_tokens,
      wallet.average_entry_price, wallet.entry_mcap_usd, wallet.supply_pct_bought,
      wallet.held_tokens_at_catch, wallet.held_supply_pct_at_catch, wallet.retained_pct_at_catch,
      wallet.common_funder, wallet.common_executor, wallet.cluster_id_at_catch,
      number(prior.edge_score), text(prior.confidence), Number(prior.eligible_episodes) || 0,
      text(prior.computed_through), wallet.evidence_status, wallet.raw_object_key, now, now,
    ));
    statements.push(db.prepare(`
      INSERT OR IGNORE INTO wallet_observations (
        episode_id, wallet_address, observed_at, current_token_balance, balance_retained_pct,
        additional_buy_tokens, outbound_transfer_tokens, behavior_status, estimated_pnl_pct,
        estimated_pnl_sol, coverage_status, raw_object_key,numeric_contract_version
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12,2)
    `).bind(
      episode.episode_id, wallet.wallet_address, observedAt, wallet.observation.current_token_balance,
      wallet.observation.balance_retained_pct, wallet.observation.additional_buy_tokens,
      wallet.observation.outbound_transfer_tokens, wallet.observation.behavior_status,
      wallet.observation.estimated_pnl_pct, wallet.observation.estimated_pnl_sol,
      wallet.observation.coverage_status, wallet.observation.raw_object_key,
    ));
  }
  await runBatch(db, statements);
}

async function upsertOutcomes(db, episode, outcome, now, horizon = null) {
  const statements = normalizedOutcome(outcome, episode, now).filter(row => horizon === null || row.horizon_minutes === horizon).map((row) => db.prepare(`
    INSERT INTO signal_outcomes (
      episode_id, horizon_minutes, due_at, evaluated_at, endpoint_price_usd, endpoint_mcap_usd,
      endpoint_liquidity_usd, return_pct, max_return_pct, max_drawdown_pct, time_to_1_5x_minutes,
      time_to_2x_minutes, time_to_5x_minutes, hit_1_5x, hit_2x, hit_5x, tradable_2x,
      market_data_coverage_pct, largest_gap_minutes, status, source, error, updated_at,numeric_contract_version,entry_verified
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23,2,?24)
    ON CONFLICT(episode_id, horizon_minutes) DO UPDATE SET
      evaluated_at = COALESCE(excluded.evaluated_at, signal_outcomes.evaluated_at),
      endpoint_price_usd = COALESCE(excluded.endpoint_price_usd, signal_outcomes.endpoint_price_usd),
      endpoint_mcap_usd = COALESCE(excluded.endpoint_mcap_usd, signal_outcomes.endpoint_mcap_usd),
      endpoint_liquidity_usd = COALESCE(excluded.endpoint_liquidity_usd, signal_outcomes.endpoint_liquidity_usd),
      return_pct = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.return_pct ELSE signal_outcomes.return_pct END,
      max_return_pct = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.max_return_pct ELSE signal_outcomes.max_return_pct END,
      max_drawdown_pct = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.max_drawdown_pct ELSE signal_outcomes.max_drawdown_pct END,
      time_to_1_5x_minutes = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.time_to_1_5x_minutes ELSE signal_outcomes.time_to_1_5x_minutes END,
      time_to_2x_minutes = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.time_to_2x_minutes ELSE signal_outcomes.time_to_2x_minutes END,
      time_to_5x_minutes = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.time_to_5x_minutes ELSE signal_outcomes.time_to_5x_minutes END,
      hit_1_5x = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.hit_1_5x ELSE signal_outcomes.hit_1_5x END,
      hit_2x = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.hit_2x ELSE signal_outcomes.hit_2x END,
      hit_5x = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.hit_5x ELSE signal_outcomes.hit_5x END,
      tradable_2x = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.tradable_2x ELSE signal_outcomes.tradable_2x END,
      market_data_coverage_pct = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.market_data_coverage_pct ELSE signal_outcomes.market_data_coverage_pct END,
      largest_gap_minutes = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.largest_gap_minutes ELSE signal_outcomes.largest_gap_minutes END,
      status = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.status ELSE signal_outcomes.status END,
      source = COALESCE(excluded.source, signal_outcomes.source),
      error = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.error ELSE signal_outcomes.error END,
      numeric_contract_version = CASE WHEN excluded.evaluated_at IS NOT NULL THEN 2 ELSE signal_outcomes.numeric_contract_version END,
      entry_verified = CASE WHEN excluded.evaluated_at IS NOT NULL THEN excluded.entry_verified ELSE signal_outcomes.entry_verified END,
      updated_at = excluded.updated_at
  `).bind(
    episode.episode_id, row.horizon_minutes, row.due_at, row.evaluated_at, row.endpoint_price_usd,
    row.endpoint_mcap_usd, row.endpoint_liquidity_usd, row.return_pct, row.max_return_pct,
    row.max_drawdown_pct, row.time_to_1_5x_minutes, row.time_to_2x_minutes, row.time_to_5x_minutes,
    row.hit_1_5x, row.hit_2x, row.hit_5x, row.tradable_2x, row.market_data_coverage_pct,
    row.largest_gap_minutes, row.status, row.source, row.error, row.updated_at,row.entry_verified,
  ));
  await runBatch(db, statements);
}

async function upsertClusterEdges(db, episode, wallets, observedAt, now, selectedPairs = null) {
  const groups = new Map();
  for (const wallet of wallets) {
    for (const [type, value] of [["common_funder", wallet.common_funder], ["common_executor", wallet.common_executor]]) {
      if (!value || infrastructureSource(value, wallet, episode)) continue;
      const key = `${type}:${value}`;
      const group = groups.get(key) || { type, value, wallets: [] };
      group.wallets.push(wallet.wallet_address);
      groups.set(key, group);
    }
  }
  const statements = [];
  const edgeIds = [];
  function* pairs() {
    for (const group of groups.values()) {
      const members = [...new Set(group.wallets)].sort();
      for (let left = 0; left < members.length; left++) {
        for (let right = left + 1; right < members.length; right++) yield {...group, a:members[left], b:members[right]};
      }
    }
  }
  for (const group of selectedPairs || pairs()) {
        const edgeId = `edge:${hash(`${group.a}|${group.b}|${group.type}|${group.value}`)}`;
        edgeIds.push(edgeId);
        statements.push(db.prepare(`
          INSERT INTO wallet_cluster_edges (
            edge_id, wallet_a, wallet_b, relation_type, evidence_count, first_seen_at, last_seen_at,
            weight, evidence_json, created_at, updated_at
          ) VALUES (?1, ?2, ?3, ?4, 0, ?5, ?5, 0, ?6, ?7, ?7)
          ON CONFLICT(edge_id) DO UPDATE SET
            last_seen_at = CASE WHEN excluded.last_seen_at > wallet_cluster_edges.last_seen_at THEN excluded.last_seen_at ELSE wallet_cluster_edges.last_seen_at END,
            updated_at = excluded.updated_at
        `).bind(edgeId, group.a, group.b, group.type, observedAt, serialize({ value: group.value, episode_id: episode.episode_id }), now));
        // A retried outbox event represents the same episode, not fresh
        // independent evidence. This table makes edge strength idempotent.
        statements.push(db.prepare(`
          INSERT OR IGNORE INTO wallet_cluster_edge_evidence (
            edge_id, episode_id, observed_at, evidence_json, created_at
          ) VALUES (?1, ?2, ?3, ?4, ?5)
        `).bind(edgeId, episode.episode_id, observedAt, serialize({ value: group.value, relation_type: group.type }), now));
  }
  await runBatch(db, statements);
  const uniqueEdges = [...new Set(edgeIds)];
  await runBatch(db, uniqueEdges.map((edgeId) => db.prepare(`
    UPDATE wallet_cluster_edges
    SET evidence_count = (
          SELECT COUNT(*) FROM wallet_cluster_edge_evidence evidence
          WHERE evidence.edge_id = wallet_cluster_edges.edge_id
        ),
        weight = (
          SELECT COUNT(*) FROM wallet_cluster_edge_evidence evidence
          WHERE evidence.edge_id = wallet_cluster_edges.edge_id
        )
    WHERE edge_id = ?1
  `).bind(edgeId)));
}

async function refreshMarketBaselines(db, now, episode = null, horizon = null) {
  const result = await db.prepare(`
    SELECT e.mcap_band, e.liquidity_band, e.age_band, e.signal_family, o.horizon_minutes,
      COUNT(*) AS eligible_episodes,
      AVG(CASE WHEN o.max_return_pct >= 50 THEN 1.0 ELSE 0.0 END) AS hit_1_5x_rate,
      AVG(CASE WHEN o.max_return_pct >= 100 THEN 1.0 ELSE 0.0 END) AS hit_2x_rate,
      AVG(CASE WHEN o.max_return_pct >= 400 THEN 1.0 ELSE 0.0 END) AS hit_5x_rate
    FROM signal_episodes e
    JOIN signal_outcomes o ON o.episode_id = e.episode_id
    WHERE o.status = 'complete'
      AND o.numeric_contract_version >= 2 AND o.entry_verified=1
      AND (julianday(o.evaluated_at) - julianday(o.due_at)) * 86400 BETWEEN 0 AND 3600.01
      ${episode ? "AND e.mcap_band=?1 AND e.liquidity_band=?2 AND e.age_band=?3 AND e.signal_family=?4 AND o.horizon_minutes=?5" : ""}
    GROUP BY e.mcap_band, e.liquidity_band, e.age_band, e.signal_family, o.horizon_minutes
  `).bind(...(episode ? [episode.mcap_band, episode.liquidity_band, episode.age_band, episode.signal_family, horizon] : [])).all();
  if (episode && !result.results?.length) {
    await db.prepare(`UPDATE market_baselines SET numeric_contract_version=1 WHERE mcap_band=?1
      AND liquidity_band=?2 AND age_band=?3 AND signal_family=?4 AND horizon_minutes=?5`)
      .bind(episode.mcap_band,episode.liquidity_band,episode.age_band,episode.signal_family,horizon).run();
  }
  const statements = (result.results || []).map((row) => {
    const key = [row.mcap_band, row.liquidity_band, row.age_band, row.signal_family, row.horizon_minutes].join("|");
    return db.prepare(`
      INSERT INTO market_baselines (
        baseline_key, mcap_band, liquidity_band, age_band, signal_family, horizon_minutes,
        eligible_episodes, hit_1_5x_rate, hit_2x_rate, hit_5x_rate,
        median_max_return_pct, median_drawdown_pct, computed_through, updated_at,numeric_contract_version
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, NULL, NULL, ?11, ?11,2)
      ON CONFLICT(baseline_key) DO UPDATE SET
        eligible_episodes = excluded.eligible_episodes,
        hit_1_5x_rate = excluded.hit_1_5x_rate,
        hit_2x_rate = excluded.hit_2x_rate,
        hit_5x_rate = excluded.hit_5x_rate,
        computed_through = excluded.computed_through,
        numeric_contract_version = 2,
        updated_at = excluded.updated_at
    `).bind(key, row.mcap_band, row.liquidity_band, row.age_band, row.signal_family, row.horizon_minutes,
      Number(row.eligible_episodes) || 0, number(row.hit_1_5x_rate, 0), number(row.hit_2x_rate, 0), number(row.hit_5x_rate, 0), now);
  });
  await runBatch(db, statements);
}

async function baselineRates(db) {
  const result = await db.prepare(`
    SELECT mcap_band, liquidity_band, age_band, signal_family, horizon_minutes, hit_2x_rate
    FROM market_baselines
    WHERE horizon_minutes = 4320 AND numeric_contract_version >= 2
  `).all();
  const rows = new Map();
  for (const row of result.results || []) {
    rows.set([row.mcap_band, row.liquidity_band, row.age_band, row.signal_family].join("|"), number(row.hit_2x_rate, 0));
  }
  return rows;
}

function confidenceFor({ episodes, tokens, wins, lift }) {
  if (episodes >= 10 && tokens >= 5 && wins >= 3 && lift >= 1.5) return "validated";
  if (episodes >= 5 && tokens >= 3 && wins >= 2) return "emerging";
  return "unproven";
}

export function edgeScore({ episodes = 0, tokens = 0, lift = 0 }) {
  const liftFactor = clamp((lift - 1) / 1.5);
  const sampleFactor = clamp(episodes / 10);
  const diversityFactor = clamp(tokens / 5);
  return Math.round(100 * (0.6 * liftFactor + 0.25 * sampleFactor + 0.15 * diversityFactor));
}

async function refreshWalletScores(db, walletAddresses, now) {
  const unique = [...new Set(walletAddresses.filter(Boolean))];
  if (!unique.length) return 0;
  const baselines = await baselineRates(db);
  let updated = 0;
  for (const wallet of unique) {
    const result = await db.prepare(`
      SELECT e.episode_id, e.token_address, e.caught_at, e.mcap_band, e.liquidity_band, e.age_band,
        e.signal_family, o.max_return_pct, o.max_drawdown_pct, o.time_to_2x_minutes,
        o.time_to_1_5x_minutes, o.horizon_minutes
      FROM signal_wallets w
      JOIN signal_episodes e ON e.episode_id = w.episode_id
      JOIN signal_outcomes o ON o.episode_id = e.episode_id
      WHERE w.wallet_address = ?1
        AND w.cohort_role = 'at_catch'
        AND o.horizon_minutes = 4320
        AND o.status = 'complete'
        AND o.numeric_contract_version >= 2 AND o.entry_verified=1
        AND (julianday(o.evaluated_at) - julianday(o.due_at)) * 86400 BETWEEN 0 AND 3600.01
    `).bind(wallet).all();
    const rows = result.results || [];
    if (!rows.length) {
      await db.prepare("UPDATE wallet_scores SET numeric_contract_version=1 WHERE wallet_address=?1").bind(wallet).run();
      continue;
    }
    const episodes = rows.length;
    const tokens = new Set(rows.map((row) => row.token_address)).size;
    const wins = rows.filter((row) => number(row.max_return_pct, -Infinity) >= 100).length;
    const wins15 = rows.filter((row) => number(row.max_return_pct, -Infinity) >= 50).length;
    const baseline = median(rows.map((row) => baselines.get([row.mcap_band, row.liquidity_band, row.age_band, row.signal_family].join("|")) ?? 0));
    const baselineRate = Math.max(0.01, baseline ?? 0.01);
    const rawRate = wins / episodes;
    const bayesianRate = (wins + PRIOR_STRENGTH * baselineRate) / (episodes + PRIOR_STRENGTH);
    const lift = bayesianRate / baselineRate;
    const score = edgeScore({ episodes, tokens, lift });
    const confidence = confidenceFor({ episodes, tokens, wins, lift });
    const maxReturns = rows.map((row) => number(row.max_return_pct)).filter((value) => value !== null);
    const drawdowns = rows.map((row) => number(row.max_drawdown_pct)).filter((value) => value !== null);
    const leads = rows.map((row) => number(row.time_to_2x_minutes ?? row.time_to_1_5x_minutes)).filter((value) => value !== null);
    const firstSeen = [...rows].sort((left, right) => String(left.caught_at).localeCompare(String(right.caught_at)))[0]?.caught_at || now;
    const lastSeen = [...rows].sort((left, right) => String(right.caught_at).localeCompare(String(left.caught_at)))[0]?.caught_at || now;
    await db.prepare(`
      INSERT INTO wallet_scores (
        wallet_address, eligible_episodes, distinct_tokens, wins_1_5x_72h, wins_2x_72h,
        wins_5x_7d, raw_hit_rate_2x, baseline_hit_rate_2x, bayesian_hit_rate_2x,
        lift_2x, median_lead_minutes, median_retained_24h, median_max_return_72h,
        median_drawdown_72h, edge_score, confidence, first_seen_at, last_seen_at,
        computed_through, score_version, updated_at,numeric_contract_version
      ) VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, ?7, ?8, ?9, ?10, NULL, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?17,2)
      ON CONFLICT(wallet_address) DO UPDATE SET
        eligible_episodes = excluded.eligible_episodes,
        distinct_tokens = excluded.distinct_tokens,
        wins_1_5x_72h = excluded.wins_1_5x_72h,
        wins_2x_72h = excluded.wins_2x_72h,
        raw_hit_rate_2x = excluded.raw_hit_rate_2x,
        baseline_hit_rate_2x = excluded.baseline_hit_rate_2x,
        bayesian_hit_rate_2x = excluded.bayesian_hit_rate_2x,
        lift_2x = excluded.lift_2x,
        median_lead_minutes = excluded.median_lead_minutes,
        median_max_return_72h = excluded.median_max_return_72h,
        median_drawdown_72h = excluded.median_drawdown_72h,
        edge_score = excluded.edge_score,
        confidence = excluded.confidence,
        first_seen_at = excluded.first_seen_at,
        last_seen_at = excluded.last_seen_at,
        computed_through = excluded.computed_through,
        score_version = excluded.score_version,
        numeric_contract_version = 2,
        updated_at = excluded.updated_at
    `).bind(
      wallet, episodes, tokens, wins15, wins, rawRate, baselineRate, bayesianRate, lift,
      median(leads), median(maxReturns), median(drawdowns), score, confidence,
      firstSeen, lastSeen, now, SCORE_VERSION,
    ).run();
    updated += 1;
  }
  return updated;
}

async function refreshClusters(db, walletAddresses, now) {
  const wallets = [...new Set(walletAddresses.filter(Boolean))];
  if (!wallets.length) return 0;
  const state = {};
  const job = `legacy:${hash(wallets.sort().join("|") + now)}`;
  while (!await stepClusterJob(db, job, state, wallets, now)) { /* Direct ingestion has no durable queue cursor. */ }
  return 1;
}

async function ingestHistoryEvent(env, rawEvent, now = nowIso(), derived = null) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const episode = normalizedEpisode(rawEvent?.episode, now);
  if (!episode) throw new Error("history_episode_token_required");
  const event = rawEvent?.event && typeof rawEvent.event === "object" ? rawEvent.event : {};
  const effects = historyEventEffects(event.event_type);
  const observedAt = iso(event.observed_at, episode.last_signal_at || now);
  const wallets = (Array.isArray(rawEvent?.wallets) ? rawEvent.wallets : [])
    .map((row) => normalizedWallet(row, episode))
    .filter(Boolean);
  const eventId = historyEventId({ ...rawEvent, episode, event: { ...event, observed_at: observedAt } });
  const archiveKey = await archiveEvent(env, rawEvent, now).catch(() => null);
  if (archiveKey) {
    episode.raw_object_key = archiveKey;
    event.raw_object_key = archiveKey;
  }
  await upsertEpisode(env.RADAR_HISTORY_DB, episode, now);
  await upsertEpisodeEvent(env.RADAR_HISTORY_DB, eventId, episode, { ...event, observed_at: observedAt }, rawEvent, now);
  // Prior edge belongs to the original catch only. A later balance recheck
  // must not rewrite what was knowable when this cohort first appeared.
  const priors = effects.capturesPriorScore
    ? await existingPriorScores(env.RADAR_HISTORY_DB, wallets, observedAt)
    : new Map();
  await upsertWallets(env.RADAR_HISTORY_DB, episode, wallets, observedAt, priors, now);
  // A signal initializes pending horizons. Only an explicit frozen horizon
  // result changes performance baselines or wallet scores.
  if (effects.updatesOutcomes) {
    await upsertOutcomes(env.RADAR_HISTORY_DB, episode, rawEvent?.outcome || {}, now);
  }
  // Rechecks reuse the cohort to record balances. They are not independent
  // relationship evidence and must never increase a cluster's edge weight.
  if (effects.recordsClusterEdge) {
    await upsertClusterEdges(env.RADAR_HISTORY_DB, episode, wallets, observedAt, now);
  }

  let relatedWallets = wallets.map((wallet) => wallet.wallet_address);
  let scoresUpdated = 0;
  if (effects.refreshesScores) {
    // Outcome events intentionally carry no fresh wallet observation. Resolve
    // every member of their episode nevertheless, so a result refreshes the
    // cohort's learned record instead of an empty event wallet list.
    const episodeWalletRows = await env.RADAR_HISTORY_DB.prepare(`
      SELECT DISTINCT wallet_address FROM signal_wallets
      WHERE episode_id = ?1 AND cohort_role = 'at_catch'
    `).bind(episode.episode_id).all();
    relatedWallets = (episodeWalletRows.results || []).map((row) => row.wallet_address);
    if (derived) {
      derived.refreshBaseline = true;
      relatedWallets.forEach((wallet) => derived.scoreWallets.add(wallet));
    } else {
      await refreshMarketBaselines(env.RADAR_HISTORY_DB, now);
      scoresUpdated = await refreshWalletScores(env.RADAR_HISTORY_DB, relatedWallets, now);
    }
  }
  if (derived && effects.refreshesClusters) relatedWallets.forEach((wallet) => derived.clusterWallets.add(wallet));
  const clustersUpdated = effects.refreshesClusters && !derived
    ? await refreshClusters(env.RADAR_HISTORY_DB, relatedWallets, now)
    : 0;
  return { event_id: eventId, episode_id: episode.episode_id, wallets: wallets.length, scores_updated: scoresUpdated, clusters_updated: clustersUpdated };
}

export function createHistoryDerivedBatch() {
  return { refreshBaseline: false, scoreWallets: new Set(), clusterWallets: new Set() };
}

export async function refreshHistoryDerivedBatch(env, derived, now = nowIso()) {
  if (derived.refreshBaseline) await refreshMarketBaselines(env.RADAR_HISTORY_DB, now);
  await refreshWalletScores(env.RADAR_HISTORY_DB, [...derived.scoreWallets], now);
  if (derived.clusterWallets.size) await refreshClusters(env.RADAR_HISTORY_DB, [...derived.clusterWallets], now);
}

// The optional write hook reserves quota before each single-row upsert/batch.
// This adapter deliberately has no operational RADAR_DB or archive dependency.
function checkedHistoryDb(db, onWrite, onQuery = () => {}) {
  const check = result => {
    if (result?.success === false) throw new Error(result.error || "history_d1_write_failed");
    return result;
  };
  const wrap = statement => ({
    statement,
    bind(...values) { return wrap(statement.bind(...values)); },
    async run() { onQuery(); onWrite(1); return check(await statement.run()); },
    async all() { onQuery(); return check(await statement.all()); },
    async first(...args) { onQuery(); return statement.first(...args); },
  });
  return {
    prepare(sql) { return wrap(db.prepare(sql)); },
    async batch(statements) {
      onQuery();
      onWrite(statements.length);
      const results = await db.batch(statements.map(item => item.statement));
      if (!Array.isArray(results) || results.length !== statements.length) throw new Error("history_d1_batch_incomplete");
      results.forEach(check);
      return results;
    },
  };
}

export async function ingestHistoryBatch(env, events, { now = nowIso(), onWrite = () => {} } = {}) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  if (!Array.isArray(events) || events.length > 25) throw new Error("history_batch_max_25");
  const historyEnv = { RADAR_HISTORY_DB: checkedHistoryDb(env.RADAR_HISTORY_DB, onWrite) };
  const derived = createHistoryDerivedBatch();
  const ingested = [];
  const failed = [];
  for (const event of events) {
    const perEvent = createHistoryDerivedBatch();
    try {
      const result = await ingestHistoryEvent(historyEnv, event, now, perEvent);
      ingested.push(result);
      derived.refreshBaseline ||= perEvent.refreshBaseline;
      perEvent.scoreWallets.forEach(wallet => derived.scoreWallets.add(wallet));
      perEvent.clusterWallets.forEach(wallet => derived.clusterWallets.add(wallet));
    } catch (error) {
      failed.push({ event_id: historyEventId(event), error: String(error?.message || error).slice(0, 500) });
    }
  }
  // A throw here means NONE of the ingested events may be acknowledged.
  await refreshHistoryDerivedBatch(historyEnv, derived, now);
  return { ingested, failed };
}

export function historyEventsFromPayload(payload = {}) {
  const ledger = payload?.history_ledger;
  const events = Array.isArray(ledger?.events) ? ledger.events : [];
  return events.filter((event) => event && typeof event === "object" && event.episode);
}

export async function enqueueHistoryEvents(env, payload, now = nowIso()) {
  if (!env?.RADAR_DB || typeof env.RADAR_DB.prepare !== "function") return { queued: 0, enabled: false };
  const events = [...historyEventsFromPayload(payload)].sort((left, right) => {
    const leftAt = iso(left?.event?.observed_at, "9999-12-31T23:59:59.999Z");
    const rightAt = iso(right?.event?.observed_at, "9999-12-31T23:59:59.999Z");
    return leftAt.localeCompare(rightAt) || historyEventId(left).localeCompare(historyEventId(right));
  });
  const statements = events.map((event) => {
    const id = historyEventId(event);
    const type = text(event?.event?.event_type) || "snapshot";
    return env.RADAR_DB.prepare(`
      INSERT INTO history_outbox (event_id, event_type, payload_json, status, attempts, next_attempt_at, delivered_at, last_error, created_at, updated_at)
      VALUES (?1, ?2, ?3, 'pending', 0, ?4, NULL, NULL, ?4, ?4)
      ON CONFLICT(event_id) DO UPDATE SET
        payload_json = CASE WHEN history_outbox.status = 'delivered' THEN history_outbox.payload_json ELSE excluded.payload_json END,
        updated_at = excluded.updated_at
    `).bind(id, type, serialize(event), now);
  });
  await runBatch(env.RADAR_DB, statements);
  return { queued: statements.length, enabled: true };
}

function retryAt(now, attempts) {
  const minutes = Math.min(60, Math.max(1, 2 ** Math.min(5, attempts)));
  return new Date(new Date(now).getTime() + minutes * 60_000).toISOString();
}

export async function flushHistoryOutbox(env, { limit = OUTBOX_BATCH_SIZE } = {}) {
  if (!env?.RADAR_DB || typeof env.RADAR_DB.prepare !== "function") return { enabled: false, delivered: 0, pending: 0 };
  if (!hasHistoryDb(env)) return { enabled: false, delivered: 0, pending: 0, error: "history_db_not_configured" };
  const now = nowIso();
  const page = await env.RADAR_DB.prepare(`
    SELECT event_id, payload_json, attempts
    FROM history_outbox
    WHERE status = 'pending' AND next_attempt_at <= ?1
    ORDER BY next_attempt_at ASC, created_at ASC
    LIMIT ?2
  `).bind(now, Math.max(1, Math.min(50, Number(limit) || OUTBOX_BATCH_SIZE))).all();
  if (env.HISTORY_QUEUE) {
    // Forward legacy payloads intact. A durable enqueue is NOT an archive
    // acknowledgement: retain the old pending row until its receipt is delivered.
    const {enqueueDurableHistory,durableHistoryReceipts}=await import("./runtime-history.js");
    const rows=(page.results || []).slice(0,25);
    if (rows.length) {
      await enqueueDurableHistory(env,{history_ledger:{events:rows.map(row=>parsePayload(row.payload_json))}});
    }
    const {receipts}=await durableHistoryReceipts(env,rows.map(row=>row.event_id));
    const delivered=receipts.filter(row=>row.status==="delivered");
    await runBatch(env.RADAR_DB,delivered.map(row=>env.RADAR_DB.prepare(`UPDATE history_outbox
      SET status='delivered',delivered_at=?2,updated_at=?2,last_error=NULL WHERE event_id=?1`).bind(row.event_id,now)));
    return {enabled:true,forwarded:rows.length,delivered:delivered.length,pending:rows.length-delivered.length,
      storage_source:"durable_history_queue",pending_capped:rows.length>0};
  }
  let delivered = 0;
  let failed = 0;
  const derived = { refreshBaseline: false, scoreWallets: new Set(), clusterWallets: new Set() };
  const ingested = [];
  for (const row of page.results || []) {
    try {
      await ingestHistoryEvent(env, parsePayload(row.payload_json, {}), now, derived);
      ingested.push(row);
    } catch (error) {
      const attempts = (Number(row.attempts) || 0) + 1;
      await env.RADAR_DB.prepare(`
        UPDATE history_outbox
        SET status = 'pending', attempts = ?2, next_attempt_at = ?3, last_error = ?4, updated_at = ?5
        WHERE event_id = ?1
      `).bind(row.event_id, attempts, retryAt(now, attempts), String(error?.message || error).slice(0, 500), now).run();
      failed += 1;
    }
  }
  // Compute derived analytics once per batch, not a full-table baseline per event.
  // Leave events pending if derived writes fail so the whole operation is retryable.
  if (derived.refreshBaseline) await refreshMarketBaselines(env.RADAR_HISTORY_DB, now);
  await refreshWalletScores(env.RADAR_HISTORY_DB, [...derived.scoreWallets], now);
  if (derived.clusterWallets.size) await refreshClusters(env.RADAR_HISTORY_DB, [...derived.clusterWallets], now);
  await runBatch(env.RADAR_DB, ingested.map((row) => env.RADAR_DB.prepare(`
    UPDATE history_outbox SET status = 'delivered', delivered_at = ?2, updated_at = ?2, last_error = NULL
    WHERE event_id = ?1
  `).bind(row.event_id, now)));
  delivered = ingested.length;
  const pending = await env.RADAR_DB.prepare("SELECT COUNT(*) AS count FROM (SELECT 1 FROM history_outbox WHERE status = 'pending' LIMIT 1001)").first();
  const status = {
    updated_at: now,
    last_flush_at: now,
    delivered,
    failed,
    pending: Number(pending?.count) || 0,
    pending_capped: Number(pending?.count) >= 1001,
    history_db_configured: true,
    archive_configured: Boolean(env?.RADAR_ARCHIVE),
  };
  await env.RADAR_DB.prepare(`
    INSERT INTO state_docs (key, payload_json, source_updated_at, updated_at)
    VALUES ('history_status', ?1, ?2, ?2)
    ON CONFLICT(key) DO UPDATE SET payload_json = excluded.payload_json, source_updated_at = excluded.source_updated_at, updated_at = excluded.updated_at
  `).bind(serialize(status), now).run();
  return { enabled: true, ...status };
}

function validWindow(value) {
  return ["30d", "90d", "all"].includes(value) ? value : "90d";
}

function windowSince(window) {
  if (window === "all") return null;
  const days = window === "30d" ? 30 : 90;
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

function qualityCounts(rows) {
  const counts = { complete: 0, partial: 0, pending: 0, unavailable: 0 };
  for (const row of rows || []) counts[row.status] = (counts[row.status] || 0) + 1;
  return counts;
}

function publicOutcomeRow(row, fields, statusField) {
  const trusted=row.outcome_numeric_version>=2 && row.outcome_entry_verified===1;
  const projected={...row,outcome_trusted:trusted};
  if (!trusted) {
    for (const field of fields) projected[field]=null;
    if (projected[statusField]==="complete") projected[statusField]="legacy_unverified";
  }
  return projected;
}

export async function historyOverview(env, windowValue = "90d") {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const window = validWindow(windowValue);
  const since = windowSince(window);
  const condition = since ? "WHERE e.caught_at >= ?1" : "";
  const binds = since ? [since] : [];
  const [episodes, outcomes, wallets, clusters] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare(`SELECT COUNT(*) AS count FROM signal_episodes e ${condition}`).bind(...binds).first(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT
        o.episode_id,
        CASE WHEN o.numeric_contract_version<2 OR o.entry_verified<>1 THEN 'legacy_unverified' ELSE o.status END status,
        o.max_return_pct,
        o.tradable_2x,
        MAX(CASE WHEN w.prior_edge_confidence = 'validated' AND w.numeric_contract_version>=2 THEN 1 ELSE 0 END) AS has_validated_edge
      FROM signal_outcomes o
      JOIN signal_episodes e ON e.episode_id = o.episode_id
      LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
      WHERE o.horizon_minutes = 4320 ${since ? "AND e.caught_at >= ?1" : ""}
      GROUP BY o.episode_id
    `).bind(...binds).all(),
    env.RADAR_HISTORY_DB.prepare(`SELECT COUNT(*) AS count FROM wallet_scores WHERE confidence != 'unproven' AND numeric_contract_version>=2`).first(),
    env.RADAR_HISTORY_DB.prepare(`SELECT COUNT(*) AS count FROM wallet_clusters WHERE confidence != 'unproven' AND active=1 AND numeric_contract_version>=2`).first(),
  ]);
  const rows = outcomes.results || [];
  const complete = rows.filter((row) => row.status === "complete" && row.max_return_pct !== null);
  const winners = complete.filter((row) => Number(row.max_return_pct) >= 100 && Number(row.tradable_2x) === 1);
  const confirmed = complete.filter((row) => Number(row.has_validated_edge) === 1);
  const confirmedWinners = confirmed.filter((row) => Number(row.max_return_pct) >= 100 && Number(row.tradable_2x) === 1);
  const overallPrecision = complete.length ? winners.length / complete.length : null;
  const edgePrecision = confirmed.length ? confirmedWinners.length / confirmed.length : null;
  const quality = qualityCounts(rows);
  return {
    ok: true,
    window,
    episodes: Number(episodes?.count) || 0,
    resolved_72h: complete.length,
    pending_72h: quality.pending || 0,
    partial_72h: quality.partial || 0,
    precision_2x_72h: overallPrecision,
    edge_precision_2x_72h: edgePrecision,
    edge_lift: overallPrecision && edgePrecision !== null ? edgePrecision / overallPrecision : null,
    emerging_or_validated_wallets: Number(wallets?.count) || 0,
    emerging_or_validated_clusters: Number(clusters?.count) || 0,
    outcome_quality: quality,
    numeric_contract_version: 2,
    legacy_unverified_72h: quality.legacy_unverified || 0,
    history_fresh_at: nowIso(),
    shadow_mode: true,
  };
}

function decodeCursor(value) {
  if (!value) return null;
  try {
    const normalized = String(value).replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(padded), (char) => char.charCodeAt(0))));
  } catch {
    return null;
  }
}

function encodeCursor(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function historyWallets(env, query = {}) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const limit = Math.max(1, Math.min(100, Number(query.limit) || 50));
  const confidence = ["unproven", "emerging", "validated"].includes(query.confidence) ? query.confidence : null;
  const minLift = Math.max(0, Number(query.min_lift) || 0);
  const minSample = Math.max(0, Number(query.min_sample) || 0);
  const cursor = decodeCursor(query.cursor);
  if (query.cursor && !cursor) throw new Error("wallet_cursor_invalid_or_obsolete");
  const where = ["numeric_contract_version>=2", "edge_score >= ?1", "eligible_episodes >= ?2"];
  const binds = [0, minSample];
  if (confidence) { where.push(`confidence = ?${binds.length + 1}`); binds.push(confidence); }
  if (minLift) { where.push(`lift_2x >= ?${binds.length + 1}`); binds.push(minLift); }
  if (cursor) {
    if (cursor.v !== 2 || !Number.isFinite(cursor.score) || !Number.isFinite(cursor.sample)
        || (cursor.lift !== null && !Number.isFinite(cursor.lift)) || typeof cursor.wallet !== "string") {
      throw new Error("wallet_cursor_invalid_or_obsolete");
    }
    const [s, l, n, w] = [1, 2, 3, 4].map(offset => `?${binds.length + offset}`);
    const lowerLift = cursor.lift === null ? "0" : `(lift_2x < ${l} OR lift_2x IS NULL)`;
    where.push(`(edge_score < ${s} OR (edge_score = ${s} AND (${lowerLift}
      OR (lift_2x IS ${l} AND (eligible_episodes < ${n}
        OR (eligible_episodes = ${n} AND wallet_address > ${w}))))))`);
    binds.push(cursor.score, cursor.lift, cursor.sample, cursor.wallet);
  }
  binds.push(limit + 1);
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT wallet_address, eligible_episodes, distinct_tokens, wins_1_5x_72h, wins_2x_72h,
      raw_hit_rate_2x, baseline_hit_rate_2x, bayesian_hit_rate_2x, lift_2x,
      median_lead_minutes, median_retained_24h, median_max_return_72h,
      median_drawdown_72h, edge_score, confidence, first_seen_at, last_seen_at, computed_through,numeric_contract_version
    FROM wallet_scores
    WHERE ${where.join(" AND ")}
    ORDER BY edge_score DESC, lift_2x DESC, eligible_episodes DESC, wallet_address ASC
    LIMIT ?${binds.length}
  `).bind(...binds).all();
  const rows = (result.results || []).slice(0, limit);
  const hasMore = (result.results || []).length > limit;
  const last = rows.at(-1);
  return { ok: true, rows, next_cursor: hasMore && last ? encodeCursor({ v: 2, score: last.edge_score,
    lift: last.lift_2x, sample: last.eligible_episodes, wallet: last.wallet_address }) : null };
}

export async function historyWalletDetail(env, wallet) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const address = text(wallet);
  if (!address) throw new Error("wallet_required");
  const [score, episodes, observations, clusters] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM wallet_scores WHERE wallet_address = ?1").bind(address).first(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT e.episode_id, e.token_address, e.symbol, e.caught_at, e.caught_mcap_usd, e.caught_tier,
        e.signal_family, w.buy_sol, w.bought_tokens,
        CASE WHEN w.numeric_contract_version>=2 THEN w.prior_edge_confidence END prior_edge_confidence,
        o.max_return_pct, o.max_drawdown_pct, o.return_pct, o.status,
        o.numeric_contract_version outcome_numeric_version,o.entry_verified outcome_entry_verified
      FROM signal_wallets w
      JOIN signal_episodes e ON e.episode_id = w.episode_id
      LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
      WHERE w.wallet_address = ?1 AND w.cohort_role = 'at_catch'
      ORDER BY e.caught_at DESC LIMIT 100
    `).bind(address).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT episode_id, observed_at, current_token_balance, balance_retained_pct, behavior_status, coverage_status,numeric_contract_version
      FROM wallet_observations legacy WHERE wallet_address = ?1 AND (numeric_contract_version>=2 OR NOT EXISTS (
        SELECT 1 FROM wallet_observation_bundles b,json_each(b.observations_json) j WHERE b.episode_id=legacy.episode_id
          AND b.observed_at=legacy.observed_at AND json_extract(j.value,'$.wallet_address')=legacy.wallet_address))
      UNION ALL
      SELECT b.episode_id,b.observed_at,json_extract(j.value,'$.current_token_balance'),
        json_extract(j.value,'$.balance_retained_pct'),json_extract(j.value,'$.behavior_status'),json_extract(j.value,'$.coverage_status'),b.numeric_contract_version
      FROM wallet_observation_bundles b,json_each(b.observations_json) j
      WHERE json_extract(j.value,'$.wallet_address')=?1 AND NOT EXISTS (
        SELECT 1 FROM wallet_observations legacy WHERE legacy.episode_id=b.episode_id
          AND legacy.observed_at=b.observed_at AND legacy.wallet_address=?1 AND legacy.numeric_contract_version>=2)
      ORDER BY observed_at DESC LIMIT 100
    `).bind(address).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT c.cluster_id, c.confidence, c.wallet_count, c.lift_2x, c.edge_score, c.relation_types_json
      FROM wallet_cluster_members m JOIN wallet_clusters c ON c.cluster_id = m.cluster_id
      WHERE m.wallet_address = ?1 AND c.active=1 AND c.numeric_contract_version>=2 ORDER BY c.edge_score DESC
    `).bind(address).all(),
  ]);
  if (!score && !(episodes.results || []).length) throw new Error("wallet_not_found");
  return { ok: true, wallet: address, score: score ? {...score,trusted:score.numeric_contract_version>=2} : null,
    episodes: (episodes.results || []).map(row=>publicOutcomeRow(row,["max_return_pct","max_drawdown_pct","return_pct"],"status")),
    observations: (observations.results || []).map(row=>({...row,trusted:row.numeric_contract_version>=2})),
    clusters: (clusters.results || []).map((row) => ({ ...row, relation_types: parsePayload(row.relation_types_json, []) })) };
}

export async function historyClusters(env, query = {}) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const limit = Math.max(1, Math.min(100, Number(query.limit) || 50));
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT cluster_id, confidence, wallet_count, eligible_episodes, wins_2x_72h, lift_2x,
      edge_score, relation_types_json, first_seen_at, last_seen_at, computed_through,numeric_contract_version
    FROM wallet_clusters WHERE active=1 AND numeric_contract_version>=2 ORDER BY edge_score DESC, lift_2x DESC, wallet_count DESC LIMIT ?1
  `).bind(limit).all();
  return { ok: true, rows: (result.results || []).map((row) => ({ ...row, relation_types: parsePayload(row.relation_types_json, []) })) };
}

export async function historyClusterDetail(env, clusterId) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const id = text(clusterId);
  if (!id) throw new Error("cluster_required");
  const [cluster, members, edges] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM wallet_clusters WHERE cluster_id = ?1").bind(id).first(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT m.wallet_address, m.first_seen_at, m.last_seen_at, m.membership_weight, s.edge_score, s.confidence, s.lift_2x, s.eligible_episodes
      FROM wallet_cluster_members m LEFT JOIN wallet_scores s ON s.wallet_address = m.wallet_address
      WHERE m.cluster_id = ?1 ORDER BY s.edge_score DESC
    `).bind(id).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT e.* FROM wallet_cluster_edges e
      WHERE e.is_infrastructure=0 AND e.wallet_a IN (SELECT wallet_address FROM wallet_cluster_members WHERE cluster_id = ?1)
        AND e.wallet_b IN (SELECT wallet_address FROM wallet_cluster_members WHERE cluster_id = ?1)
      ORDER BY e.weight DESC LIMIT 100
    `).bind(id).all(),
  ]);
  if (!cluster) throw new Error("cluster_not_found");
  return { ok: true, cluster: { ...cluster,trusted:cluster.numeric_contract_version>=2, relation_types: parsePayload(cluster.relation_types_json, []) }, members: members.results || [], edges: (edges.results || []).map((row) => ({ ...row, evidence: parsePayload(row.evidence_json, {}) })) };
}

export async function historyEpisodes(env, query = {}) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const limit = Math.max(1, Math.min(100, Number(query.limit) || 50));
  const window = validWindow(query.window);
  const since = windowSince(window);
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT e.episode_id, e.token_address, e.pool_address, e.symbol, e.name, e.caught_at,
      e.caught_mcap_usd, e.caught_tier, e.signal_family, e.data_quality_status,
      o.return_pct AS return_72h, o.max_return_pct AS max_return_72h,
      o.max_drawdown_pct AS max_drawdown_72h, o.status AS outcome_status,
      o.numeric_contract_version outcome_numeric_version,o.entry_verified outcome_entry_verified,
      MAX(CASE WHEN w.prior_edge_confidence = 'validated' AND w.numeric_contract_version>=2 THEN 1 ELSE 0 END) AS has_validated_edge,
      MAX(CASE WHEN w.prior_edge_confidence = 'emerging' AND w.numeric_contract_version>=2 THEN 1 ELSE 0 END) AS has_emerging_edge
    FROM signal_episodes e
    LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
    LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
    ${since ? "WHERE e.caught_at >= ?1" : ""}
    GROUP BY e.episode_id
    ORDER BY e.caught_at DESC
    LIMIT ?${since ? 2 : 1}
  `).bind(...(since ? [since, limit] : [limit])).all();
  return { ok: true, window, rows: (result.results || []).map(row=>publicOutcomeRow(row,["return_72h","max_return_72h","max_drawdown_72h"],"outcome_status")) };
}

export async function historyEpisodeDetail(env, episodeIdValue) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const id = text(episodeIdValue);
  if (!id) throw new Error("episode_required");
  const [episode, events, wallets, observations, outcomes] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM signal_episodes WHERE episode_id = ?1").bind(id).first(),
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM signal_episode_events WHERE episode_id = ?1 ORDER BY observed_at DESC LIMIT 100").bind(id).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT w.*, s.edge_score AS current_edge_score, s.confidence AS current_edge_confidence
      FROM signal_wallets w LEFT JOIN wallet_scores s ON s.wallet_address = w.wallet_address
      WHERE w.episode_id = ?1 ORDER BY w.buy_sol DESC LIMIT 100
    `).bind(id).all(),
    env.RADAR_HISTORY_DB.prepare(`SELECT * FROM wallet_observations legacy WHERE episode_id=?1
      AND (numeric_contract_version>=2 OR NOT EXISTS (SELECT 1 FROM wallet_observation_bundles b,json_each(b.observations_json) j
        WHERE b.episode_id=legacy.episode_id AND b.observed_at=legacy.observed_at
          AND json_extract(j.value,'$.wallet_address')=legacy.wallet_address))
      UNION ALL SELECT b.episode_id,json_extract(j.value,'$.wallet_address'),b.observed_at,
        json_extract(j.value,'$.current_token_balance'),json_extract(j.value,'$.balance_retained_pct'),
        json_extract(j.value,'$.additional_buy_tokens'),json_extract(j.value,'$.outbound_transfer_tokens'),
        json_extract(j.value,'$.behavior_status'),json_extract(j.value,'$.estimated_pnl_pct'),
        json_extract(j.value,'$.estimated_pnl_sol'),json_extract(j.value,'$.coverage_status'),json_extract(j.value,'$.raw_object_key'),b.numeric_contract_version
      FROM wallet_observation_bundles b,json_each(b.observations_json) j WHERE b.episode_id=?1 AND NOT EXISTS (
        SELECT 1 FROM wallet_observations legacy WHERE legacy.episode_id=b.episode_id AND legacy.observed_at=b.observed_at
          AND legacy.wallet_address=json_extract(j.value,'$.wallet_address') AND legacy.numeric_contract_version>=2)
      ORDER BY observed_at DESC LIMIT 250`).bind(id).all(),
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM signal_outcomes WHERE episode_id = ?1 ORDER BY horizon_minutes ASC").bind(id).all(),
  ]);
  if (!episode) throw new Error("episode_not_found");
  return { ok: true, episode, events: (events.results || []).map((row) => ({ ...row, payload: parsePayload(row.payload_json, {}) })), wallets: wallets.results || [],
    observations: (observations.results || []).map(row=>({...row,trusted:row.numeric_contract_version>=2})),
    outcomes: (outcomes.results || []).map(row=>({...row,trusted:row.numeric_contract_version>=2 && row.entry_verified===1})) };
}

export async function historyTokenDetail(env, tokenKey) {
  if (!hasHistoryDb(env)) return null;
  const token = text(tokenKey);
  if (!token) return null;
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT e.episode_id, e.caught_at, e.caught_mcap_usd, e.caught_tier,
      e.signal_family, e.data_quality_status, o.max_return_pct, o.return_pct,
      o.status AS outcome_status,o.numeric_contract_version outcome_numeric_version,o.entry_verified outcome_entry_verified,
      COUNT(DISTINCT CASE WHEN w.prior_edge_confidence = 'validated' AND w.numeric_contract_version>=2 THEN w.wallet_address END) AS validated_wallets,
      COUNT(DISTINCT CASE WHEN w.prior_edge_confidence = 'emerging' AND w.numeric_contract_version>=2 THEN w.wallet_address END) AS emerging_wallets,
      MAX(CASE WHEN w.numeric_contract_version>=2 THEN w.prior_edge_score END) AS edge_at_catch_score,
      MAX(CASE WHEN s.numeric_contract_version>=2 THEN s.edge_score END) AS edge_now_score
    FROM signal_episodes e
    LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
    LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
    LEFT JOIN wallet_scores s ON s.wallet_address = w.wallet_address
    WHERE e.token_address = ?1
    GROUP BY e.episode_id
    ORDER BY e.caught_at DESC LIMIT 12
  `).bind(token).all();
  const episodes = (result.results || []).map(row=>publicOutcomeRow(row,["max_return_pct","return_pct"],"outcome_status"));
  if (!episodes.length) return null;
  return { token_key: token, episodes, latest: episodes[0] };
}

export async function historyStatus(env) {
  if (!hasHistoryDb(env)) return { configured: false, healthy: false };
  const [episode, outbox] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at FROM signal_episodes").first(),
    env?.RADAR_DB?.prepare("SELECT COUNT(*) AS count, MIN(created_at) AS oldest_at FROM history_outbox WHERE status = 'pending'").first(),
  ]);
  return {
    configured: true,
    healthy: true,
    episodes: Number(episode?.count) || 0,
    last_history_update_at: episode?.updated_at || null,
    pending_outbox: Number(outbox?.count) || 0,
    oldest_pending_outbox_at: outbox?.oldest_at || null,
    archive_configured: Boolean(env?.RADAR_ARCHIVE),
  };
}

const SERVICE_KINDS = new Set(["service", "cex", "exchange", "bridge", "router", "relay", "lifi",
  "terminal", "executor", "pool", "burn", "program"]);
const BUILTIN_INFRA = new Set(["So11111111111111111111111111111111111111112",
  "1nc1nerator11111111111111111111111111111111111", "11111111111111111111111111111111"]);

export function infrastructureSource(value, row = {}, episode = {}, extra = []) {
  const kinds = [row.source_kind,
    ...(row.common_funder === value ? [row.common_funder_kind] : []),
    ...(row.common_executor === value ? [row.common_executor_kind] : [])];
  return BUILTIN_INFRA.has(value) || extra.includes(value)
    || [episode.pool_address, episode.token_address].includes(value)
    || kinds.some(kind=>SERVICE_KINDS.has(String(kind || "").toLowerCase())) || row.supporting_only === true;
}

export { OUTCOME_HORIZONS, normalizedEpisode, normalizedWallet, normalizedOutcome, historyEventId,
  hash, serialize, number, runBatch, checkedHistoryDb, existingPriorScores, upsertEpisode, upsertEpisodeEvent,
  upsertWallets, upsertOutcomes, upsertClusterEdges, refreshMarketBaselines, refreshWalletScores };
