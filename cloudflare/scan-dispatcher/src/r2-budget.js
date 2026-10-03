const GB = 1_000_000_000;
export const R2_FREE_LIMITS = Object.freeze({class_a:1_000_000, class_b:10_000_000, storage_bytes:10 * GB});
const STOP = 0.9, WARN = 0.8, METADATA_ALLOWANCE = 4096;
const SAFETY_DAYS = 33, DAY_MS = 86400000;
const WRAPPED = Symbol("r2-budget-wrapped");
const KEY = "budget:v1";

export class R2BudgetError extends Error {
  constructor(reason = "r2_monthly_budget_paused") { super(reason); this.status = 503; }
}

function period(now) {
  const date = new Date(now);
  if (!Number.isFinite(date.getTime())) throw new R2BudgetError("r2_budget_clock_invalid");
  return {month:date.toISOString().slice(0,7), resets_at:new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,1)).toISOString()};
}

function objectKey(key) {
  if (typeof key !== "string" || !key || key.length > 1024 || /[\x00-\x1f]/.test(key)) throw new R2BudgetError("r2_budget_key_invalid");
  return key;
}

function amount(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw new R2BudgetError("r2_budget_amount_invalid");
  return value;
}

function initial(now) {
  return {...period(now), initialized:false, class_a:0, class_b:0, storage_bytes:0,
    baseline_at:null, paused:false, pause_reason:null, notifications:[], dispatch_at:null, daily:{}};
}

function rolling(state, now) {
  const today = new Date(now).toISOString().slice(0,10);
  const cutoff = new Date(Date.parse(today)-32*DAY_MS).toISOString().slice(0,10);
  state.daily = Object.fromEntries(Object.entries(state.daily || {}).filter(([day])=>day >= cutoff && day <= today));
  return Object.values(state.daily).reduce((sum,row)=>({class_a:sum.class_a+row.class_a,class_b:sum.class_b+row.class_b}),{class_a:0,class_b:0});
}

function chargeDay(state, now, counter, value) {
  const today = new Date(now).toISOString().slice(0,10);
  state.daily[today] ||= {class_a:0,class_b:0};
  state.daily[today][counter] += value;
}

function notify(state, kind, now) {
  const id = `radar-r2-budget-${state.month}-${kind}`;
  if (!state.notifications.some(item => item.id === id)) {
    const usage = rolling(state,now);
    state.notifications.push({id, kind, month:state.month, created_at:new Date(now).toISOString(),
      class_a:Math.max(state.class_a,usage.class_a), class_b:Math.max(state.class_b,usage.class_b), storage_bytes:state.storage_bytes,
      pause_reason:state.pause_reason, resets_at:state.resets_at, notified_at:null, issue_url:null});
  }
}

function evaluate(state, now) {
  const {month,resets_at} = period(now);
  const previousPaused = state.paused;
  const usage = rolling(state,now);
  const rollingReached = () => ["class_a","class_b"].find(key=>usage[key] > state[key] && usage[key]+1 >= R2_FREE_LIMITS[key]*STOP);
  if (state.paused && state.pause_reason?.startsWith("rolling_") && !rollingReached()) Object.assign(state,{paused:false,pause_reason:null});
  if (state.month !== month) {
    Object.assign(state,{month,resets_at,class_a:0,class_b:0,paused:false,pause_reason:null,dispatch_at:null});
    // Storage is persistent, unlike monthly operation counters.
    state.notifications = state.notifications.filter(item => !item.notified_at || item.month >= month);
  }
  if (!state.initialized) return state;
  const reached = Object.keys(R2_FREE_LIMITS).find(key => state[key] >= R2_FREE_LIMITS[key] * STOP);
  if (reached && !state.paused) Object.assign(state,{paused:true,pause_reason:reached});
  const rollingReason = rollingReached();
  if (rollingReason && !state.paused) Object.assign(state,{paused:true,pause_reason:`rolling_${rollingReason}`});
  if (previousPaused && !state.paused) notify(state,"resumed",now);
  if (state.paused) notify(state,"paused",now);
  else if (Object.keys(R2_FREE_LIMITS).some(key => state[key] >= R2_FREE_LIMITS[key] * WARN)) notify(state,"warning",now);
  return state;
}

