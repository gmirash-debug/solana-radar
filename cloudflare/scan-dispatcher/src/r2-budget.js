const GB = 1_000_000_000;
export const R2_FREE_LIMITS = Object.freeze({class_a:1_000_000, class_b:10_000_000, storage_bytes:10 * GB});
const STOP = 0.9, WARN = 0.8, METADATA_ALLOWANCE = 4096;
const SAFETY_DAYS = 33, DAY_MS = 86400000;
const WRAPPED = Symbol("r2-budget-wrapped");
const STORES = new WeakMap();
const KEY = "budget:v1";
export const R2_BATCH_LIMITS = Object.freeze({operations:128, putBytes:32 * 1024 * 1024,
  durationMs:30000, boundaryMarginMs:1000});

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

function catalogueEntry(value) {
  if (value === undefined) return {bytes:0, retained:false};
  if (typeof value === "number") return {bytes:amount(value), retained:false};
  if (value?.retained === true) return {bytes:amount(value.bytes), retained:true};
  throw new R2BudgetError("r2_budget_catalogue_invalid");
}

function batchPlan(operations) {
  if (!Array.isArray(operations) || !operations.length || operations.length > R2_BATCH_LIMITS.operations) {
    throw new R2BudgetError("r2_budget_batch_too_large");
  }
  let putBytes = 0;
  return Array.from(operations,op => {
    if (!["get","head","put","list"].includes(op?.kind)) throw new R2BudgetError("r2_budget_operation_unsupported");
    const key = op.kind === "list" ? null : objectKey(op.key);
    if (op.kind !== "put") return {kind:op.kind, key};
    const bytes = amount(op.bytes);
    putBytes += bytes;
    if (putBytes > R2_BATCH_LIMITS.putBytes) throw new R2BudgetError("r2_budget_batch_bytes_exceeded");
    return {kind:op.kind, key, bytes};
  });
}

function batchExpiry(now, deadline) {
  if (deadline !== undefined && (!Number.isSafeInteger(deadline) || deadline <= now)) {
    throw new R2BudgetError("r2_budget_batch_expired");
  }
  // Daily attribution must not outlive its UTC day (and therefore its calendar month).
  const nextDay = (Math.floor(now / DAY_MS) + 1) * DAY_MS;
  const expires = Math.min(now + R2_BATCH_LIMITS.durationMs,
    nextDay - R2_BATCH_LIMITS.boundaryMarginMs, deadline ?? Infinity);
  if (expires <= now) throw new R2BudgetError("r2_budget_batch_expired");
  return expires;
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
  else if (Object.keys(R2_FREE_LIMITS).some(key => Math.max(state[key],usage[key] || 0) >= R2_FREE_LIMITS[key] * WARN)) notify(state,"warning",now);
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
    accounting:"conservative calendar-month and 33-day reservations; unused grants and batch PUT storage high-water marks are never refunded; not a Cloudflare invoice",
    scope:"scanner R2 binding; baseline includes account usage before activation",
    notifications:state.notifications, dispatch_at:state.dispatch_at};
}

export class R2Budget {
  constructor(state) { this.storage = state.storage; }

