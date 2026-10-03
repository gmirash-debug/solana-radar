// Fetch-only Hrana v2 adapter. Each call owns and closes its SQL stream; writes
// are never retried here because an interrupted response may hide a commit.
const MAX_BATCH_STATEMENTS = 100;
const MAX_REQUEST_BYTES = 16 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
const MAX_PARAMETERS = 32766;
const FOREIGN_KEYS_SQL = "PRAGMA foreign_keys = ON";
const MIN_INTEGER = -(1n << 63n);
const MAX_INTEGER = (1n << 63n) - 1n;
const encoder = new TextEncoder();

export class StorageSqlError extends Error {
  constructor(code, {status, sqlCode, step, retryable = false, outcomeUnknown = false} = {}) {
    super(sqlCode ? `${code}:${sqlCode}` : code);
    this.name = "StorageSqlError";
    this.code = code;
    if (status !== undefined) this.status = status;
    if (sqlCode !== undefined) this.sqlCode = sqlCode;
    if (step !== undefined) this.step = step;
    this.retryable = retryable;
    this.outcomeUnknown = outcomeUnknown;
  }
}

function invalid(code) { throw new StorageSqlError(code); }
function protocolInvalid() { throw new StorageSqlError("storage_sql_invalid_response", {outcomeUnknown:true}); }

function databaseEndpoint(value) {
  if (typeof value !== "string" || !value.trim() || /[\x00-\x20\x7f]/.test(value.trim())) {
    invalid("storage_sql_database_url_invalid");
  }
  let url;
  try { url = new URL(value.trim().replace(/^libsql:/i, "https:")); }
  catch { invalid("storage_sql_database_url_invalid"); }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search || url.hash
      || !["", "/", "/v2/pipeline", "/v2/pipeline/"].includes(url.pathname)) {
    invalid("storage_sql_database_url_invalid");
  }
  url.pathname = "/v2/pipeline";
  return url.href;
}

// Split SQL without splitting quoted strings, comments or trigger bodies.
// Transaction controls are rejected below: the adapter owns the transaction.
function sqlStatements(sql) {
  if (typeof sql !== "string" || sql.includes("\0") || encoder.encode(sql).byteLength > MAX_REQUEST_BYTES) {
    invalid("storage_sql_statement_invalid");
  }
  const statements = [];
  let start = 0;
  let words = [];
  let meaningful = false;
  let trigger = false;
  let triggerBody = false;
  let depth = 0;
  const finish = end => {
    if (meaningful) statements.push({sql:sql.slice(start, end).trim(), words});
    start = end + 1;
    words = [];
    meaningful = trigger = triggerBody = false;
    depth = 0;
  };
  for (let i = 0; i < sql.length; i++) {
    const c = sql[i];
    if (/\s/.test(c)) continue;
    if (c === "-" && sql[i + 1] === "-") {
      while (i < sql.length && sql[i] !== "\n" && sql[i] !== "\r") i++;
      continue;
    }
    if (c === "/" && sql[i + 1] === "*") {
      const end = sql.indexOf("*/", i + 2);
      if (end < 0) invalid("storage_sql_unterminated_comment");
      i = end + 1;
      continue;
    }
    if (c === "'" || c === '"' || c === "`" || c === "[") {
      meaningful = true;
      const close = c === "[" ? "]" : c;
      let closed = false;
      while (++i < sql.length) {
        if (sql[i] !== close) continue;
        if (c !== "[" && sql[i + 1] === close) { i++; continue; }
        closed = true;
        break;
      }
      if (!closed) invalid("storage_sql_unterminated_quote");
      continue;
    }
    if (/[a-zA-Z_]/.test(c)) {
      const begin = i;
      while (i + 1 < sql.length && /[a-zA-Z_0-9$]/.test(sql[i + 1])) i++;
      const word = sql.slice(begin, i + 1).toUpperCase();
      meaningful = true;
      words.push(word);
      trigger ||= words[0] === "CREATE" && (words[1] === "TRIGGER"
        || (["TEMP", "TEMPORARY"].includes(words[1]) && words[2] === "TRIGGER"));
      if (trigger) {
        if (!triggerBody && word === "BEGIN") { triggerBody = true; depth = 1; }
        else if (triggerBody && word === "CASE") depth++;
        else if (triggerBody && word === "END") depth--;
      }
      continue;
    }
    if (c === ";" && depth === 0) { finish(i); continue; }
    meaningful = true;
  }
  if (depth !== 0) invalid("storage_sql_unterminated_trigger");
  finish(sql.length);
  return statements;
}

