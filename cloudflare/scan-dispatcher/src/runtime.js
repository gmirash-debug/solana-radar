// Operational documents are independent of the analytics database's quota.
import {compactDashboardReport, compactDashboardAlert, dashboardRecordMatchesToken} from "./dashboard-shaping.js";
const MAX_BYTES = 8 * 1024 * 1024;
const CHUNK_CHARS = 16000;

export class RuntimeSnapshots {
  constructor(state, env = {}) { this.storage = state.storage; this.env = env; }

  async fetch(request) {
    const url = new URL(request.url);
    const name = decodeURIComponent(url.pathname.slice(1));
    if (!/^[a-zA-Z0-9:_-]{1,180}$/.test(name)) return Response.json({ok:false, error:"invalid_runtime_document"}, {status:400});
    try {
      if (request.method === "GET") {
        const result = await this.storage.transaction(async tx => {
          const meta = await tx.get(`${name}:meta`);
          if (!meta) return null;
          if (url.searchParams.has("meta")) return meta;
          const keys = Array.from({length:meta.chunks}, (_, i) => `${name}:part:${i}`);
          const parts = new Map();
          for (let i = 0; i < keys.length; i += 128) {
            for (const [key, value] of await tx.get(keys.slice(i, i + 128))) parts.set(key, value);
          }
          if (keys.some(key => !parts.has(key))) throw new Error("runtime_document_incomplete");
          return {...meta, value:JSON.parse(keys.map(key => parts.get(key)).join(""))};
        });
        if (url.searchParams.has("projection")) {
          if (!result?.value?.report?.generated_at) return Response.json({ok:false, error:"runtime_snapshot_missing"}, {status:404});
          const snapshot = result.value;
          const token = url.searchParams.get("token_key");
          if (token) {
            const matches = row => dashboardRecordMatchesToken(row, token);
            const thesis = (snapshot.detail_signal_theses || snapshot.report.signal_theses || []).find(matches);
            const current = (snapshot.detail_current_alerts || snapshot.report.alerts || []).filter(matches);
            const history = (snapshot.detail_history || snapshot.history || []).filter(matches);
            const market = snapshot.market?.[token] || snapshot.market?.[token.replace(/^solana:/, "")];
            return Response.json({ok:Boolean(thesis || current.length || history.length || market),
              token_key:token, thesis:thesis || null, current_alerts:current, history, market:market || null,
              wallet_edge:null, report_source_updated_at:result.updated_at, storage_source:"durable_snapshot"});
          }
          const {detail_signal_theses, detail_current_alerts, detail_history, ...summary} = snapshot;
          summary.report = compactDashboardReport(summary.report);
          summary.history = (summary.history || []).slice(0, Math.max(1, Math.min(250, Number(url.searchParams.get("history_limit")) || 40)));
          for (const field of ["scan_status", "discovery_status", "deleted_tokens", "history_status"]) {
            const status = await runtimeDocument(this.env, field).catch(() => null);
            if (status?.document?.value) summary[field] = status.document.value;
          }
          return Response.json({...summary, ok:true, report_source_updated_at:result.updated_at, storage_source:"durable_snapshot"});
        }
        return Response.json({ok:true, document:result});
      }
      if (request.method !== "POST") return Response.json({ok:false, error:"GET or POST required"}, {status:405});
      let payload = await request.json();
      if (url.searchParams.has("ingest_checkpoint")) {
        if (!payload.checkpoint || typeof payload.checkpoint !== "object" || Array.isArray(payload.checkpoint)) {
          throw new Error("invalid_runtime_checkpoint");
        }
        payload = {value:payload.checkpoint, updated_at:payload.updated_at, revision:payload.revision};
      }
      if (url.searchParams.has("ingest_dashboard")) {
        const snapshot = {...payload, report:compactDashboardReport(payload.report),
          history:(payload.history || []).map(compactDashboardAlert)};
        delete snapshot.history_ledger;
        delete snapshot._sync_progress;
        payload = {value:snapshot, updated_at:snapshot.report.generated_at, revision:0};
      }
      const timestamp = Date.parse(payload.updated_at);
      if (!Number.isFinite(timestamp) || timestamp > Date.now() + 300000) throw new Error("invalid_runtime_timestamp");
      const serialized = JSON.stringify(payload.value);
      if (!serialized || new TextEncoder().encode(serialized).byteLength > MAX_BYTES) throw new Error("runtime_document_exceeds_8mb");
      const revision = Math.max(0, Number(payload.revision) || 0);
      const result = await this.storage.transaction(async tx => {
        const previous = await tx.get(`${name}:meta`);
        if (previous && (timestamp < Date.parse(previous.updated_at)
            || (timestamp === Date.parse(previous.updated_at) && revision < previous.revision))) {
          return {accepted:false, ignored:"stale_checkpoint", updated_at:previous.updated_at};
        }
        const chunks = Math.ceil(serialized.length / CHUNK_CHARS);
        const values = {};
        for (let i = 0; i < chunks; i++) values[`${name}:part:${i}`] = serialized.slice(i * CHUNK_CHARS, (i + 1) * CHUNK_CHARS);
        const meta = {updated_at:payload.updated_at, revision, chunks, bytes:new TextEncoder().encode(serialized).byteLength};
        values[`${name}:meta`] = meta;
        const entries = Object.entries(values);
        for (let i = 0; i < entries.length; i += 128) await tx.put(Object.fromEntries(entries.slice(i, i + 128)));
        if (previous?.chunks > chunks) {
          const removed = Array.from({length:previous.chunks - chunks}, (_, i) => `${name}:part:${chunks + i}`);
          for (let i = 0; i < removed.length; i += 128) await tx.delete(removed.slice(i, i + 128));
        }
        return {accepted:true, ...meta};
      });
      return Response.json({ok:true, ...result});
    } catch (error) {
      return Response.json({ok:false, error:error.message}, {status:400});
    }
  }
}

