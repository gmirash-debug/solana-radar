import {number, serialize, infrastructureSource} from "./history.js";

const PAGE = 25;

async function scoreSummary(db, members, binds) {
  return db.prepare(`WITH members AS (${members}),
    scores AS (SELECT s.* FROM wallet_scores s JOIN members m USING(wallet_address) WHERE s.numeric_contract_version>=2),
    ranked_score AS (SELECT edge_score,ROW_NUMBER() OVER (ORDER BY edge_score) n,COUNT(*) OVER () total FROM scores),
    ranked_lift AS (SELECT lift_2x,ROW_NUMBER() OVER (ORDER BY lift_2x) n,COUNT(*) OVER () total FROM scores WHERE lift_2x IS NOT NULL)
    SELECT (SELECT COUNT(*) FROM members) wallet_count,
      COALESCE(SUM(eligible_episodes),0) eligible_episodes,COALESCE(SUM(wins_2x_72h),0) wins,
      COALESCE((SELECT AVG(edge_score) FROM ranked_score WHERE n IN ((total+1)/2,(total+2)/2)),0) edge_score,
      (SELECT AVG(lift_2x) FROM ranked_lift WHERE n IN ((total+1)/2,(total+2)/2)) lift,
      CASE WHEN MAX(confidence='validated') THEN 'validated' WHEN MAX(confidence='emerging') THEN 'emerging' ELSE 'unproven' END confidence
    FROM scores`).bind(...binds).first();
}

export async function refreshClusterScoreStep(db, episode, state, now) {
  const next=await db.prepare(`SELECT DISTINCT c.cluster_id FROM wallet_clusters c
    JOIN wallet_cluster_members m USING(cluster_id) JOIN signal_wallets w ON w.wallet_address=m.wallet_address
    WHERE c.active=1 AND c.numeric_contract_version>=2 AND w.episode_id=?1 AND c.cluster_id>?2
    ORDER BY c.cluster_id LIMIT 1`).bind(episode,state.after || "").first();
  if (!next) return true;
  const stats=await scoreSummary(db,"SELECT wallet_address FROM wallet_cluster_members WHERE cluster_id=?1",[next.cluster_id]);
  await db.prepare(`UPDATE wallet_clusters SET confidence=?2,eligible_episodes=?3,wins_2x_72h=?4,lift_2x=?5,
    edge_score=?6,computed_through=?7,updated_at=?7 WHERE cluster_id=?1 AND active=1`)
    .bind(next.cluster_id,stats.confidence,stats.eligible_episodes,stats.wins,number(stats.lift),stats.edge_score,now).run();
  state.after=next.cluster_id;
  return false;
}

function digestMembers(state, members) {
  state.digest ||= [0x811c9dc5, 0x9e3779b9];
  for (const member of members) {
    const value = `${state.hasMember ? "|" : ""}${member}`;
    for (let i = 0; i < value.length; i++) {
      state.digest[0] = Math.imul(state.digest[0] ^ value.charCodeAt(i), 0x01000193);
      state.digest[1] = Math.imul(state.digest[1] ^ value.charCodeAt(i), 0x85ebca6b);
    }
    state.hasMember = true;
  }
}

