const OUTBOX_BATCH_SIZE = 12;
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
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positive(value, fallback = null) {
  const parsed = number(value, null);
  return parsed !== null && parsed > 0 ? parsed : fallback;
}

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
  const bought = positive(raw.bought_tokens ?? raw.attributed_tokens ?? raw.token_bought, 0);
  const balance = positive(raw.current_token_balance ?? raw.current_balance, 0);
  const retainedPct = number(raw.balance_retained_pct ?? raw.retention_pct, bought > 0 ? clamp(balance / bought * 100, 0, 100) : null);
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
    held_tokens_at_catch: positive(raw.held_tokens_at_catch ?? raw.initial_balance ?? raw.current_balance, 0),
    held_supply_pct_at_catch: number(raw.held_supply_pct_at_catch, null),
    // Retention is a live observation. It must never overwrite the at-catch
    // fact or turn a balance decrease into a claimed sale.
    retained_pct_at_catch: number(raw.retained_pct_at_catch, bought > 0 ? 100 : null),
    common_funder: text(raw.common_funder),
    common_executor: text(raw.common_executor),
    cluster_id_at_catch: text(raw.cluster_id_at_catch),
    evidence_status: text(raw.evidence_status) || "partial",
    raw_object_key: text(raw.raw_object_key),
    observation: {
      current_token_balance: balance,
      balance_retained_pct: retainedPct,
      additional_buy_tokens: positive(raw.additional_buy_tokens, 0),
      outbound_transfer_tokens: positive(raw.outbound_transfer_tokens, 0),
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
      status: hasCheckpoint ? "complete" : "pending",
      source: text(raw.source) || "scanner_market_snapshot",
      error: text(raw.error),
      updated_at: now,
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
  for (let index = 0; index < addresses.length; index += D1_IN_PARAMETER_LIMIT) {
    const chunk = addresses.slice(index, index + D1_IN_PARAMETER_LIMIT);
    const placeholders = chunk.map((_, item) => `?${item + 1}`).join(", ");
    const result = await db.prepare(`
      SELECT wallet_address, edge_score, confidence, eligible_episodes, computed_through
      FROM wallet_scores
      WHERE wallet_address IN (${placeholders})
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
        prior_episode_count, prior_score_computed_through, evidence_status, raw_object_key, created_at, updated_at
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26)
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
        estimated_pnl_sol, coverage_status, raw_object_key
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
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

async function upsertOutcomes(db, episode, outcome, now) {
  const statements = normalizedOutcome(outcome, episode, now).map((row) => db.prepare(`
    INSERT INTO signal_outcomes (
      episode_id, horizon_minutes, due_at, evaluated_at, endpoint_price_usd, endpoint_mcap_usd,
      endpoint_liquidity_usd, return_pct, max_return_pct, max_drawdown_pct, time_to_1_5x_minutes,
      time_to_2x_minutes, time_to_5x_minutes, hit_1_5x, hit_2x, hit_5x, tradable_2x,
      market_data_coverage_pct, largest_gap_minutes, status, source, error, updated_at
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23)
    ON CONFLICT(episode_id, horizon_minutes) DO UPDATE SET
      evaluated_at = COALESCE(excluded.evaluated_at, signal_outcomes.evaluated_at),
      endpoint_price_usd = COALESCE(excluded.endpoint_price_usd, signal_outcomes.endpoint_price_usd),
      endpoint_mcap_usd = COALESCE(excluded.endpoint_mcap_usd, signal_outcomes.endpoint_mcap_usd),
      endpoint_liquidity_usd = COALESCE(excluded.endpoint_liquidity_usd, signal_outcomes.endpoint_liquidity_usd),
      return_pct = COALESCE(excluded.return_pct, signal_outcomes.return_pct),
      max_return_pct = CASE WHEN excluded.max_return_pct IS NOT NULL AND (signal_outcomes.max_return_pct IS NULL OR excluded.max_return_pct > signal_outcomes.max_return_pct) THEN excluded.max_return_pct ELSE signal_outcomes.max_return_pct END,
      max_drawdown_pct = CASE WHEN excluded.max_drawdown_pct IS NOT NULL AND (signal_outcomes.max_drawdown_pct IS NULL OR excluded.max_drawdown_pct < signal_outcomes.max_drawdown_pct) THEN excluded.max_drawdown_pct ELSE signal_outcomes.max_drawdown_pct END,
      time_to_1_5x_minutes = COALESCE(signal_outcomes.time_to_1_5x_minutes, excluded.time_to_1_5x_minutes),
      time_to_2x_minutes = COALESCE(signal_outcomes.time_to_2x_minutes, excluded.time_to_2x_minutes),
      time_to_5x_minutes = COALESCE(signal_outcomes.time_to_5x_minutes, excluded.time_to_5x_minutes),
      hit_1_5x = COALESCE(excluded.hit_1_5x, signal_outcomes.hit_1_5x),
      hit_2x = COALESCE(excluded.hit_2x, signal_outcomes.hit_2x),
      hit_5x = COALESCE(excluded.hit_5x, signal_outcomes.hit_5x),
      tradable_2x = COALESCE(excluded.tradable_2x, signal_outcomes.tradable_2x),
      market_data_coverage_pct = COALESCE(excluded.market_data_coverage_pct, signal_outcomes.market_data_coverage_pct),
      largest_gap_minutes = COALESCE(excluded.largest_gap_minutes, signal_outcomes.largest_gap_minutes),
      status = CASE WHEN excluded.status = 'complete' THEN 'complete' WHEN signal_outcomes.status = 'complete' THEN 'complete' ELSE excluded.status END,
      source = COALESCE(excluded.source, signal_outcomes.source),
      error = COALESCE(excluded.error, signal_outcomes.error),
      updated_at = excluded.updated_at
  `).bind(
    episode.episode_id, row.horizon_minutes, row.due_at, row.evaluated_at, row.endpoint_price_usd,
    row.endpoint_mcap_usd, row.endpoint_liquidity_usd, row.return_pct, row.max_return_pct,
    row.max_drawdown_pct, row.time_to_1_5x_minutes, row.time_to_2x_minutes, row.time_to_5x_minutes,
    row.hit_1_5x, row.hit_2x, row.hit_5x, row.tradable_2x, row.market_data_coverage_pct,
    row.largest_gap_minutes, row.status, row.source, row.error, row.updated_at,
  ));
  await runBatch(db, statements);
}

async function upsertClusterEdges(db, episode, wallets, observedAt, now) {
  const groups = new Map();
  for (const wallet of wallets) {
    for (const [type, value] of [["common_funder", wallet.common_funder], ["common_executor", wallet.common_executor]]) {
      if (!value) continue;
      const key = `${type}:${value}`;
      const group = groups.get(key) || { type, value, wallets: [] };
      group.wallets.push(wallet.wallet_address);
      groups.set(key, group);
    }
  }
  const statements = [];
  for (const group of groups.values()) {
    const members = [...new Set(group.wallets)].sort();
    for (let left = 0; left < members.length; left += 1) {
      for (let right = left + 1; right < members.length; right += 1) {
        const edgeId = `edge:${hash(`${members[left]}|${members[right]}|${group.type}|${group.value}`)}`;
        statements.push(db.prepare(`
          INSERT INTO wallet_cluster_edges (
            edge_id, wallet_a, wallet_b, relation_type, evidence_count, first_seen_at, last_seen_at,
            weight, evidence_json, created_at, updated_at
          ) VALUES (?1, ?2, ?3, ?4, 1, ?5, ?5, 1, ?6, ?7, ?7)
          ON CONFLICT(edge_id) DO UPDATE SET
            evidence_count = wallet_cluster_edges.evidence_count + 1,
            last_seen_at = excluded.last_seen_at,
            weight = wallet_cluster_edges.weight + 1,
            updated_at = excluded.updated_at
        `).bind(edgeId, members[left], members[right], group.type, observedAt, serialize({ value: group.value, episode_id: episode.episode_id }), now));
      }
    }
  }
  await runBatch(db, statements);
}

async function refreshMarketBaselines(db, now) {
  const result = await db.prepare(`
    SELECT e.mcap_band, e.liquidity_band, e.age_band, e.signal_family, o.horizon_minutes,
      COUNT(*) AS eligible_episodes,
      AVG(CASE WHEN o.max_return_pct >= 50 THEN 1.0 ELSE 0.0 END) AS hit_1_5x_rate,
      AVG(CASE WHEN o.max_return_pct >= 100 THEN 1.0 ELSE 0.0 END) AS hit_2x_rate,
      AVG(CASE WHEN o.max_return_pct >= 400 THEN 1.0 ELSE 0.0 END) AS hit_5x_rate
    FROM signal_episodes e
    JOIN signal_outcomes o ON o.episode_id = e.episode_id
    WHERE o.status = 'complete'
    GROUP BY e.mcap_band, e.liquidity_band, e.age_band, e.signal_family, o.horizon_minutes
  `).all();
  const statements = (result.results || []).map((row) => {
    const key = [row.mcap_band, row.liquidity_band, row.age_band, row.signal_family, row.horizon_minutes].join("|");
    return db.prepare(`
      INSERT INTO market_baselines (
        baseline_key, mcap_band, liquidity_band, age_band, signal_family, horizon_minutes,
        eligible_episodes, hit_1_5x_rate, hit_2x_rate, hit_5x_rate,
        median_max_return_pct, median_drawdown_pct, computed_through, updated_at
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, NULL, NULL, ?11, ?11)
      ON CONFLICT(baseline_key) DO UPDATE SET
        eligible_episodes = excluded.eligible_episodes,
        hit_1_5x_rate = excluded.hit_1_5x_rate,
        hit_2x_rate = excluded.hit_2x_rate,
        hit_5x_rate = excluded.hit_5x_rate,
        computed_through = excluded.computed_through,
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
    WHERE horizon_minutes = 4320
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
    `).bind(wallet).all();
    const rows = result.results || [];
    if (!rows.length) continue;
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
        computed_through, score_version, updated_at
      ) VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, ?7, ?8, ?9, ?10, NULL, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?17)
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
  if (wallets.length < 2) return 0;
  const placeholders = wallets.map((_, index) => `?${index + 1}`).join(", ");
  const result = await db.prepare(`
    SELECT wallet_a, wallet_b, relation_type, weight, first_seen_at, last_seen_at
    FROM wallet_cluster_edges
    WHERE wallet_a IN (${placeholders}) OR wallet_b IN (${placeholders})
  `).bind(...wallets, ...wallets).all();
  const parent = new Map();
  const find = (value) => {
    if (!parent.has(value)) parent.set(value, value);
    if (parent.get(value) !== value) parent.set(value, find(parent.get(value)));
    return parent.get(value);
  };
  const join = (left, right) => {
    const a = find(left);
    const b = find(right);
    if (a !== b) parent.set(b, a);
  };
  for (const edge of result.results || []) {
    if (number(edge.weight, 0) >= 1) join(edge.wallet_a, edge.wallet_b);
  }
  const components = new Map();
  for (const wallet of parent.keys()) {
    const root = find(wallet);
    const rows = components.get(root) || [];
    rows.push(wallet);
    components.set(root, rows);
  }
  let count = 0;
  for (const members of components.values()) {
    if (members.length < 2) continue;
    const sorted = members.sort();
    const clusterId = `cluster:${hash(sorted.join("|"))}`;
    const memberPlaceholders = sorted.map((_, index) => `?${index + 1}`).join(", ");
    const scoreRows = await db.prepare(`
      SELECT edge_score, lift_2x, eligible_episodes, wins_2x_72h, confidence, first_seen_at, last_seen_at
      FROM wallet_scores WHERE wallet_address IN (${memberPlaceholders})
    `).bind(...sorted).all();
    const scores = scoreRows.results || [];
    const edgeScore = median(scores.map((row) => number(row.edge_score, 0))) || 0;
    const lift = median(scores.map((row) => number(row.lift_2x)).filter((value) => value !== null));
    const episodes = scores.reduce((sum, row) => sum + (Number(row.eligible_episodes) || 0), 0);
    const wins = scores.reduce((sum, row) => sum + (Number(row.wins_2x_72h) || 0), 0);
    const confidence = scores.some((row) => row.confidence === "validated") ? "validated" : scores.some((row) => row.confidence === "emerging") ? "emerging" : "unproven";
    const linked = (result.results || []).filter((edge) => sorted.includes(edge.wallet_a) && sorted.includes(edge.wallet_b));
    const relations = [...new Set(linked.map((edge) => edge.relation_type))];
    const firstSeen = linked.map((edge) => edge.first_seen_at).sort()[0] || now;
    const lastSeen = linked.map((edge) => edge.last_seen_at).sort().at(-1) || now;
    await db.prepare(`
      INSERT INTO wallet_clusters (
        cluster_id, confidence, wallet_count, eligible_episodes, wins_2x_72h, lift_2x,
        edge_score, relation_types_json, first_seen_at, last_seen_at, computed_through, updated_at
      ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?11)
      ON CONFLICT(cluster_id) DO UPDATE SET
        confidence = excluded.confidence,
        wallet_count = excluded.wallet_count,
        eligible_episodes = excluded.eligible_episodes,
        wins_2x_72h = excluded.wins_2x_72h,
        lift_2x = excluded.lift_2x,
        edge_score = excluded.edge_score,
        relation_types_json = excluded.relation_types_json,
        last_seen_at = excluded.last_seen_at,
        computed_through = excluded.computed_through,
        updated_at = excluded.updated_at
    `).bind(clusterId, confidence, sorted.length, episodes, wins, lift, edgeScore, serialize(relations), firstSeen, lastSeen, now).run();
    const statements = sorted.map((wallet) => db.prepare(`
      INSERT INTO wallet_cluster_members (cluster_id, wallet_address, first_seen_at, last_seen_at, membership_weight)
      VALUES (?1, ?2, ?3, ?4, 1)
      ON CONFLICT(cluster_id, wallet_address) DO UPDATE SET
        last_seen_at = excluded.last_seen_at,
        membership_weight = excluded.membership_weight
    `).bind(clusterId, wallet, firstSeen, lastSeen));
    await runBatch(db, statements);
    count += 1;
  }
  return count;
}

async function ingestHistoryEvent(env, rawEvent, now = nowIso()) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const episode = normalizedEpisode(rawEvent?.episode, now);
  if (!episode) throw new Error("history_episode_token_required");
  const event = rawEvent?.event && typeof rawEvent.event === "object" ? rawEvent.event : {};
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
  const priors = await existingPriorScores(env.RADAR_HISTORY_DB, wallets, observedAt);
  await upsertWallets(env.RADAR_HISTORY_DB, episode, wallets, observedAt, priors, now);
  await upsertOutcomes(env.RADAR_HISTORY_DB, episode, rawEvent?.outcome || {}, now);
  await upsertClusterEdges(env.RADAR_HISTORY_DB, episode, wallets, observedAt, now);
  await refreshMarketBaselines(env.RADAR_HISTORY_DB, now);
  // Outcome events intentionally carry no fresh wallet observation. Resolve
  // every member of their episode nevertheless, so an outcome refreshes the
  // cohort's learned result instead of only the event's empty wallet list.
  const episodeWalletRows = await env.RADAR_HISTORY_DB.prepare(`
    SELECT DISTINCT wallet_address FROM signal_wallets
    WHERE episode_id = ?1 AND cohort_role = 'at_catch'
  `).bind(episode.episode_id).all();
  const relatedWallets = [
    ...wallets.map((wallet) => wallet.wallet_address),
    ...(episodeWalletRows.results || []).map((row) => row.wallet_address),
  ];
  const scoresUpdated = await refreshWalletScores(env.RADAR_HISTORY_DB, relatedWallets, now);
  const clustersUpdated = await refreshClusters(env.RADAR_HISTORY_DB, relatedWallets, now);
  return { event_id: eventId, episode_id: episode.episode_id, wallets: wallets.length, scores_updated: scoresUpdated, clusters_updated: clustersUpdated };
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
    WHERE status != 'delivered' AND next_attempt_at <= ?1
    ORDER BY next_attempt_at ASC, event_id ASC
    LIMIT ?2
  `).bind(now, Math.max(1, Math.min(50, Number(limit) || OUTBOX_BATCH_SIZE))).all();
  let delivered = 0;
  let failed = 0;
  for (const row of page.results || []) {
    try {
      await ingestHistoryEvent(env, parsePayload(row.payload_json, {}), now);
      await env.RADAR_DB.prepare(`
        UPDATE history_outbox
        SET status = 'delivered', delivered_at = ?2, updated_at = ?2, last_error = NULL
        WHERE event_id = ?1
      `).bind(row.event_id, now).run();
      delivered += 1;
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
  const pending = await env.RADAR_DB.prepare("SELECT COUNT(*) AS count FROM history_outbox WHERE status != 'delivered'").first();
  const status = {
    updated_at: now,
    last_flush_at: now,
    delivered,
    failed,
    pending: Number(pending?.count) || 0,
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
        o.status,
        o.max_return_pct,
        o.tradable_2x,
        MAX(CASE WHEN w.prior_edge_confidence = 'validated' THEN 1 ELSE 0 END) AS has_validated_edge
      FROM signal_outcomes o
      JOIN signal_episodes e ON e.episode_id = o.episode_id
      LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
      WHERE o.horizon_minutes = 4320 ${since ? "AND e.caught_at >= ?1" : ""}
      GROUP BY o.episode_id
    `).bind(...binds).all(),
    env.RADAR_HISTORY_DB.prepare(`SELECT COUNT(*) AS count FROM wallet_scores WHERE confidence != 'unproven'`).first(),
    env.RADAR_HISTORY_DB.prepare(`SELECT COUNT(*) AS count FROM wallet_clusters WHERE confidence != 'unproven'`).first(),
  ]);
  const rows = outcomes.results || [];
  const complete = rows.filter((row) => row.status === "complete");
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
  const where = ["edge_score >= ?1", "eligible_episodes >= ?2"];
  const binds = [0, minSample];
  if (confidence) { where.push(`confidence = ?${binds.length + 1}`); binds.push(confidence); }
  if (minLift) { where.push(`lift_2x >= ?${binds.length + 1}`); binds.push(minLift); }
  if (cursor?.score !== undefined && cursor?.wallet) {
    where.push(`(edge_score < ?${binds.length + 1} OR (edge_score = ?${binds.length + 1} AND wallet_address > ?${binds.length + 2}))`);
    binds.push(cursor.score, cursor.wallet);
  }
  binds.push(limit + 1);
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT wallet_address, eligible_episodes, distinct_tokens, wins_1_5x_72h, wins_2x_72h,
      raw_hit_rate_2x, baseline_hit_rate_2x, bayesian_hit_rate_2x, lift_2x,
      median_lead_minutes, median_retained_24h, median_max_return_72h,
      median_drawdown_72h, edge_score, confidence, first_seen_at, last_seen_at, computed_through
    FROM wallet_scores
    WHERE ${where.join(" AND ")}
    ORDER BY edge_score DESC, lift_2x DESC, eligible_episodes DESC, wallet_address ASC
    LIMIT ?${binds.length}
  `).bind(...binds).all();
  const rows = (result.results || []).slice(0, limit);
  const hasMore = (result.results || []).length > limit;
  const last = rows.at(-1);
  return { ok: true, rows, next_cursor: hasMore && last ? encodeCursor({ score: last.edge_score, wallet: last.wallet_address }) : null };
}

export async function historyWalletDetail(env, wallet) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const address = text(wallet);
  if (!address) throw new Error("wallet_required");
  const [score, episodes, observations, clusters] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM wallet_scores WHERE wallet_address = ?1").bind(address).first(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT e.episode_id, e.token_address, e.symbol, e.caught_at, e.caught_mcap_usd, e.caught_tier,
        e.signal_family, w.buy_sol, w.bought_tokens, w.prior_edge_confidence,
        o.max_return_pct, o.max_drawdown_pct, o.return_pct, o.status
      FROM signal_wallets w
      JOIN signal_episodes e ON e.episode_id = w.episode_id
      LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
      WHERE w.wallet_address = ?1 AND w.cohort_role = 'at_catch'
      ORDER BY e.caught_at DESC LIMIT 100
    `).bind(address).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT episode_id, observed_at, current_token_balance, balance_retained_pct, behavior_status, coverage_status
      FROM wallet_observations WHERE wallet_address = ?1 ORDER BY observed_at DESC LIMIT 100
    `).bind(address).all(),
    env.RADAR_HISTORY_DB.prepare(`
      SELECT c.cluster_id, c.confidence, c.wallet_count, c.lift_2x, c.edge_score, c.relation_types_json
      FROM wallet_cluster_members m JOIN wallet_clusters c ON c.cluster_id = m.cluster_id
      WHERE m.wallet_address = ?1 ORDER BY c.edge_score DESC
    `).bind(address).all(),
  ]);
  if (!score && !(episodes.results || []).length) throw new Error("wallet_not_found");
  return { ok: true, wallet: address, score: score || null, episodes: episodes.results || [], observations: observations.results || [], clusters: (clusters.results || []).map((row) => ({ ...row, relation_types: parsePayload(row.relation_types_json, []) })) };
}

