"""Сверка изменённых определений с утверждёнными ответами, без записи оценок."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import json
import time

from . import judge, precedents
from .checklist import load_default
from .incremental import fingerprints

NO_APPROVED = "Утверждённых оценок по этому критерию пока нет — проверьте на 3 диалогах."


def approved_answers(db, active: dict) -> dict[str, dict[str, str]]:
    """Ответы по действующей версии; решения перекрывают только рассмотренный критерий.

    Подтверждение сохраняет предыдущие изменения по той же оценке. Решение остаётся
    утверждённым и после нового машинного прогона. Несовместимые определения старых
    версий не подмешиваются. Метки DEV привязаны к номерам и типам исходных критериев.
    """
    defaults = {c["id"]: c for c in load_default()["criteria"]}
    compatible = {c["id"] for c in active["criteria"]
                  if c["id"] in defaults and c["type"] == defaults[c["id"]]["type"]}
    reference = json.loads(precedents.REFERENCE.read_text(encoding="utf-8"))["dialogs"]
    labels = {cid: {key: value["answer"] for key, value in answers.items() if key in compatible}
              for cid, answers in reference.items()}
    hashes, configs, evaluations, decided = {}, {}, {}, {}
    appeals = db.q("SELECT a.*, e.conversation_id FROM appeals a JOIN evaluations e ON e.id=a.evaluation_id "
                   "WHERE a.status IN ('confirmed','changed','decided') AND e.status='ok' "
                   "ORDER BY a.decided_at, a.id")
    for a in appeals:
        eid, key = a["evaluation_id"], a["criterion_id"]
        if eid not in evaluations:
            evaluations[eid] = db.evaluation(eid)
        ev = evaluations[eid]
        model = ev["model"]
        if model not in hashes:
            hashes[model] = fingerprints(active, model)
        clid = ev["checklist_id"]
        if clid not in configs:
            source = db.checklist(clid)
            configs[clid] = source["config"] if source else {"criteria": []}
        if fingerprints(configs[clid], model).get(key) != hashes[model].get(key) or key not in hashes[model]:
            continue
        state_key = (eid, key)
        original = ev["answers"].get(key, {}).get("answer")
        answer = a["new_answer"] if a["new_answer"] is not None else decided.get(state_key, original)
        if answer is not None:
            decided[state_key] = answer
            labels.setdefault(a["conversation_id"], {})[key] = answer
    available = {c["id"] for c in db.list_conversations()}
    return {cid: answers for cid, answers in labels.items() if cid in available and answers}


def sample(labels: dict[str, dict[str, str]], criteria: list[str], limit: int) -> list[str]:
    """Общий лимит диалогов; по очереди заполняем наименее представленные ответы.

    При нескольких критериях ищем диалоги, покрывающие также редкие группы остальных
    критериев. Одну запись никогда не выбираем дважды; смысл диалога не анализируем.
    """
    buckets = defaultdict(list)
    for cid in sorted(labels):
        for key in criteria:
            if key in labels[cid]:
                buckets[key, labels[cid][key]].append(cid)
    selected, counts = [], Counter()
    while len(selected) < limit:
        progress = False
        for key in criteria:
            groups = [group for group, ids in buckets.items()
                      if group[0] == key and any(cid not in selected for cid in ids)]
            if not groups or len(selected) == limit:
                continue
            group = min(groups, key=lambda g: (counts[g], g[1]))
            candidates = [cid for cid in buckets[group] if cid not in selected]
            cid = min(candidates, key=lambda cid: (
                -sum(1 / (1 + counts[k, labels[cid][k]]) for k in criteria if k in labels[cid]), cid))
            selected.append(cid)
            for k in criteria:
                if k in labels[cid]:
                    counts[k, labels[cid][k]] += 1
            progress = True
        if not progress:
            break
    return selected


async def check_approved(db, config: dict, provider, criteria=None, limit: int = 12) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Лимит диалогов — целое число от 1 до 100.")
    model = judge.model_name(provider.pid)
    active = db.checklist(db.list_checklists()[-1]["id"])
    old, draft = fingerprints(active["config"], model), fingerprints(config, model)
    if criteria is not None and (not criteria or set(criteria) - draft.keys()):
        raise ValueError("Укажите непустой список критериев из чек-листа.")
    changed = [key for key in draft if draft[key] != old.get(key) and (criteria is None or key in criteria)]
    labels = approved_answers(db, active["config"]) if changed else {}
    ids = sample(labels, changed, limit)
    rows = [{"id": c["id"], "title": c["title"], "matched": 0, "total": 0, "sampled": 0,
             "available": sum(c["id"] in answers for answers in labels.values()), "disagreements": [], "errors": []}
            for c in config["criteria"] if c["id"] in changed]
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        if not row["available"]:
            row["message"] = NO_APPROVED
    if ids and not judge.available(provider.pid):
        raise ValueError("Модель недоступна: проверьте настройки провайдера.")

    async def one(cid):
        only = [key for key in changed if key in labels[cid]]
        call_id = db.start_criterion_test_call(cid, active["id"], only, provider.pid, model)
        started = time.monotonic()
        result = {"status": "error", "error": "Проверка прервана.", "cost_usd": 0}
        try:
            # Один вызов со строгой схемой, без голосования, повторов и эталона в промпте.
            result = await judge.evaluate(db.record(cid), config, provider, only=only)
        except Exception as e:  # сбой провайдера отражаем в результате и журнале
            result = {"status": "error", "error": f"{type(e).__name__}: {e}", "cost_usd": 0}
        finally:
            result.setdefault("latency_s", round(time.monotonic() - started, 2))
            db.finish_criterion_test_call(call_id, result)
        return cid, only, result

    started = time.monotonic()
    results = await asyncio.gather(*(one(cid) for cid in ids))
    for cid, only, result in results:
        for key in only:
            row = by_id[key]
            row["sampled"] += 1
            if result["status"] != "ok":
                row["errors"].append({"conversation_id": cid, "error": result["error"]})
                continue
            answer = result["answers"][key]
            row["total"] += 1
            if answer["answer"] == labels[cid][key]:
                row["matched"] += 1
            else:
                row["disagreements"].append({"conversation_id": cid, "approved_answer": labels[cid][key],
                                             **{k: answer[k] for k in ("answer", "explanation", "evidence")}})
    return {"provider": provider.pid, "model": model, "checklist_id": active["id"], "criteria": rows,
            "conversation_ids": ids, "model_calls": len(ids),
            "message": "Определения критериев не изменились." if not changed else None,
            "cost_usd": round(sum(result.get("cost_usd") or 0 for _, _, result in results), 6),
            "latency_s": round(time.monotonic() - started, 2)}