function status(state) {
  const rollingUsage = Object.values(state.daily || {}).reduce((sum,row)=>({class_a:sum.class_a+row.class_a,class_b:sum.class_b+row.class_b}),{class_a:0,class_b:0});
  const usage = {class_a:Math.max(state.class_a,rollingUsage.class_a),class_b:Math.max(state.class_b,rollingUsage.class_b),storage_bytes:state.storage_bytes};
  return {enabled:true, initialized:state.initialized, month:state.month,
    status:!state.initialized ? "not_initialized" : state.paused ? "paused" : "active",
    paused:!state.initialized || state.paused, pause_reason:!state.initialized ? "baseline_required" : state.pause_reason,
    stop_pct:90, warning_pct:80, resets_at:state.resets_at, baseline_at:state.baseline_at,
    usage, calendar_usage:{class_a:state.class_a,class_b:state.class_b}, rolling_usage:rollingUsage,rolling_window_days:SAFETY_DAYS,
    limits:R2_FREE_LIMITS, stop_limits:Object.fromEntries(Object.entries(R2_FREE_LIMITS).map(([k,v])=>[k,v*STOP])),
    used_pct:Object.fromEntries(Object.entries(R2_FREE_LIMITS).map(([k,v])=>[k,100*usage[k]/v])),
    accounting:"conservative calendar-month and 33-day reservations; persistent storage upper bound; not a Cloudflare invoice",
    scope:"scanner R2 binding; baseline includes account usage before activation",
    notifications:state.notifications, dispatch_at:state.dispatch_at};
}

export class R2Budget {
  constructor(state) { this.storage = state.storage; }

  async fetch(request) {
    try {
      const url = new URL(request.url), now = Date.now();
      const body = request.method === "POST" ? await request.json() : {};
      const result = await this.storage.transaction(async tx => {
        const previous = await tx.get(KEY);
        const original = JSON.stringify(previous);
        const current = evaluate(previous || initial(now), now);
        let extra = {};
        if (url.pathname === "/bootstrap") {
          if (current.initialized) return {ok:true,unchanged:true,...status(current)};
          if (body.month !== current.month || !Array.isArray(body.objects) || body.objects.length > 10000) throw new R2BudgetError("r2_budget_baseline_invalid");
          const seen = new Set();
          let stored = 0;
          let catalogue = {};
          for (const row of body.objects) {
            const key = objectKey(row.key), bytes = amount(row.size) + METADATA_ALLOWANCE;
            if (seen.has(key)) throw new R2BudgetError("r2_budget_duplicate_baseline_key");
            seen.add(key); stored += bytes;
            catalogue[`object:${key}`] = bytes;
            if (Object.keys(catalogue).length === 128) { await tx.put(catalogue); catalogue = {}; }
          }
          if (Object.keys(catalogue).length) await tx.put(catalogue);
          Object.assign(current,{initialized:true,class_a:amount(body.class_a),class_b:amount(body.class_b),
            storage_bytes:Math.max(stored,amount(body.account_storage_bytes)),baseline_at:new Date(now).toISOString()});
          chargeDay(current,now,"class_a",current.class_a);chargeDay(current,now,"class_b",current.class_b);
          evaluate(current,now);
        } else if (url.pathname === "/reserve") {
          const kind = body.kind;
          if (!["get","head","put","list","delete"].includes(kind)) throw new R2BudgetError("r2_budget_operation_unsupported");
          const key = kind === "list" ? null : objectKey(body.key);
          if (kind !== "delete" && (!current.initialized || current.paused)) extra = {allowed:false,error:!current.initialized ? "r2_budget_baseline_required" : "r2_monthly_budget_paused"};
          else if (kind !== "delete") {
            const counter = ["put","list"].includes(kind) ? "class_a" : "class_b";
            const oldBytes = kind === "put" ? await tx.get(`object:${key}`) || 0 : 0;
            const bytes = kind === "put" ? amount(body.bytes) + METADATA_ALLOWANCE : 0;
            const delta = Math.max(0,bytes-oldBytes);
            const reason = current[counter]+1 >= R2_FREE_LIMITS[counter]*STOP ? counter
              : rolling(current,now)[counter]+1 >= R2_FREE_LIMITS[counter]*STOP ? `rolling_${counter}`
              : current.storage_bytes+delta >= R2_FREE_LIMITS.storage_bytes*STOP ? "storage_bytes" : null;
            if (reason) {
              Object.assign(current,{paused:true,pause_reason:reason});
              notify(current,"paused",now);
              extra = {allowed:false,error:"r2_monthly_budget_paused"};
            } else {
              current[counter]++; current.storage_bytes += delta;
              chargeDay(current,now,counter,1);
              // Uncertain PUT outcomes stay charged. Never refund a failed network request.
              if (kind === "put") await tx.put({[`object:${key}`]:Math.max(oldBytes,bytes)});
              evaluate(current,now); extra = {allowed:true};
            }
          } else extra = {allowed:true}; // R2 DELETE is free; existing safe GC may reclaim storage.
        } else if (url.pathname === "/deleted") {
          for (const key of Array.isArray(body.keys) ? body.keys : []) {
            objectKey(key);
            const bytes = await tx.get(`object:${key}`) || 0;
            if (bytes) { current.storage_bytes = Math.max(0,current.storage_bytes-bytes); await tx.delete([`object:${key}`]); }
          }
        } else if (url.pathname === "/ack") {
          const item = current.notifications.find(row => row.id === body.id);
          if (!item || !/^https:\/\/github\.com\/gmirash-debug\/solana-radar\/issues\/\d+$/.test(body.issue_url || "")) throw new R2BudgetError("r2_budget_notification_ack_invalid");
          Object.assign(item,{notified_at:new Date(now).toISOString(),issue_url:body.issue_url});
        } else if (url.pathname === "/claim-dispatch") {
          const pending = current.notifications.some(row => !row.notified_at);
          const allowed = pending && (!current.dispatch_at || now-Date.parse(current.dispatch_at) >= 15*60*1000);
          if (allowed) current.dispatch_at = new Date(now).toISOString();
          extra = {dispatch:allowed};
        } else if (url.pathname !== "/status") throw new R2BudgetError("r2_budget_endpoint_invalid");
        if (JSON.stringify(current) !== original) await tx.put({[KEY]:current});
        return {ok:true,...status(current),...extra};
      });
      return Response.json(result);
    } catch (error) {
      return Response.json({ok:false,error:error instanceof R2BudgetError ? error.message : "r2_budget_unavailable"},{status:503});
    }
  }
}

