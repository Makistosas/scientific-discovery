"""SQLite memory of the continuous agent: runs, ranked opportunities, seen
publications/preprints/trials, hypotheses and alerts."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  params TEXT
);
CREATE TABLE IF NOT EXISTS opportunities (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  watch TEXT NOT NULL,
  key TEXT NOT NULL,
  rank INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  disease TEXT NOT NULL,
  opportunity REAL NOT NULL,
  evidence REAL, gap REAL, tractability REAL, safety REAL,
  payload TEXT,
  PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS seen_items (
  watch TEXT NOT NULL,
  item TEXT NOT NULL,
  kind TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (watch, item)
);
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT NOT NULL,
  watch TEXT NOT NULL,
  key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  generated_by TEXT,
  confidence TEXT,
  payload TEXT,
  PRIMARY KEY (id, created_at)
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch TEXT NOT NULL,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  payload TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- runs ---------------------------------------------------------------
    def start_run(self, watch: str, params: dict) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO runs (watch, started_at, params) VALUES (?, ?, ?)",
                                    (watch, now_iso(), json.dumps(params, default=str)))
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str = "ok") -> None:
        with self._lock:
            self.conn.execute("UPDATE runs SET finished_at = ?, status = ? WHERE id = ?", (now_iso(), status, run_id))
            self.conn.commit()

    def last_run(self, watch: str, status: str = "ok") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE watch = ? AND status = ? ORDER BY id DESC LIMIT 1", (watch, status)).fetchone()

    def previous_ok_run(self, watch: str, before_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE watch = ? AND status = 'ok' AND id < ? ORDER BY id DESC LIMIT 1",
            (watch, before_id)).fetchone()

    # -- opportunities --------------------------------------------------------
    def save_opportunities(self, run_id: int, watch: str, rows: list[dict]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO opportunities (run_id, watch, key, rank, symbol, disease, opportunity, "
                "evidence, gap, tractability, safety, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, watch, r["key"], r["rank"], r["symbol"], r["disease"], r["opportunity"], r.get("evidence"),
                  r.get("gap"), r.get("tractability"), r.get("safety"), json.dumps(r.get("payload"), default=str))
                 for r in rows])
            self.conn.commit()

    def opportunities(self, run_id: int) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM opportunities WHERE run_id = ? ORDER BY rank", (run_id,)).fetchall()
        return {r["key"]: r for r in rows}

    # -- seen items -----------------------------------------------------------
    def mark_seen(self, watch: str, items: list[tuple[str, str]]) -> list[str]:
        """Insert (item, kind) pairs; return the ones that were new."""
        new = []
        with self._lock:
            for item, kind in items:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO seen_items (watch, item, kind, first_seen) VALUES (?, ?, ?, ?)",
                    (watch, item, kind, now_iso()))
                if cur.rowcount:
                    new.append(item)
            self.conn.commit()
        return new

    def seen_count(self, watch: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM seen_items WHERE watch = ?", (watch,)).fetchone()[0])

    # -- hypotheses & alerts --------------------------------------------------
    def save_hypothesis(self, watch: str, key: str, hypothesis: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO hypotheses (id, watch, key, created_at, generated_by, confidence, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (hypothesis["id"], watch, key, now_iso(), hypothesis.get("generated_by"),
                 (hypothesis.get("confidence") or {}).get("level"), json.dumps(hypothesis, default=str)))
            self.conn.commit()

    def add_alerts(self, watch: str, alerts: list[dict]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT INTO alerts (watch, created_at, kind, message, payload) VALUES (?,?,?,?,?)",
                [(watch, now_iso(), a["kind"], a["message"], json.dumps(a.get("payload"), default=str)) for a in alerts])
            self.conn.commit()

    def recent_alerts(self, watch: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if watch:
            return self.conn.execute("SELECT * FROM alerts WHERE watch = ? ORDER BY id DESC LIMIT ?",
                                     (watch, limit)).fetchall()
        return self.conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def close(self) -> None:
        self.conn.close()
