import {normalizedEpisode, normalizedWallet, historyEventEffects, historyEventId, OUTCOME_HORIZONS,
  upsertEpisode, upsertEpisodeEvent, upsertWallets, upsertOutcomes, upsertClusterEdges,
  existingPriorScores, refreshMarketBaselines, refreshWalletScores, infrastructureSource, hash} from "./history.js";
import {stepClusterJob,refreshClusterScoreStep} from "./history-clusters.js";

const PAGE = 25;

export class HistoryYield extends Error {
  constructor(reason) { super(reason); this.deferred = true; }
}

function edgeGroups(wallets, episode, infra) {
  const groups = new Map();
  for (const wallet of wallets) {
    for (const type of ["common_funder", "common_executor"]) {
      const value = wallet[type];
      if (!value || infrastructureSource(value, wallet, episode, infra)) continue;
      const key = `${type}:${value}`;
      if (!groups.has(key)) groups.set(key, {type,value,members:new Set()});
      groups.get(key).members.add(wallet.wallet_address);
    }
  }
  return [...groups.values()].map(group => ({...group,members:[...group.members].sort()}));
}

export function historyInfrastructure(env) {
  const value = env?.HISTORY_INFRASTRUCTURE_ADDRESSES;
  if (value === undefined) return [];
  const addresses = typeof value === "string" ? JSON.parse(value) : value;
  if (!Array.isArray(addresses) || addresses.some(row => typeof row !== "string")) {
    throw new Error("history_infrastructure_addresses_invalid");
  }
  return addresses;
}

export function minimumHistoryWork(event, infra = []) {
  const episode = normalizedEpisode(event.episode);
  const wallets = (event.wallets || []).map(row => normalizedWallet(row,episode)).filter(Boolean);
  if (historyEventEffects(event.event.event_type).recordsClusterEdge
      && edgeGroups(wallets,episode,infra).some(group => group.members.length > 1)) return 24;
  return wallets.length || historyEventEffects(event.event.event_type).refreshesClusters ? 16 : 8;
}

export function minimumProgressWork(event, state = {}, infra = []) {
  if (state.phase === "done") return 0;
  if (state.phase === "wallets" || state.phase === "clusters") return 16;
  if (state.phase === "edges" && historyEventEffects(event.event.event_type).recordsClusterEdge) {
    const episode=normalizedEpisode(event.episode);
    const wallets=(event.wallets || []).map(row=>normalizedWallet(row,episode)).filter(Boolean);
    const groups=edgeGroups(wallets,episode,infra);
    if ((state.group || 0)<groups.length && groups.some(group=>group.members.length>1)) return 24;
  }
  return 8;
}

export function estimatedHistoryQueries(event, state = {}) {
  if (state.phase === "done") return 0;
  if (state.phase && state.phase !== "episode") return 40;
  const effects=historyEventEffects(event.event.event_type);
  if (effects.refreshesClusters) return effects.isSignalEvent ? 40 : 22;
  return 2 + ((event.wallets || []).length ? 1+Math.ceil(event.wallets.length/PAGE) : 0);
}