function validateStatement({sql, words}, {inBatch = false} = {}) {
  const first = words[0];
  if (["BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "ATTACH", "DETACH"].includes(first)) {
    invalid("storage_sql_transaction_control_unsupported");
  }
  if (inBatch && ["VACUUM", "PRAGMA"].includes(first)) {
    invalid("storage_sql_batch_statement_unsupported");
  }
  return sql;
}

function integer(value, response = false) {
  let parsed;
  if (typeof value !== "string" || !/^-?(0|[1-9]\d*)$/.test(value) || value.length > 20) {
    if (response) protocolInvalid();
    invalid("storage_sql_integer_invalid");
  }
  try { parsed = BigInt(value); } catch { if (response) protocolInvalid(); invalid("storage_sql_integer_invalid"); }
  if (parsed < MIN_INTEGER || parsed > MAX_INTEGER) {
    if (response) protocolInvalid();
    invalid("storage_sql_integer_out_of_range");
  }
  return parsed;
}

function base64(bytes) {
  let value = "";
  for (let i = 0; i < bytes.length; i += 8192) value += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(value);
}

function encodeValue(value) {
  if (value === null) return {type:"null"};
  if (typeof value === "string") return {type:"text", value};
  if (typeof value === "bigint") {
    integer(value.toString());
    return {type:"integer", value:value.toString()};
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    if (!Number.isInteger(value)) return {type:"float", value};
    if (!Number.isSafeInteger(value)) invalid("storage_sql_unsafe_integer_parameter");
    return {type:"integer", value:String(value)};
  }
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    const bytes = value instanceof ArrayBuffer ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    return {type:"blob", base64:base64(bytes)};
  }
  invalid("storage_sql_parameter_type_unsupported");
}

function decodeValue(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) protocolInvalid();
  switch (value.type) {
    case "null": return null;
    case "integer": {
      const parsed = integer(value.value, true);
      // Keep large SQLite integers lossless and JSON-safe, rather than rounding.
      return parsed >= BigInt(Number.MIN_SAFE_INTEGER) && parsed <= BigInt(Number.MAX_SAFE_INTEGER)
        ? Number(parsed) : parsed.toString();
    }
    case "float":
      if (typeof value.value !== "number" || !Number.isFinite(value.value)) protocolInvalid();
      return value.value;
    case "text":
      if (typeof value.value !== "string") protocolInvalid();
      return value.value;
    case "blob": {
      if (typeof value.base64 !== "string" || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.base64)) {
        protocolInvalid();
      }
      let decoded;
      try { decoded = atob(value.base64); } catch { protocolInvalid(); }
      return Uint8Array.from(decoded, c => c.charCodeAt(0)).buffer;
    }
    default: protocolInvalid();
  }
}

function count(value) {
  if (!Number.isSafeInteger(value) || value < 0) protocolInvalid();
  return value;
}

function decodeResult(result, elapsed) {
  if (!result || !Array.isArray(result.cols) || !Array.isArray(result.rows)) protocolInvalid();
  const names = result.cols.map(col => {
    if (!col || (typeof col.name !== "string" && col.name !== null)) protocolInvalid();
    return col.name ?? "";
  });
  const rows = result.rows.map(row => {
    if (!Array.isArray(row) || row.length !== names.length) protocolInvalid();
    return row.map(decodeValue);
  });
  const changes = count(result.affected_row_count);
  const meta = {
    changes,
    rows_written:result.rows_written === undefined ? changes : count(result.rows_written),
    rows_read:result.rows_read === undefined ? rows.length : count(result.rows_read),
    duration:result.query_duration_ms === undefined ? elapsed : result.query_duration_ms,
    last_row_id:result.last_insert_rowid == null ? 0 : decodeValue({type:"integer", value:result.last_insert_rowid}),
  };
  if (typeof meta.duration !== "number" || !Number.isFinite(meta.duration) || meta.duration < 0) protocolInvalid();
  // Hrana v2 does not guarantee index-inclusive counters. Do not label these
  // fallback values as measured provider billing or actual scanned row counts.
  if (result.rows_written === undefined) meta.rows_written_estimated = true;
  if (result.rows_read === undefined) meta.rows_read_estimated = true;
  return {success:true, results:rows.map(row => Object.fromEntries(names.map((name, i) => [name, row[i]]))),
    meta, names, rows};
}