  async fetch(request) {
    try {
      const url = new URL(request.url);
      const body = request.method === "POST" ? await request.json() : {};
      if (url.pathname === "/status" && request.method === "GET") {
        // Monitoring remains strictly read-only, including month rollover.
        // Persist derived transitions on the next reservation/dispatch, not GET.
        const previous = await this.storage.get(KEY);
        const now = Date.now();
        return Response.json({ok:true,...status(evaluate(previous ? structuredClone(previous) : initial(now), now))});
      }
      const result = await this.storage.transaction(async tx => {
        const now = Date.now();
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
            const old = kind === "put" ? catalogueEntry(await tx.get(`object:${key}`)) : {bytes:0};
            const oldBytes = old.bytes;
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
              if (kind === "put") await tx.put({[`object:${key}`]:old.retained
                ? {bytes:Math.max(oldBytes,bytes),retained:true} : Math.max(oldBytes,bytes)});
              evaluate(current,now); extra = {allowed:true};
            }
          } else extra = {allowed:true}; // R2 DELETE is free; existing safe GC may reclaim storage.
        } else if (url.pathname === "/reserve-batch") {
          const operations = batchPlan(body.operations), expires_at = batchExpiry(now,body.deadline);
          if (body.invocation_id !== undefined && !/^[a-f0-9-]{36}$/.test(body.invocation_id)) {
            throw new R2BudgetError("r2_budget_batch_invocation_invalid");
          }
          if (!current.initialized || current.paused) extra = {allowed:false,
            error:!current.initialized ? "r2_budget_baseline_required" : "r2_monthly_budget_paused"};
          else {
            const counts = {class_a:0,class_b:0}, puts = new Map(), catalogue = {};
            for (const op of operations) {
              counts[["put","list"].includes(op.kind) ? "class_a" : "class_b"]++;
              if (op.kind === "put") puts.set(op.key,Math.max(puts.get(op.key) || 0,op.bytes + METADATA_ALLOWANCE));
            }
            let delta = 0;
            for (const [key,bytes] of puts) {
              const old = catalogueEntry(await tx.get(`object:${key}`));
              delta += Math.max(0,bytes-old.bytes);
              // Retain a tombstone even after DELETE: an admitted PUT can finish late or have an uncertain outcome.
              // Immutable-key replay can reuse this upper bound, but never the operation allowance.
              if (!old.retained || bytes > old.bytes) catalogue[`object:${key}`] = {bytes:Math.max(old.bytes,bytes),retained:true};
            }
            const usage = rolling(current,now);
            const reason = ["class_a","class_b"].find(key=>current[key]+counts[key] >= R2_FREE_LIMITS[key]*STOP)
              || ["class_a","class_b"].filter(key=>usage[key]+counts[key] >= R2_FREE_LIMITS[key]*STOP).map(key=>`rolling_${key}`)[0]
              || (current.storage_bytes+delta >= R2_FREE_LIMITS.storage_bytes*STOP ? "storage_bytes" : null);
            if (reason) {
              Object.assign(current,{paused:true,pause_reason:reason}); notify(current,"paused",now);
              extra = {allowed:false,error:"r2_monthly_budget_paused"};
            } else {
              for (const key of ["class_a","class_b"]) {current[key] += counts[key];chargeDay(current,now,key,counts[key]);}
              current.storage_bytes += delta;
              const catalog_rows_written = Object.keys(catalogue).length;
              if (catalog_rows_written) await tx.put(catalogue);
              evaluate(current,now);
              extra = {allowed:true,grant:{invocation_id:body.invocation_id,issued_at:now,expires_at,operations:operations.length,
                ...counts,storage_bytes:delta,catalog_rows_written,budget_rows_written:1}};
            }
          }
        } else if (url.pathname === "/deleted") {
          for (const key of Array.isArray(body.keys) ? body.keys : []) {
            objectKey(key);
            const {bytes,retained} = catalogueEntry(await tx.get(`object:${key}`));
            if (bytes && !retained) { current.storage_bytes = Math.max(0,current.storage_bytes-bytes); await tx.delete([`object:${key}`]); }
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
  STORES.set(wrapper,{store});
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

// The callback's environment is ephemeral. It is not a token/cache and cannot be used after return.
// Replaying a reservation request creates a new charge; no caller-supplied grant/id can authorize I/O.
export async function withR2BudgetBatch(env, operations, action, options = {}) {
  const guarded = guardR2Env(env), plan = batchPlan(operations);
  if (typeof action !== "function") throw new R2BudgetError("r2_budget_batch_callback_invalid");
  if (env.R2_BUDGET_GUARD !== "enabled") return action(guarded);
  const info = STORES.get(guarded.RADAR_ARCHIVE);
  if (!info || info.batch) throw new R2BudgetError("r2_budget_batch_scope_invalid");
  const started = Date.now(), deadline = batchExpiry(started,options.deadline);
  const invocation_id = crypto.randomUUID();
  let result, timer;
  try {
    // A timed-out authority call may still commit. Abandon it without I/O or a refund.
    result = await Promise.race([r2BudgetCall(env,"reserve-batch",{operations:plan,deadline,invocation_id}),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new R2BudgetError("r2_budget_batch_expired")),deadline-started);})]);
  } finally { clearTimeout(timer); }
  if (!result.allowed) throw new R2BudgetError(result.error);
  const grant = result.grant;
  const counts = plan.reduce((n,op)=>{n[["put","list"].includes(op.kind) ? "class_a" : "class_b"]++;return n;},{class_a:0,class_b:0});
  if (!grant || grant.invocation_id !== invocation_id || grant.operations !== plan.length || grant.class_a !== counts.class_a || grant.class_b !== counts.class_b
      || !Number.isSafeInteger(grant.issued_at) || grant.issued_at < started
      || !Number.isSafeInteger(grant.expires_at) || grant.expires_at > deadline || grant.expires_at <= grant.issued_at
      || result.month !== period(grant.issued_at).month) throw new R2BudgetError("r2_budget_batch_grant_invalid");
  if (Date.now() < grant.issued_at || Date.now() >= grant.expires_at) throw new R2BudgetError("r2_budget_batch_expired");
  const slots = plan.map(op=>({...op,used:false})), wrapper = {[WRAPPED]:true};
  let open = true;
  STORES.set(wrapper,{store:info.store,batch:true});
  for (const method of ["head","get","put","list","delete"]) wrapper[method] = async (...args) => {
    if (!open) throw new R2BudgetError("r2_budget_batch_closed");
    if (Date.now() < grant.issued_at || Date.now() >= grant.expires_at) throw new R2BudgetError("r2_budget_batch_expired");
    if (method === "put" && args[2]?.storageClass && args[2].storageClass !== "Standard") throw new R2BudgetError("r2_budget_infrequent_access_refused");
    const key = method === "list" ? null : objectKey(args[0]);
    const bytes = method === "put" ? byteLength(args[1]) : 0;
    const slot = slots.find(op=>!op.used && op.kind === method && op.key === key && (method !== "put" || bytes <= op.bytes));
    if (!slot) throw new R2BudgetError("r2_budget_batch_operation_not_reserved");
    // Consume synchronously, before any native call/await; failures cannot make a slot reusable.
    slot.used = true;
    return info.store[method](...args);
  };
  try { return await action({...guarded,RADAR_ARCHIVE:wrapper}); }
  finally { open = false; }
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
