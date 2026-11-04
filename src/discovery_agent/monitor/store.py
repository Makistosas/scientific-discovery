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

    def opportunities(self, run_id: int, watch: str | None = None) -> dict[str, sqlite3.Row]:
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


class JsonStore:
    """Same interface as :class:`Store`, persisted as one small JSON file per watch.

    Designed for a Claude Managed Agents memory store mounted at ``/mnt/memory/<store>/``:
    memories are small text files (<= 100 KB each), so every collection is capped and
    each watch is written once per run (at :meth:`finish_run`), not on every change.
    """

    MAX_RUNS = 10
    MAX_OPPORTUNITY_RUNS = 3
    MAX_SEEN = 1500
    MAX_ALERTS = 100
    MAX_HYPOTHESES = 50

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._run_watch: dict[int, str] = {}

    # -- file handling --------------------------------------------------------
    def _path(self, watch: str) -> Path:
        return self.dir / f"{watch}.json"

    def _data(self, watch: str) -> dict:
        if watch not in self._cache:
            path = self._path(watch)
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            for key, empty in (("runs", []), ("opportunities", {}), ("seen", []), ("alerts", []),
                               ("hypotheses", [])):
                data.setdefault(key, empty)
            data["watch"] = watch
            self._cache[watch] = data
        return self._cache[watch]

    def flush(self, watch: str) -> None:
        data = self._data(watch)
        data["runs"] = data["runs"][-self.MAX_RUNS:]
        keep = {str(r["id"]) for r in data["runs"][-self.MAX_OPPORTUNITY_RUNS:]}
        data["opportunities"] = {k: v for k, v in data["opportunities"].items() if k in keep}
        data["seen"] = data["seen"][-self.MAX_SEEN:]
        data["alerts"] = data["alerts"][-self.MAX_ALERTS:]
        data["hypotheses"] = data["hypotheses"][-self.MAX_HYPOTHESES:]
        self._path(watch).write_text(json.dumps(data, ensure_ascii=False, indent=0, default=str), encoding="utf-8")

    # -- runs ---------------------------------------------------------------
    def start_run(self, watch: str, params: dict) -> int:
        with self._lock:
            data = self._data(watch)
            run_id = max((r["id"] for r in data["runs"]), default=0) + 1
            data["runs"].append({"id": run_id, "watch": watch, "started_at": now_iso(), "finished_at": None,
                                 "status": "running"})
            self._run_watch[run_id] = watch
            return run_id

    def finish_run(self, run_id: int, status: str = "ok") -> None:
        with self._lock:
            watch = self._run_watch[run_id]
            for r in self._data(watch)["runs"]:
                if r["id"] == run_id:
                    r["finished_at"], r["status"] = now_iso(), status
            self.flush(watch)

    def last_run(self, watch: str, status: str = "ok") -> dict | None:
        runs = [r for r in self._data(watch)["runs"] if r["status"] == status]
        return runs[-1] if runs else None

    def previous_ok_run(self, watch: str, before_id: int) -> dict | None:
        runs = [r for r in self._data(watch)["runs"] if r["status"] == "ok" and r["id"] < before_id]
        return runs[-1] if runs else None

    # -- opportunities --------------------------------------------------------
    def save_opportunities(self, run_id: int, watch: str, rows: list[dict]) -> None:
        with self._lock:
            slim = [{k: r.get(k) for k in ("key", "rank", "symbol", "disease", "opportunity", "evidence", "gap",
                                           "tractability", "safety")} for r in rows]
            self._data(watch)["opportunities"][str(run_id)] = slim

    def opportunities(self, run_id: int, watch: str | None = None) -> dict[str, dict]:
        watch = watch or self._run_watch.get(run_id)
        if watch is None:
            return {}
        rows = self._data(watch)["opportunities"].get(str(run_id), [])
        return {r["key"]: r for r in rows}

    # -- seen items -----------------------------------------------------------
    def mark_seen(self, watch: str, items: list[tuple[str, str]]) -> list[str]:
        with self._lock:
            data = self._data(watch)
            known = {s[0] for s in data["seen"]}
            new = []
            for item, kind in items:
                if item not in known:
                    known.add(item)
                    data["seen"].append([item, kind, now_iso()])
                    new.append(item)
            return new

    def seen_count(self, watch: str) -> int:
        return len(self._data(watch)["seen"])

    # -- hypotheses & alerts --------------------------------------------------
    def save_hypothesis(self, watch: str, key: str, hypothesis: dict) -> None:
        with self._lock:
            self._data(watch)["hypotheses"].append({
                "id": hypothesis.get("id"), "key": key, "created_at": now_iso(), "title": hypothesis.get("title"),
                "generated_by": hypothesis.get("generated_by"),
                "confidence": (hypothesis.get("confidence") or {}).get("level")})

    def add_alerts(self, watch: str, alerts: list[dict]) -> None:
        with self._lock:
            self._data(watch)["alerts"].extend(
                {"watch": watch, "created_at": now_iso(), "kind": a["kind"], "message": a["message"]} for a in alerts)

    def recent_alerts(self, watch: str | None = None, limit: int = 50) -> list[dict]:
        watches = [watch] if watch else sorted(p.stem for p in self.dir.glob("*.json"))
        rows = [a for w in watches for a in self._data(w)["alerts"]]
        # newest first; insertion order breaks ties between alerts created within the same second
        ordered = sorted(enumerate(rows), key=lambda t: (t[1]["created_at"], t[0]), reverse=True)
        return [a for _, a in ordered][:limit]

    def close(self) -> None:
        with self._lock:
            for watch in list(self._cache):
                self.flush(watch)


def open_store(path: Path | str) -> "Store | JsonStore":
    """``*.sqlite`` / ``*.db`` -> SQLite store; anything else is a directory of JSON files."""
    p = Path(path)
    if p.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
        return Store(p)
    return JsonStore(p)