export async function historyClusters(env, query = {}) {
  if (!hasHistoryDb(env)) throw new Error("history_db_not_configured");
  const limit = Math.max(1, Math.min(100, Number(query.limit) || 50));
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT cluster_id, confidence, wallet_count, eligible_episodes, wins_2x_72h, lift_2x,
      edge_score, relation_types_json, first_seen_at, last_seen_at, computed_through
    FROM wallet_clusters ORDER BY edge_score DESC, lift_2x DESC, wallet_count DESC LIMIT ?1
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
      WHERE e.wallet_a IN (SELECT wallet_address FROM wallet_cluster_members WHERE cluster_id = ?1)
        AND e.wallet_b IN (SELECT wallet_address FROM wallet_cluster_members WHERE cluster_id = ?1)
      ORDER BY e.weight DESC LIMIT 100
    `).bind(id).all(),
  ]);
  if (!cluster) throw new Error("cluster_not_found");
  return { ok: true, cluster: { ...cluster, relation_types: parsePayload(cluster.relation_types_json, []) }, members: members.results || [], edges: (edges.results || []).map((row) => ({ ...row, evidence: parsePayload(row.evidence_json, {}) })) };
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
      MAX(CASE WHEN w.prior_edge_confidence = 'validated' THEN 1 ELSE 0 END) AS has_validated_edge,
      MAX(CASE WHEN w.prior_edge_confidence = 'emerging' THEN 1 ELSE 0 END) AS has_emerging_edge
    FROM signal_episodes e
    LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
    LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
    ${since ? "WHERE e.caught_at >= ?1" : ""}
    GROUP BY e.episode_id
    ORDER BY e.caught_at DESC
    LIMIT ?${since ? 2 : 1}
  `).bind(...(since ? [since, limit] : [limit])).all();
  return { ok: true, window, rows: result.results || [] };
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
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM wallet_observations WHERE episode_id = ?1 ORDER BY observed_at DESC LIMIT 250").bind(id).all(),
    env.RADAR_HISTORY_DB.prepare("SELECT * FROM signal_outcomes WHERE episode_id = ?1 ORDER BY horizon_minutes ASC").bind(id).all(),
  ]);
  if (!episode) throw new Error("episode_not_found");
  return { ok: true, episode, events: (events.results || []).map((row) => ({ ...row, payload: parsePayload(row.payload_json, {}) })), wallets: wallets.results || [], observations: observations.results || [], outcomes: outcomes.results || [] };
}