export async function r2BudgetCall(env, path, body) {
  if (!env.R2_BUDGET?.idFromName || !env.R2_BUDGET?.get) throw new R2BudgetError("r2_budget_not_configured");
  try {
    const stub = env.R2_BUDGET.get(env.R2_BUDGET.idFromName("r2-account-budget-v1"));
    const response = await stub.fetch(new Request(`https://r2-budget/${path}`,body === undefined ? {}
      : {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)}));
    const result = await response.json();
    if (!response.ok || !result.ok) throw new R2BudgetError(result.error || "r2_budget_unavailable");
    return result;
  } catch (error) { throw error instanceof R2BudgetError ? error : new R2BudgetError("r2_budget_unavailable"); }
}

export async function r2BudgetStatus(env) {
  if (env.R2_BUDGET_GUARD !== "enabled") return {enabled:false};
  return r2BudgetCall(env,"status");
}

function byteLength(value) {
  if (typeof value === "string") return new TextEncoder().encode(value).byteLength;
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) return value.byteLength;
  if (value instanceof Blob) return value.size;
  throw new R2BudgetError("r2_budget_unbounded_put_refused");
}

export function guardR2Env(env) {
  if (env.R2_BUDGET_GUARD && !["enabled","disabled"].includes(env.R2_BUDGET_GUARD)) throw new R2BudgetError("r2_budget_mode_invalid");
  if (env.R2_BUDGET_GUARD !== "enabled" || !env.RADAR_ARCHIVE || env.RADAR_ARCHIVE[WRAPPED]) return env;
  const store = env.RADAR_ARCHIVE;
  const wrapper = {[WRAPPED]:true};
  for (const method of ["head","get","put","list","delete"]) {
    wrapper[method] = async (...args) => {
      const keys = method === "delete" && Array.isArray(args[0]) ? args[0] : [args[0]];
      if (keys.length > 1000) throw new R2BudgetError("r2_budget_batch_too_large");
      if (method === "put" && args[2]?.storageClass && args[2].storageClass !== "Standard") throw new R2BudgetError("r2_budget_infrequent_access_refused");
      for (const key of method === "list" ? [null] : keys) {
        const result = await r2BudgetCall(env,"reserve",{kind:method,key,
          ...(method === "put" ? {bytes:byteLength(args[1])} : {})});
        if (!result.allowed) throw new R2BudgetError(result.error);
      }
      const result = await store[method](...args);
      if (method === "delete") await r2BudgetCall(env,"deleted",{keys});
      return result;
    };
  }
  return {...env,RADAR_ARCHIVE:wrapper};
}

export async function dispatchR2BudgetNotice(env, fetchFn = globalThis.fetch.bind(globalThis)) {
  if (env.R2_BUDGET_GUARD !== "enabled") return {enabled:false};
  const claimed = await r2BudgetCall(env,"claim-dispatch",{});
  if (!claimed.dispatch) return {enabled:true,dispatched:false};
  if (!env.GITHUB_TOKEN) return {enabled:true,dispatched:false,error:"notification_dispatch_not_configured"};
  const owner = env.GITHUB_OWNER || "gmirash-debug", repo = env.GITHUB_REPO || "solana-radar";
  const response = await fetchFn(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/r2-budget-monitor.yml/dispatches`,{
    method:"POST",redirect:"manual",signal:AbortSignal.timeout(10000),
    headers:{accept:"application/vnd.github+json",authorization:`Bearer ${env.GITHUB_TOKEN}`,"user-agent":"SolanaRadar-R2-Budget"},
    body:JSON.stringify({ref:env.GITHUB_REF || "main"}),
  });
  return {enabled:true,dispatched:response.status === 204,status:response.status};
}
