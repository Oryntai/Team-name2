"""Отпечатки определений и общий расчёт переноса для плана и прогона."""
from __future__ import annotations

import hashlib
import json

from .judge import prompt


def fingerprints(config: dict, model: str) -> dict[str, str]:
    # «Нет» тоже входит в промпт. Вес и секция нужны только для арифметики и показа.
    fields = ("id", "title", "type", "yes", "no", "na", "na_when", "steps", "guidance")
    out = {}
    for c in config["criteria"]:
        definition = {k: c.get(k) for k in fields}
        definition.update(rules=config.get("rules") or [], prompt_version=prompt.PROMPT_VERSION, model=model)
        raw = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        out[c["id"]] = hashlib.sha256(raw.encode()).hexdigest()
    return out


def partition(config: dict, model: str, previous: dict | None, mode: str) -> tuple[list[str], dict]:
    hashes = fingerprints(config, model)
    reused = {}
    if previous and mode == "incremental":
        for cid, h in hashes.items():
            answer = previous["answers"].get(cid)
            if answer and answer.get("fingerprint") == h:
                reused[cid] = {**answer, "reused_from": previous["id"]}
    return [cid for cid in hashes if cid not in reused], reused


def plan(db, ids: list[str], checklist_id: int, model: str, mode: str) -> dict:
    config = db.checklist(checklist_id)["config"]
    hashes = fingerprints(config, model)
    rows = {c["id"]: {"id": c["id"], "title": c["title"], "evaluated": 0, "reused": 0,
                       "definition_changed": 0, "weight_changed": 0} for c in config["criteria"]}
    fresh = calls = 0
    for cid in ids:
        previous = db.latest_success(cid, model)
        only, reused = partition(config, model, previous, mode)
        fresh += previous is None
        calls += bool(only)
        old = db.checklist(previous["checklist_id"])["config"] if previous else {"criteria": []}
        old_criteria = {c["id"]: c for c in old["criteria"]}
        for c in config["criteria"]:
            row = rows[c["id"]]
            row["evaluated"] += c["id"] in only
            row["reused"] += c["id"] in reused
            if previous:
                answer = previous["answers"].get(c["id"], {})
                changed = answer.get("fingerprint") != hashes[c["id"]]
                row["definition_changed"] += changed
                row["weight_changed"] += not changed and old_criteria.get(c["id"], {}).get("weight") != c.get("weight")
    criteria = list(rows.values())
    return {"mode": mode, "total": len(ids), "without_previous": fresh, "model_calls": calls,
            "evaluated": sum(r["evaluated"] for r in criteria), "reused": sum(r["reused"] for r in criteria),
            "criteria": criteria}