export function runtimeCheckpointResponse(env, request, kind) {
  const name = `checkpoint:${kind}`;
  const stub = env.RUNTIME_SNAPSHOTS.get(env.RUNTIME_SNAPSHOTS.idFromName(name));
  const target = new URL(`https://runtime/${name}`);
  const upload = request.method === "POST";
  if (upload) target.searchParams.set("ingest_checkpoint", "1");
  return stub.fetch(new Request(target, upload ? {
    method:"POST", body:request.body, duplex:"half", headers:{"content-type":"application/json"},
  } : {}));
}

export async function runtimeDashboardResponse(env, request, upload = false) {
  if (!env.RUNTIME_SNAPSHOTS) return null;
  const url = new URL(request.url);
  const stub = env.RUNTIME_SNAPSHOTS.get(env.RUNTIME_SNAPSHOTS.idFromName("dashboard"));
  const target = new URL("https://runtime/dashboard");
  if (upload) target.searchParams.set("ingest_dashboard", "1");
  else {
    target.searchParams.set("projection", "public");
    for (const field of ["token_key", "history_limit"]) if (url.searchParams.has(field)) target.searchParams.set(field, url.searchParams.get(field));
  }
  return stub.fetch(new Request(target, upload ? {method:"POST", body:request.body, duplex:"half", headers:{"content-type":"application/json"}} : {}));
}

export async function runtimeMetadata(env, name) {
  if (!env.RUNTIME_SNAPSHOTS) return null;
  const stub = env.RUNTIME_SNAPSHOTS.get(env.RUNTIME_SNAPSHOTS.idFromName(name));
  const response = await stub.fetch(new Request(`https://runtime/${encodeURIComponent(name)}?meta=1`));
  return (await response.json()).document;
}

export async function runtimeDocument(env, name, value, updatedAt, revision = 0) {
  if (!env.RUNTIME_SNAPSHOTS) return null;
  const stub = env.RUNTIME_SNAPSHOTS.get(env.RUNTIME_SNAPSHOTS.idFromName(name));
  const response = await stub.fetch(new Request(`https://runtime/${encodeURIComponent(name)}`, value === undefined ? {} : {
    method:"POST", headers:{"content-type":"application/json"},
    body:JSON.stringify({value, updated_at:updatedAt, revision}),
  }));
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || "runtime_storage_unavailable");
  return result;
}
