"""Хранение: SQLite (stdlib), один файл `var/cqa.db` (путь — переменная CQA_DB).

Схема — в docs/ARCHITECTURE.md (+ служебная таблица run_items: состав и состояние прогона).
Версии чек-листов неизменяемы: правка = новая запись, version = max(version) + 1.
Оценки модели не переписываются: решения проверяющего хранятся в appeals и накладываются при чтении.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import threading

from . import audio

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "var" / "cqa.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, source TEXT, fetched_at TEXT, total INTEGER);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, dataset_id TEXT, channel TEXT, language TEXT, transcript TEXT NOT NULL, audio_url TEXT,
  imported_at TEXT, pos INTEGER, n_replies INTEGER, duration TEXT);
CREATE TABLE IF NOT EXISTS checklists(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version INTEGER, parent_id INTEGER, note TEXT,
  config TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, finished_at TEXT, status TEXT, checklist_id INTEGER,
  provider TEXT, model TEXT, total INTEGER, done INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS run_items(
  run_id INTEGER, pos INTEGER, conversation_id TEXT, status TEXT, evaluation_id INTEGER, error TEXT,
  PRIMARY KEY(run_id, conversation_id));
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, conversation_id TEXT, checklist_id INTEGER, created_at TEXT,
  provider TEXT, model TEXT, prompt_version TEXT, status TEXT, error TEXT, latency_s REAL, input_tokens INTEGER,
  output_tokens INTEGER, cost_usd REAL, answers TEXT, pii_masked TEXT, raw TEXT, total REAL, gross INTEGER);
CREATE INDEX IF NOT EXISTS evaluations_conv ON evaluations(conversation_id);
CREATE INDEX IF NOT EXISTS evaluations_run ON evaluations(run_id);
CREATE TABLE IF NOT EXISTS appeals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id INTEGER, criterion_id TEXT, comment TEXT, created_at TEXT,
  status TEXT, new_answer TEXT, reason TEXT, decided_at TEXT);
CREATE INDEX IF NOT EXISTS appeals_eval ON appeals(evaluation_id);
CREATE TABLE IF NOT EXISTS fairness_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  phase TEXT NOT NULL, variant TEXT NOT NULL, attempt INTEGER NOT NULL, created_at TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT, status TEXT NOT NULL, error TEXT, latency_s REAL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd REAL, request TEXT NOT NULL, response TEXT);
CREATE INDEX IF NOT EXISTS fairness_calls_experiment ON fairness_calls(experiment_id);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def _json(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class DB:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = pathlib.Path(path or os.environ.get("CQA_DB") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            additions = {
                "runs": {"mode": "TEXT DEFAULT 'full'"},
                "run_items": {"previous_evaluation_id": "INTEGER"},
                "evaluations": {"mode": "TEXT DEFAULT 'full'", "evaluated": "TEXT DEFAULT '[]'",
                                "reused": "TEXT DEFAULT '[]'", "model_calls": "INTEGER DEFAULT 0",
                                "precedents": "TEXT DEFAULT '[]'", "thinking": "TEXT DEFAULT '[]'"},
            }
            for table, columns in additions.items():
                existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # --- низкий уровень ---
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def ex(self, sql: str, args=()) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, args).lastrowid

    # --- настройки ---
    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key: str, value) -> None:
        if value is None:
            self.ex("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))

    # --- наборы и диалоги ---
    def save_dataset(self, dataset_id: str, source: str, records: list[dict], total: int | None = None) -> int:
        ts = now()
        with self.lock, self.conn:
            c = self.conn
            c.execute("INSERT INTO datasets(id, source, fetched_at, total) VALUES(?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET source=excluded.source, fetched_at=excluded.fetched_at, "
                      "total=excluded.total", (dataset_id, source, ts, total if total is not None else len(records)))
            for pos, r in enumerate(records):
                tr = r["transcript"]
                c.execute("INSERT INTO conversations(id, dataset_id, channel, language, transcript, audio_url, imported_at, "
                          "pos, n_replies, duration) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                          "dataset_id=excluded.dataset_id, channel=excluded.channel, language=excluded.language, "
                          "transcript=excluded.transcript, audio_url=excluded.audio_url, imported_at=excluded.imported_at, "
                          "pos=excluded.pos, n_replies=excluded.n_replies, duration=excluded.duration",
                          (r["conversation_id"], dataset_id, r.get("channel"), r.get("language"),
                           json.dumps(tr, ensure_ascii=False), r.get("audio_url"), ts, pos, len(tr), audio.duration(tr)))
        return len(records)

    def list_datasets(self) -> list[dict]:
        return self.q("SELECT d.id, d.source, d.fetched_at, d.total, "
                      "(SELECT COUNT(*) FROM conversations c WHERE c.dataset_id=d.id) AS n_conversations "
                      "FROM datasets d ORDER BY d.fetched_at DESC, d.id")

    def count_conversations(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM conversations")["n"]

    def conversation(self, cid: str) -> dict | None:
        r = self.one("SELECT * FROM conversations WHERE id=?", (cid,))
        if r:
            r["transcript"] = _json(r["transcript"], [])
        return r

    def record(self, cid: str) -> dict | None:
        """Диалог в исходном формате API организаторов (как его видит судья)."""
        r = self.conversation(cid)
        if not r:
            return None
        return {"conversation_id": r["id"], "channel": r["channel"], "language": r["language"],
                "transcript": r["transcript"], "audio_url": r["audio_url"]}

    def list_conversations(self, dataset_id=None, language=None, channel=None) -> list[dict]:
        sql = ("SELECT c.id, c.dataset_id, c.channel, c.language, c.n_replies, c.duration, c.audio_url "
               "FROM conversations c LEFT JOIN datasets d ON d.id=c.dataset_id WHERE 1=1")
        args: list = []
        if dataset_id:
            sql += " AND c.dataset_id=?"
            args.append(dataset_id)
        if language:
            sql += " AND lower(c.language)=lower(?)"
            args.append(language)
        if channel:
            sql += " AND lower(c.channel)=lower(?)"
            args.append(channel)
        sql += " ORDER BY d.fetched_at DESC, c.dataset_id, c.pos, c.id"
        return self.q(sql, args)

    def conversation_ids(self, dataset_id: str) -> list[str]:
        return [r["id"] for r in self.q("SELECT id FROM conversations WHERE dataset_id=? ORDER BY pos, id", (dataset_id,))]

    # --- чек-листы ---
    def list_checklists(self) -> list[dict]:
        rows = self.q("SELECT id, name, version, parent_id, note, created_at, config FROM checklists ORDER BY id")
        for r in rows:
            r["n_criteria"] = len((_json(r.pop("config"), {}) or {}).get("criteria") or [])
        return rows

    def checklist(self, cl_id: int) -> dict | None:
        r = self.one("SELECT * FROM checklists WHERE id=?", (cl_id,))
        if r:
            r["config"] = _json(r["config"], {})
        return r

    def create_checklist(self, config: dict, note: str = "", parent_id: int | None = None) -> tuple[int, int]:
        with self.lock, self.conn:
            v = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM checklists").fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO checklists(name, version, parent_id, note, config, created_at) VALUES(?,?,?,?,?,?)",
                (config.get("name") or "Чек-лист", v, parent_id, note or "", json.dumps(config, ensure_ascii=False), now()))
            return cur.lastrowid, v

    def count_checklists(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM checklists")["n"]

    def seed_default_checklist(self) -> int | None:
        """Пустая база → начальный чек-лист из PDF организаторов (version 1)."""
        if self.count_checklists():
            return None
        from .checklist import load_default
        cl_id, _ = self.create_checklist(load_default(), note="Начальная конфигурация (PDF организаторов)")
        return cl_id

    # --- прогоны ---
    def create_run(self, checklist_id: int, provider: str, model: str, conversation_ids: list[str],
                   mode: str = "full") -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(created_at, status, checklist_id, provider, model, total, done, failed, cost_usd, mode) "
                "VALUES(?,?,?,?,?,?,0,0,0,?)", (now(), "running", checklist_id, provider, model, len(conversation_ids), mode))
            run_id = cur.lastrowid
            self.conn.executemany("INSERT INTO run_items(run_id, pos, conversation_id, status, previous_evaluation_id) "
                                  "VALUES(?,?,?,?,?)",
                                  [(run_id, i, cid, "pending", (self.latest_success(cid, model) or {}).get("id"))
                                   for i, cid in enumerate(conversation_ids)])
            return run_id

    def run(self, run_id: int) -> dict | None:
        return self.one("SELECT * FROM runs WHERE id=?", (run_id,))

    def run_items(self, run_id: int) -> list[dict]:
        return self.q("SELECT i.conversation_id, i.status, i.evaluation_id, i.previous_evaluation_id, i.error, e.total, e.gross "
                      "FROM run_items i LEFT JOIN evaluations e ON e.id=i.evaluation_id WHERE i.run_id=? ORDER BY i.pos",
                      (run_id,))

    def list_runs(self) -> list[dict]:
        return self.q("SELECT r.*, COUNT(e.id) AS n_evals, AVG(e.cost_usd) AS avg_cost_per_eval, "
                      "AVG(e.latency_s) AS avg_latency_s, COALESCE(SUM(e.input_tokens), 0) AS tokens_in, "
                      "COALESCE(SUM(e.output_tokens), 0) AS tokens_out FROM runs r "
                      "LEFT JOIN evaluations e ON e.run_id=r.id AND e.status='ok' GROUP BY r.id ORDER BY r.id DESC")

    def run_stats(self, run_id: int) -> dict:
        return self.one("SELECT COUNT(id) AS n_evals, AVG(cost_usd) AS avg_cost_per_eval, AVG(latency_s) AS avg_latency_s, "
                        "COALESCE(SUM(input_tokens), 0) AS tokens_in, COALESCE(SUM(output_tokens), 0) AS tokens_out "
                        "FROM evaluations WHERE run_id=? AND status='ok'", (run_id,))

    def run_counts(self, run_id: int) -> dict:
        rows = self.q("SELECT model_calls, evaluated, reused FROM evaluations WHERE run_id=?", (run_id,))
        return {"model_calls": sum(r["model_calls"] or 0 for r in rows),
                "evaluated": sum(len(_json(r["evaluated"], [])) for r in rows),
                "reused": sum(len(_json(r["reused"], [])) for r in rows)}

    def set_item(self, run_id: int, cid: str, status: str, evaluation_id: int | None = None, error: str | None = None):
        self.ex("UPDATE run_items SET status=?, evaluation_id=COALESCE(?, evaluation_id), error=? "
                "WHERE run_id=? AND conversation_id=?", (status, evaluation_id, error, run_id, cid))

    def bump_run(self, run_id: int, ok: bool, cost: float) -> None:
        self.ex("UPDATE runs SET done=done+?, failed=failed+?, cost_usd=cost_usd+? WHERE id=?",
                (1 if ok else 0, 0 if ok else 1, cost or 0.0, run_id))

    def finish_run(self, run_id: int, status: str = "done") -> None:
        self.ex("UPDATE runs SET status=?, finished_at=? WHERE id=?", (status, now(), run_id))

    def fail_interrupted_runs(self) -> int:
        """Прогоны, прерванные перезапуском сервера: помечаем ошибкой (оценки, что успели, остаются)."""
        with self.lock, self.conn:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM runs WHERE status IN ('running', 'pending')")]
            for rid in ids:
                n = self.conn.execute("UPDATE run_items SET status='error', error='прервано перезапуском сервера' "
                                      "WHERE run_id=? AND status IN ('pending', 'running')", (rid,)).rowcount
                self.conn.execute("UPDATE runs SET status='error', failed=failed+?, finished_at=? WHERE id=?",
                                  (n, now(), rid))
            return len(ids)

    # --- оценки ---
    def insert_evaluation(self, ev: dict) -> int:
        return self.ex(
            "INSERT INTO evaluations(run_id, conversation_id, checklist_id, created_at, provider, model, prompt_version, "
            "status, error, latency_s, input_tokens, output_tokens, cost_usd, answers, pii_masked, raw, total, gross, "
            "mode, evaluated, reused, model_calls, precedents, thinking) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.get("run_id"), ev["conversation_id"], ev["checklist_id"], ev.get("created_at") or now(), ev.get("provider"),
             ev.get("model"), ev.get("prompt_version"), ev.get("status"), ev.get("error"), ev.get("latency_s"),
             ev.get("input_tokens"), ev.get("output_tokens"), ev.get("cost_usd"),
             json.dumps(ev.get("answers") or {}, ensure_ascii=False),
             json.dumps(ev.get("pii_masked") or [], ensure_ascii=False), ev.get("raw"), ev.get("total"),
             None if ev.get("gross") is None else int(bool(ev.get("gross"))), ev.get("mode", "full"),
             json.dumps(ev.get("evaluated", [])), json.dumps(ev.get("reused", [])), ev.get("model_calls", 0),
             json.dumps(ev.get("precedents") or [], ensure_ascii=False),
             json.dumps(ev.get("thinking") or [], ensure_ascii=False)))

    def evaluation(self, ev_id: int) -> dict | None:
        r = self.one("SELECT * FROM evaluations WHERE id=?", (ev_id,))
        if r:
            r["answers"] = _json(r["answers"], {})
            r["pii_masked"] = _json(r["pii_masked"], [])
            r["evaluated"] = _json(r["evaluated"], [])
            r["reused"] = _json(r["reused"], [])
            r["precedents"] = _json(r["precedents"], [])
            r["thinking"] = _json(r.get("thinking"), [])
        return r

    def latest_success(self, cid: str, model: str) -> dict | None:
        row = self.one("SELECT id FROM evaluations WHERE conversation_id=? AND model=? AND status='ok' "
                       "ORDER BY id DESC LIMIT 1", (cid, model))
        return self.evaluation(row["id"]) if row else None

    def evaluations_for(self, cid: str) -> list[dict]:
        rows = self.q("SELECT * FROM evaluations WHERE conversation_id=? ORDER BY id DESC", (cid,))
        for r in rows:
            r["answers"] = _json(r["answers"], {})
            r["pii_masked"] = _json(r["pii_masked"], [])
            r["precedents"] = _json(r["precedents"], [])
            r["thinking"] = _json(r.get("thinking"), [])
        return rows

    def latest_evaluations(self) -> dict[str, dict]:
        rows = self.q("SELECT e.* FROM evaluations e JOIN (SELECT conversation_id, MAX(id) AS id FROM evaluations "
                      "GROUP BY conversation_id) m ON m.id=e.id")
        out = {}
        for r in rows:
            r["answers"] = _json(r["answers"], {})
            out[r["conversation_id"]] = r
        return out

    # --- апелляции ---
    def create_appeal(self, evaluation_id: int, criterion_id: str, comment: str) -> int:
        return self.ex("INSERT INTO appeals(evaluation_id, criterion_id, comment, created_at, status) VALUES(?,?,?,?, 'open')",
                       (evaluation_id, criterion_id, comment, now()))

    def appeal(self, appeal_id: int) -> dict | None:
        return self.one("SELECT * FROM appeals WHERE id=?", (appeal_id,))

    def appeals_for(self, evaluation_id: int) -> list[dict]:
        return self.q("SELECT * FROM appeals WHERE evaluation_id=? ORDER BY id", (evaluation_id,))

    def open_appeal(self, evaluation_id: int, criterion_id: str) -> dict | None:
        return self.one("SELECT * FROM appeals WHERE evaluation_id=? AND criterion_id=? AND status='open'",
                        (evaluation_id, criterion_id))

    def list_appeals(self, status: str = "all") -> list[dict]:
        sql = ("SELECT a.*, e.conversation_id FROM appeals a JOIN evaluations e ON e.id=a.evaluation_id")
        if status == "open":
            sql += " WHERE a.status='open'"
        elif status == "decided":
            sql += " WHERE a.status IN ('confirmed', 'changed')"
        sql += " ORDER BY CASE a.status WHEN 'open' THEN 0 ELSE 1 END, a.id DESC"
        return self.q(sql)

    def decide_appeal(self, appeal_id: int, status: str, new_answer: str | None, reason: str) -> None:
        self.ex("UPDATE appeals SET status=?, new_answer=?, reason=?, decided_at=? WHERE id=?",
                (status, new_answer, reason, now(), appeal_id))

    def appeal_counts(self) -> dict[str, dict]:
        rows = self.q("SELECT e.conversation_id AS cid, SUM(a.status='open') AS open, "
                      "SUM(a.status IN ('confirmed', 'changed')) AS decided FROM appeals a "
                      "JOIN evaluations e ON e.id=a.evaluation_id GROUP BY e.conversation_id")
        return {r["cid"]: {"open": int(r["open"] or 0), "decided": int(r["decided"] or 0)} for r in rows}
