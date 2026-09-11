"""Small chain-specific durable state; never shares Solana's database or quotas."""
import json
import sqlite3
import time


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS logs (pool TEXT, tx TEXT, idx TEXT, block INTEGER, value TEXT NOT NULL, PRIMARY KEY(pool,tx,idx))")
        self.db.execute("CREATE TABLE IF NOT EXISTS windows (pool TEXT, block INTEGER, value TEXT NOT NULL, PRIMARY KEY(pool,block))")

    def get(self, key, max_age=None):
        row = self.db.execute("SELECT value,updated FROM cache WHERE key=?", (key,)).fetchone()
        if row and (max_age is None or time.time() - row[1] <= max_age):
            return json.loads(row[0])
        return None

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, separators=(",", ":")), time.time()))

    def add_logs(self, pool, logs):
        self.db.executemany("INSERT OR IGNORE INTO logs VALUES (?,?,?,?,?)", [
            (pool, x["transactionHash"], x["logIndex"], int(x["blockNumber"], 16), json.dumps(x)) for x in logs])

    def logs(self, pool, start, end):
        rows = [json.loads(x[0]) for x in self.db.execute(
            "SELECT value FROM logs WHERE pool=? AND block>=? AND block<=?", (pool, start, end))]
        return sorted(rows, key=lambda x: (int(x["blockNumber"], 16), int(x.get("transactionIndex", "0x0"), 16), int(x["logIndex"], 16)))

    def finish(self, success=True):
        if success:
            # Receipt cache is disposable; frozen cohorts and checkpoints are not.
            self.db.execute("DELETE FROM cache WHERE key LIKE 'receipt:%' AND updated<?", (time.time() - 7 * 86400,))
            self.db.execute("DELETE FROM cache WHERE (key LIKE 'relay:%' OR key LIKE 'block-time:%') AND updated<?", (time.time() - 7 * 86400,))
            self.db.commit()
        else:
            self.db.rollback()

    def close(self):
        self.db.close()

    def summarize(self, pool, row):
        summary = {k: row.get(k) for k in ("checked_at", "buy_swaps", "sell_swaps", "retained_supply_lower_bound_pct", "retained_supply_upper_bound_pct", "status", "history_complete")}
        self.db.execute("INSERT OR REPLACE INTO windows VALUES (?,?,?)", (pool, row["window_to_block"], json.dumps(summary)))
        # Keep logs needed by the last window; compact older observations into summaries.
        self.db.execute("DELETE FROM logs WHERE pool=? AND block<?", (pool, row["window_from_block"]))