export async function historyTokenDetail(env, tokenKey) {
  if (!hasHistoryDb(env)) return null;
  const token = text(tokenKey);
  if (!token) return null;
  const result = await env.RADAR_HISTORY_DB.prepare(`
    SELECT e.episode_id, e.caught_at, e.caught_mcap_usd, e.caught_tier,
      e.signal_family, e.data_quality_status, o.max_return_pct, o.return_pct,
      o.status AS outcome_status,
      COUNT(DISTINCT CASE WHEN w.prior_edge_confidence = 'validated' THEN w.wallet_address END) AS validated_wallets,
      COUNT(DISTINCT CASE WHEN w.prior_edge_confidence = 'emerging' THEN w.wallet_address END) AS emerging_wallets,
      MAX(w.prior_edge_score) AS edge_at_catch_score,
      MAX(s.edge_score) AS edge_now_score
    FROM signal_episodes e
    LEFT JOIN signal_outcomes o ON o.episode_id = e.episode_id AND o.horizon_minutes = 4320
    LEFT JOIN signal_wallets w ON w.episode_id = e.episode_id AND w.cohort_role = 'at_catch'
    LEFT JOIN wallet_scores s ON s.wallet_address = w.wallet_address
    WHERE e.token_address = ?1
    GROUP BY e.episode_id
    ORDER BY e.caught_at DESC LIMIT 12
  `).bind(token).all();
  const episodes = result.results || [];
  if (!episodes.length) return null;
  return { token_key: token, episodes, latest: episodes[0] };
}

export async function historyStatus(env) {
  if (!hasHistoryDb(env)) return { configured: false, healthy: false };
  const [episode, outbox] = await Promise.all([
    env.RADAR_HISTORY_DB.prepare("SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at FROM signal_episodes").first(),
    env?.RADAR_DB?.prepare("SELECT COUNT(*) AS count, MIN(created_at) AS oldest_at FROM history_outbox WHERE status != 'delivered'").first(),
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

export { OUTCOME_HORIZONS, normalizedEpisode, normalizedOutcome, historyEventId };