// Frontier and visited vertices live in D1, not in an unbounded DO JSON value.
// Each step either commits an idempotent bounded batch or advances a key cursor.
export async function stepClusterJob(db, job, state, seeds, now, extraInfra = [], pageSize = PAGE) {
  state.phase ||= "seed";
  if (state.phase === "seed") {
    const addresses = seeds.slice(state.seedIndex || 0, (state.seedIndex || 0) + pageSize);
    if (addresses.length) {
      await db.batch(addresses.map(wallet => db.prepare(`INSERT OR IGNORE INTO history_cluster_work
        (job_id,wallet_address) VALUES (?1,?2)`).bind(job, wallet)));
      state.seedIndex = (state.seedIndex || 0) + addresses.length;
      return false;
    }
    state.phase = "old_members";
    state.after = "";
  }
  if (state.phase === "old_members") {
    // Seed obsolete overlapping fragments too, so infrastructure removal can
    // split and retire them without losing the unaffected half of a component.
    const page = await db.prepare(`SELECT DISTINCT m.wallet_address FROM wallet_cluster_members m
      WHERE m.wallet_address > ?2 AND m.cluster_id IN (
        SELECT old.cluster_id FROM wallet_cluster_members old JOIN history_cluster_work w
          ON w.wallet_address=old.wallet_address WHERE w.job_id=?1)
      ORDER BY m.wallet_address LIMIT ${pageSize}`).bind(job, state.after).all();
    if (page.results.length) {
      const inserted = await db.batch(page.results.map(row => db.prepare(`INSERT OR IGNORE INTO history_cluster_work
        (job_id,wallet_address) VALUES (?1,?2)`).bind(job, row.wallet_address)));
      state.after = inserted.some(result => result.meta?.rows_written > 0) ? "" : page.results.at(-1).wallet_address;
      return false;
    }
    state.phase = "start";
  }
  if (state.phase === "start") {
    const next = await db.prepare(`SELECT wallet_address FROM history_cluster_work
      WHERE job_id=?1 AND component IS NULL ORDER BY wallet_address LIMIT 1`).bind(job).first();
    if (!next) { state.phase = "cleanup"; return false; }
    const seed = next.wallet_address;
    await db.prepare(`UPDATE history_cluster_work SET component=?2 WHERE job_id=?1 AND wallet_address=?2`)
      .bind(job, seed).run();
    state.component = seed;
    state.phase = "frontier";
  }
  if (state.phase === "frontier") {
    const next = await db.prepare(`SELECT wallet_address FROM history_cluster_work
      WHERE job_id=?1 AND component=?2 AND visited=0 ORDER BY wallet_address LIMIT 1`)
      .bind(job, state.component).first();
    if (!next) {
      state.phase = "digest"; state.after = ""; state.digest = null; state.hasMember = false;
      return false;
    }
    state.wallet = next.wallet_address;
    state.edgeAfter = "";
    state.phase = "edges";
  }
  if (state.phase === "edges") {
    const page = await db.prepare(`SELECT e.edge_id,e.wallet_a,e.wallet_b,e.evidence_json,e.is_infrastructure,
      neighbor.component neighbor_component FROM wallet_cluster_edges e LEFT JOIN history_cluster_work neighbor
        ON neighbor.job_id=?3 AND neighbor.wallet_address=CASE WHEN e.wallet_a=?1 THEN e.wallet_b ELSE e.wallet_a END
      WHERE (e.wallet_a=?1 OR e.wallet_b=?1) AND e.weight>=1 AND e.edge_id>?2
      ORDER BY e.edge_id LIMIT ${pageSize}`).bind(state.wallet, state.edgeAfter,job).all();
    const statements = [];
    for (const edge of page.results) {
      const evidence = JSON.parse(edge.evidence_json || "{}");
      if (edge.is_infrastructure || infrastructureSource(evidence.value, evidence, {}, extraInfra)) {
        if (!edge.is_infrastructure) statements.push(db.prepare(`UPDATE wallet_cluster_edges
          SET is_infrastructure=1 WHERE edge_id=?1`).bind(edge.edge_id));
        continue;
      }
      if (edge.neighbor_component === state.component) continue;
      if (edge.neighbor_component !== null) throw new Error("history_cluster_component_conflict");
      const other = edge.wallet_a === state.wallet ? edge.wallet_b : edge.wallet_a;
      statements.push(db.prepare(`INSERT INTO history_cluster_work (job_id,wallet_address,component)
        VALUES (?1,?2,?3) ON CONFLICT(job_id,wallet_address) DO UPDATE SET
          component=COALESCE(history_cluster_work.component,excluded.component)`)
        .bind(job, other, state.component));
    }
    if (statements.length) await db.batch(statements);
    if (page.results.length) state.edgeAfter = page.results.at(-1).edge_id;
    else {
      await db.prepare(`UPDATE history_cluster_work SET visited=1 WHERE job_id=?1 AND wallet_address=?2`)
        .bind(job, state.wallet).run();
      state.phase = "frontier";
    }
    return false;
  }
  if (state.phase === "digest") {
    const page = await db.prepare(`SELECT wallet_address FROM history_cluster_work
      WHERE job_id=?1 AND component=?2 AND wallet_address>?3 ORDER BY wallet_address LIMIT ${pageSize}`)
      .bind(job, state.component, state.after).all();
    if (page.results.length) {
      digestMembers(state, page.results.map(row => row.wallet_address));
      state.after = page.results.at(-1).wallet_address;
      return false;
    }
    state.cluster = `cluster:${state.digest.map(n => (n >>> 0).toString(16).padStart(8,"0")).join("")}`;
    state.phase = "summary";
  }
  if (state.phase === "summary") {
    const stats = await scoreSummary(db,"SELECT wallet_address FROM history_cluster_work WHERE job_id=?1 AND component=?2",[job,state.component]);
    const links = await db.prepare(`SELECT JSON_GROUP_ARRAY(DISTINCT e.relation_type) relations,
      MIN(e.first_seen_at) first_seen,MAX(e.last_seen_at) last_seen FROM wallet_cluster_edges e
      JOIN history_cluster_work a ON a.wallet_address=e.wallet_a AND a.job_id=?1 AND a.component=?2
      JOIN history_cluster_work b ON b.wallet_address=e.wallet_b AND b.job_id=?1 AND b.component=?2
      WHERE e.weight>=1 AND e.is_infrastructure=0`).bind(job, state.component).first();
    state.first = links.first_seen || now;
    state.last = links.last_seen || now;
    state.singleton = stats.wallet_count < 2;
    if (!state.singleton) {
      await db.prepare(`INSERT INTO wallet_clusters (cluster_id,confidence,wallet_count,eligible_episodes,
        wins_2x_72h,lift_2x,edge_score,relation_types_json,first_seen_at,last_seen_at,computed_through,updated_at,active,numeric_contract_version)
        VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?11,0,2)
        ON CONFLICT(cluster_id) DO UPDATE SET confidence=excluded.confidence,wallet_count=excluded.wallet_count,
          eligible_episodes=excluded.eligible_episodes,wins_2x_72h=excluded.wins_2x_72h,lift_2x=excluded.lift_2x,
          edge_score=excluded.edge_score,relation_types_json=excluded.relation_types_json,
          last_seen_at=excluded.last_seen_at,computed_through=excluded.computed_through,updated_at=excluded.updated_at,numeric_contract_version=2`)
        .bind(state.cluster,stats.confidence,stats.wallet_count,stats.eligible_episodes,stats.wins,
          number(stats.lift),stats.edge_score,links.relations || serialize([]),state.first,state.last,now).run();
    }
    state.after = "";
    state.phase = "members";
    return false;
  }
  if (state.phase === "members") {
    const page = state.singleton ? {results:[]} : await db.prepare(`SELECT wallet_address FROM history_cluster_work
      WHERE job_id=?1 AND component=?2 AND wallet_address>?3 ORDER BY wallet_address LIMIT ${pageSize}`)
      .bind(job,state.component,state.after).all();
    if (page.results.length) {
      await db.batch(page.results.map(row => db.prepare(`INSERT INTO wallet_cluster_members
        (cluster_id,wallet_address,first_seen_at,last_seen_at,membership_weight) VALUES (?1,?2,?3,?4,1)
        ON CONFLICT(cluster_id,wallet_address) DO UPDATE SET last_seen_at=excluded.last_seen_at,membership_weight=1`)
        .bind(state.cluster,row.wallet_address,state.first,state.last)));
      state.after = page.results.at(-1).wallet_address;
      return false;
    }
    state.phase = "publish";
  }
  if (state.phase === "publish") {
    const replacement=state.singleton ? null : state.cluster;
    const obsolete=await db.prepare(`SELECT DISTINCT c.cluster_id FROM wallet_clusters c
      JOIN wallet_cluster_members m USING(cluster_id) JOIN history_cluster_work w USING(wallet_address)
      WHERE w.job_id=?1 AND w.component=?2 AND c.active=1 AND (?3 IS NULL OR c.cluster_id<>?3)
      ORDER BY c.cluster_id LIMIT ${pageSize}`).bind(job,state.component,replacement).all();
    if (obsolete.results.length) {
      await db.batch(obsolete.results.map(row=>db.prepare(`UPDATE wallet_clusters
        SET active=0,retired_at=?2,replaced_by=?3 WHERE cluster_id=?1 AND active=1`)
        .bind(row.cluster_id,now,replacement)));
      return false;
    }
    // Activate only after all overlapping fragments have retired. A deferred
    // publication may temporarily omit a component, never publish duplicate ones.
    if (!state.singleton) await db.prepare(`UPDATE wallet_clusters SET active=1,retired_at=NULL,replaced_by=NULL
      WHERE cluster_id=?1`).bind(state.cluster).run();
    state.phase = "start";
    return false;
  }
  if (state.phase === "cleanup") {
    // Bounded deletion is scratch cleanup only, never archive or queue payloads.
    const page=await db.prepare(`SELECT wallet_address FROM history_cluster_work
      WHERE job_id=?1 ORDER BY wallet_address LIMIT ${pageSize}`).bind(job).all();
    if (!page.results.length) return true;
    await db.batch(page.results.map(row=>db.prepare(`DELETE FROM history_cluster_work
      WHERE job_id=?1 AND wallet_address=?2`).bind(job,row.wallet_address)));
  }
  return false;
}