// Cursor transitions happen only after committed writes. A failed SQL batch
// replays at most that bounded idempotent batch, never earlier event phases.
export async function resumeHistoryEvent(db, raw, state, {now, remaining, infra=[]}) {
  const episode = normalizedEpisode(raw.episode,now);
  const event = raw.event;
  const effects = historyEventEffects(event.event_type);
  const eventId = historyEventId(raw);
  const observed = event.observed_at;
  const wallets = (raw.wallets || []).map(row => normalizedWallet(row,episode)).filter(Boolean);
  const job = `history:${hash(eventId)}`;
  state.version ||= 1;
  state.phase ||= "episode";
  const requireWrites = n => {
    if (remaining() < n) throw new HistoryYield("history_flush_write_budget");
  };
  const lock = async () => {
    if (state.locked) return;
    requireWrites(1);
    await db.prepare(`UPDATE history_cluster_lock SET job_id=?1 WHERE id=1 AND (job_id IS NULL OR job_id=?1)`)
      .bind(job).run();
    const held=await db.prepare("SELECT job_id FROM history_cluster_lock WHERE id=1").first();
    if (held.job_id !== job) throw new HistoryYield("history_cluster_job_busy");
    state.locked=true;
  };
  while (true) {
    switch (state.phase) {
      case "episode":
        requireWrites(1); await upsertEpisode(db,episode,now); state.phase="event"; break;
      case "event":
        requireWrites(1); await upsertEpisodeEvent(db,eventId,episode,event,raw,now);
        state.phase=effects.isSignalEvent ? "wallets" : "observations"; state.index=0; break;
      case "wallets": {
        if (state.index >= wallets.length) { state.phase="outcomes"; state.index=0; break; }
        const count=Math.min(PAGE,Math.floor(remaining()/2)); requireWrites(2);
        const page=wallets.slice(state.index,state.index+count);
        const priors=await existingPriorScores(db,page,episode.caught_at);
        await upsertWallets(db,episode,page,observed,priors,now);
        state.index+=page.length; break;
      }
      case "observations": {
        // Legacy/missed signals still get their catch records. Existing catch
        // rows are untouched; every new live observation remains in the bundle.
        if (state.index < wallets.length) {
          const count=Math.min(PAGE,Math.max(1,Math.floor(remaining()/2)));
          const page=wallets.slice(state.index,state.index+count);
          const placeholders=page.map((_,i)=>`?${i+2}`).join(",");
          const found=await db.prepare(`SELECT wallet_address,cohort_role FROM signal_wallets WHERE episode_id=?1
            AND wallet_address IN (${placeholders})`).bind(episode.episode_id,...page.map(row=>row.wallet_address)).all();
          const known=new Set(found.results.map(row=>`${row.wallet_address}|${row.cohort_role}`));
          const missing=page.filter(row=>!known.has(`${row.wallet_address}|${row.cohort_role}`));
          requireWrites(missing.length*2);
          if (missing.length) await upsertWallets(db,episode,missing,observed,new Map(),now);
          state.index+=page.length; break;
        }
        if (wallets.length) {
          requireWrites(1);
          const observations=wallets.map(wallet=>({wallet_address:wallet.wallet_address,...wallet.observation}));
          await db.prepare(`INSERT OR IGNORE INTO wallet_observation_bundles
            (event_id,episode_id,observed_at,observations_json) VALUES (?1,?2,?3,?4)`)
            .bind(eventId,episode.episode_id,observed,JSON.stringify(observations)).run();
        }
        state.phase="outcomes"; state.index=0; break;
      }
      case "outcomes": {
        const horizons=Object.values(OUTCOME_HORIZONS);
        if (effects.updatesOutcomes && state.index < horizons.length) {
          requireWrites(1); await upsertOutcomes(db,episode,raw.outcome || {},now,horizons[state.index]);
          state.index++; break;
        }
        state.phase="edges"; state.group=0; state.left=0; state.right=1; break;
      }
      case "edges": {
        const groups=effects.recordsClusterEdge ? edgeGroups(wallets,episode,infra) : [];
        if (groups.some(group=>group.members.length>1)) await lock();
        // Three statements per edge, bounded by both remaining units and D1's
        // transactional batch size. Build no quadratic statement array.
        const limit=Math.min(PAGE,Math.floor(remaining()/3));
        let group=state.group,left=state.left,right=state.right;
        const pairs=[];
        while (group<groups.length && pairs.length<limit) {
          const current=groups[group];
          if (left>=current.members.length-1) { group++; left=0; right=1; continue; }
          pairs.push({type:current.type,value:current.value,a:current.members[left],b:current.members[right]});
          if (++right>=current.members.length) { left++; right=left+1; }
        }
        if (pairs.length) {
          // Both evidence insertion and strength recomputation are replay-safe.
          await upsertClusterEdges(db,episode,[],observed,now,pairs);
          Object.assign(state,{group,left,right}); break;
        }
        if (group<groups.length) requireWrites(3);
        state.phase=effects.refreshesScores ? "baselines" : "cluster_seeds";
        state.index=0; state.after=""; break;
      }
      case "baselines": {
        const horizons=Object.values(OUTCOME_HORIZONS);
        if (state.index<horizons.length) {
          requireWrites(1); await refreshMarketBaselines(db,now,episode,horizons[state.index]);
          state.index++; break;
        }
        state.phase="scores"; state.after=""; break;
      }
      case "scores": {
        const next=await db.prepare(`SELECT DISTINCT wallet_address FROM signal_wallets WHERE episode_id=?1
          AND cohort_role='at_catch' AND wallet_address>?2 ORDER BY wallet_address LIMIT 1`)
          .bind(episode.episode_id,state.after).first();
        if (!next) { state.phase="cluster_scores"; state.after=""; break; }
        requireWrites(1); await refreshWalletScores(db,[next.wallet_address],now);
        state.after=next.wallet_address; break;
      }
      case "cluster_scores":
        requireWrites(1);
        if (await refreshClusterScoreStep(db,episode.episode_id,state,now)) state.phase="done";
        break;
      case "cluster_seeds": {
        if (!effects.refreshesClusters) { state.phase="done"; break; }
        if (state.after || wallets.length || effects.refreshesScores) await lock();
        requireWrites(1);
        const limit=Math.min(PAGE,remaining());
        const page=await db.prepare(`SELECT DISTINCT wallet_address FROM signal_wallets WHERE episode_id=?1
          AND wallet_address>?2 ORDER BY wallet_address LIMIT ${limit}`).bind(episode.episode_id,state.after).all();
        if (!page.results.length) {
          state.phase=state.after ? "clusters" : state.locked ? "unlock" : "done";
          state.cluster={phase:"old_members",after:""}; break;
        }
        requireWrites(page.results.length);
        await db.batch(page.results.map(row=>db.prepare(`INSERT OR IGNORE INTO history_cluster_work
          (job_id,wallet_address) VALUES (?1,?2)`).bind(job,row.wallet_address)));
        state.after=page.results.at(-1).wallet_address; break;
      }
      case "clusters":
        requireWrites(2);
        if (await stepClusterJob(db,job,state.cluster,[],now,infra,Math.min(PAGE,remaining()-1))) state.phase="unlock";
        break;
      case "unlock":
        requireWrites(1);
        await db.prepare("UPDATE history_cluster_lock SET job_id=NULL WHERE id=1 AND job_id=?1").bind(job).run();
        state.locked=false; state.phase="done";
        break;
      case "done": return {event_id:eventId,episode_id:episode.episode_id};
      default: throw new Error("history_progress_phase_invalid");
    }
  }
}
