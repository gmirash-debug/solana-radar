CREATE TABLE IF NOT EXISTS runtime_sql_documents (
  name TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_ms INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  content_id TEXT,
  encoding TEXT,
  encoded_bytes INTEGER,
  token_key TEXT,
  touched_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_sql_blob_retention ON runtime_sql_documents(touched_at, name) WHERE content_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS runtime_sql_references (
  root TEXT NOT NULL REFERENCES runtime_sql_documents(name) ON DELETE CASCADE,
  part TEXT NOT NULL REFERENCES runtime_sql_documents(name) ON DELETE RESTRICT,
  PRIMARY KEY(root, part)
);
CREATE INDEX IF NOT EXISTS runtime_sql_referenced_part ON runtime_sql_references(part);