function publicResult(result) { return {success:result.success, results:result.results, meta:result.meta}; }

async function readResponse(response, signal) {
  const contentLength = response.headers?.get("content-length");
  if (contentLength && (!/^\d+$/.test(contentLength) || Number(contentLength) > MAX_RESPONSE_BYTES)) {
    throw new StorageSqlError("storage_sql_response_too_large", {outcomeUnknown:true});
  }
  if (!response.body?.getReader) protocolInvalid();
  const reader = response.body.getReader();
  const abort = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", abort, {once:true});
  if (signal.aborted) abort();
  const decoder = new TextDecoder("utf-8", {fatal:true});
  let bytes = 0;
  let text = "";
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > MAX_RESPONSE_BYTES) throw new StorageSqlError("storage_sql_response_too_large", {outcomeUnknown:true});
      try { text += decoder.decode(part.value, {stream:true}); } catch { protocolInvalid(); }
    }
    try { text += decoder.decode(); } catch { protocolInvalid(); }
    try { return JSON.parse(text); } catch { protocolInvalid(); }
  } finally {
    signal.removeEventListener("abort", abort);
    try { await reader.cancel(); } catch { /* Already closed or aborted. */ }
    reader.releaseLock();
  }
}

class TursoStatement {
  #db;
  #sql;
  #args;
  constructor(db, sql, args = []) { this.#db = db; this.#sql = sql; this.#args = args; }
  bind(...values) {
    if (values.length > MAX_PARAMETERS) invalid("storage_sql_too_many_parameters");
    return new TursoStatement(this.#db, this.#sql, values.map(encodeValue));
  }
  descriptor(db, inBatch = false, wantRows = true) {
    if (db !== this.#db) invalid("storage_sql_batch_database_mismatch");
    const sql = validateStatement(sqlStatements(this.#sql)[0], {inBatch});
    return {sql, args:this.#args, want_rows:wantRows};
  }
  async run() { return publicResult(await this.#db.execute(this)); }
  async all() { return publicResult(await this.#db.execute(this)); }
  async first(column) {
    if (column !== undefined && typeof column !== "string") invalid("storage_sql_first_column_invalid");
    const result = await this.#db.execute(this);
    if (column !== undefined && !result.names.includes(column)) invalid("storage_sql_first_column_missing");
    if (!result.results.length) return null;
    return column === undefined ? result.results[0] : result.results[0][column];
  }
  async raw(options = {}) {
    if (!options || typeof options !== "object" || (options.columnNames !== undefined && typeof options.columnNames !== "boolean")) {
      invalid("storage_sql_raw_options_invalid");
    }
    const result = await this.#db.execute(this);
    return options.columnNames ? [result.names, ...result.rows] : result.rows;
  }
}

class TursoDatabase {
  #endpoint;
  #token;
  #fetch;
  #timeout;
  constructor(env, options) {
    if (typeof env?.TURSO_DATABASE_URL !== "string" || !env.TURSO_DATABASE_URL.trim()
        || typeof env?.TURSO_AUTH_TOKEN !== "string" || !env.TURSO_AUTH_TOKEN.trim()) {
      invalid("storage_sql_turso_credentials_missing");
    }
    this.#endpoint = databaseEndpoint(env.TURSO_DATABASE_URL);
    this.#token = env.TURSO_AUTH_TOKEN;
    if (/[\x00-\x20\x7f]/.test(this.#token)) invalid("storage_sql_auth_token_invalid");
    const fetchImpl = options.fetch ?? options.fetchImpl ?? globalThis.fetch;
    if (typeof fetchImpl !== "function") invalid("storage_sql_fetch_missing");
    // Workers' native fetch rejects a database instance as its receiver.
    this.#fetch = fetchImpl.bind(globalThis);
    this.#timeout = Number(options.timeoutMs ?? env.TURSO_REQUEST_TIMEOUT_MS ?? 15000);
    if (!Number.isSafeInteger(this.#timeout) || this.#timeout < 1 || this.#timeout > 60000) {
      invalid("storage_sql_timeout_invalid");
    }
  }
  prepare(sql) {
    const statements = sqlStatements(sql);
    if (statements.length !== 1) invalid("storage_sql_prepare_requires_single_statement");
    return new TursoStatement(this, validateStatement(statements[0]));
  }
  #sqlFailure(error, step) {
    if (!error || typeof error !== "object" || typeof error.message !== "string") protocolInvalid();
    const candidate = error.code;
    const sqlCode = typeof candidate === "string" && /^[A-Z0-9_]{1,80}$/.test(candidate)
      && !candidate.includes(this.#token) ? candidate : "UNKNOWN";
    // Remote messages can echo SQL values, URLs or tokens. Surface only a code.
    return new StorageSqlError("storage_sql_statement_failed", {sqlCode, step,
      outcomeUnknown:/^SQLITE_IOERR/.test(sqlCode),
      retryable:/^(SQLITE_BUSY|SQLITE_LOCKED|SQLITE_IOERR)/.test(sqlCode)});
  }
  async #pipeline(request) {
    const requests = [request, {type:"close"}];
    const body = JSON.stringify({baton:null, requests});
    if (encoder.encode(body).byteLength > MAX_REQUEST_BYTES) invalid("storage_sql_request_too_large");
    const controller = new AbortController();
    let timer;
    const started = performance.now();
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new StorageSqlError("storage_sql_timeout", {retryable:true, outcomeUnknown:true}));
      }, this.#timeout);
    });
    try {
      const payload = await Promise.race([(async () => {
        const response = await this.#fetch(this.#endpoint, {method:"POST", redirect:"error", signal:controller.signal,
          headers:{Authorization:`Bearer ${this.#token}`, "Content-Type":"application/json", Accept:"application/json"}, body});
        if (!response || typeof response.status !== "number") protocolInvalid();
        if (response.status < 200 || response.status >= 300) {
          try { await response.body?.cancel(); } catch { /* No response payload retained. */ }
          throw new StorageSqlError("storage_sql_http_error", {status:response.status,
            retryable:response.status === 408 || response.status === 429 || response.status >= 500,
            outcomeUnknown:response.status !== 401 && response.status !== 403 && response.status !== 400});
        }
        return readResponse(response, controller.signal);
      })(), timeout]);
      if (!payload || !Array.isArray(payload.results) || payload.results.length !== requests.length || payload.baton !== null) {
        protocolInvalid();
      }
      for (let i = 0; i < payload.results.length; i++) {
        const item = payload.results[i];
        if (item?.type === "error") {
          const failure = this.#sqlFailure(item.error, i);
          failure.outcomeUnknown = true;
          throw failure;
        }
        if (item?.type !== "ok" || item.response?.type !== requests[i].type) protocolInvalid();
      }
      return {result:payload.results[0].response.result, elapsed:performance.now() - started};
    } catch (error) {
      if (error instanceof StorageSqlError) throw error;
      // Do not attach the original error: fetch failures may include secret URLs.
      throw new StorageSqlError("storage_sql_network_error", {retryable:true, outcomeUnknown:true});
    } finally { clearTimeout(timer); }
  }
  async execute(statement) {
    if (!(statement instanceof TursoStatement)) invalid("storage_sql_statement_invalid");
    const steps = [{stmt:{sql:FOREIGN_KEYS_SQL, args:[], want_rows:false}},
      {condition:{type:"ok", step:0}, stmt:statement.descriptor(this)}];
    const {result, elapsed} = await this.#pipeline({type:"batch", batch:{steps}});
    this.#checkSteps(result, steps.length);
    for (let i = 0; i < steps.length; i++) {
      if (result.step_errors[i] !== null) throw this.#sqlFailure(result.step_errors[i], i);
      if (result.step_results[i] === null) protocolInvalid();
    }
    decodeResult(result.step_results[0], elapsed);
    return decodeResult(result.step_results[1], elapsed);
  }
  #checkSteps(result, length) {
    if (!result || !Array.isArray(result.step_results) || !Array.isArray(result.step_errors)
        || result.step_results.length !== length || result.step_errors.length !== length) protocolInvalid();
    for (let i = 0; i < length; i++) {
      if (result.step_results[i] !== null && result.step_errors[i] !== null) protocolInvalid();
    }
  }
  async batch(statements) {
    if (!Array.isArray(statements) || statements.length > MAX_BATCH_STATEMENTS) invalid("storage_sql_batch_invalid");
    if (!statements.length) return [];
    const descriptors = statements.map(statement => {
      if (!(statement instanceof TursoStatement)) invalid("storage_sql_batch_statement_invalid");
      return statement.descriptor(this, true);
    });
    const steps = [{stmt:{sql:FOREIGN_KEYS_SQL, args:[], want_rows:false}},
      {condition:{type:"ok", step:0}, stmt:{sql:"BEGIN IMMEDIATE", args:[], want_rows:false}}];
    descriptors.forEach((stmt, i) => steps.push({condition:{type:"ok", step:i + 1}, stmt}));
    const commit = steps.length;
    steps.push({condition:{type:"ok", step:commit - 1}, stmt:{sql:"COMMIT", args:[], want_rows:false}});
    steps.push({condition:{type:"and", conds:[{type:"ok", step:1}, {type:"not", cond:{type:"ok", step:commit}}]},
      stmt:{sql:"ROLLBACK", args:[], want_rows:false}});
    const {result, elapsed} = await this.#pipeline({type:"batch", batch:{steps}});
    this.#checkSteps(result, steps.length);
    for (let i = 0; i < steps.length; i++) {
      if (result.step_errors[i] !== null) {
        const failure = this.#sqlFailure(result.step_errors[i], i);
        if (i >= commit) failure.outcomeUnknown = true;
        throw failure;
      }
      if (i <= commit && result.step_results[i] === null) protocolInvalid();
    }
    if (result.step_results[commit + 1] !== null) protocolInvalid();
    decodeResult(result.step_results[0], elapsed);
    decodeResult(result.step_results[1], elapsed);
    decodeResult(result.step_results[commit], elapsed);
    return result.step_results.slice(2, commit).map(row => publicResult(decodeResult(row, elapsed)));
  }
  async exec(sql) {
    const parsed = sqlStatements(sql);
    const statements = [];
    for (const row of parsed) {
      if (row.words[0] === "PRAGMA") {
        // The historical migration declares this before CREATE. Each stream
        // already enables it before BEGIN, so the declaration is not discarded.
        const declaration = row.sql.replace(/\/\*[\s\S]*?\*\/|--[^\r\n]*/g, " ");
        if (row.words.join(" ") !== "PRAGMA FOREIGN_KEYS ON" || !/^\s*PRAGMA\s+foreign_keys\s*=\s*ON\s*$/i.test(declaration)) {
          invalid("storage_sql_exec_pragma_unsupported");
        }
      } else statements.push(validateStatement(row, {inBatch:true}));
    }
    if (parsed.length > MAX_BATCH_STATEMENTS) invalid("storage_sql_batch_invalid");
    if (!parsed.length) return {count:0, duration:0};
    const started = performance.now();
    if (statements.length) await this.batch(statements.map(statement => this.prepare(statement)));
    else await this.prepare(FOREIGN_KEYS_SQL).run();
    return {count:parsed.length, duration:performance.now() - started};
  }
}

export function createTursoDatabase(env, options = {}) { return new TursoDatabase(env, options); }

export function backendEnv(env, options = {}) {
  const backend = env?.STORAGE_SQL_BACKEND;
  if (backend === undefined || backend === null || backend === "" || backend === "d1") return env;
  if (backend !== "turso") invalid("storage_sql_backend_invalid");
  const db = createTursoDatabase(env, options);
  return {...env, RADAR_DB:db, RADAR_HISTORY_DB:db};
}

export const resolveStorageEnv = backendEnv;
