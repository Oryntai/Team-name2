"""Проба черновика на трёх диалогах без сохранения чек-листа, прогона и оценок."""
from __future__ import annotations

import asyncio
import time

from . import judge
from .incremental import partition


async def preview(db, config: dict, provider, conversation_ids=None, criteria=None) -> dict:
    model = judge.model_name(provider.pid)
    all_ids = [c["id"] for c in config["criteria"]]
    if criteria is not None and (not criteria or set(criteria) - set(all_ids)):
        raise ValueError("Укажите непустой список критериев из чек-листа.")
    if conversation_ids is not None:
        ids = list(dict.fromkeys(cid.strip() for cid in conversation_ids if cid.strip()))
        if not ids:
            raise ValueError("Выберите диалоги для проверки.")
        if any(db.conversation(cid) is None for cid in ids):
            raise ValueError("Один из выбранных диалогов не найден.")
        selected = [(cid, db.latest_success(cid, model)) for cid in ids[:3]]
    else:
        candidates = [(c, db.latest_success(c["id"], model)) for c in db.list_conversations()]
        selected, languages = [], set()
        # Сначала прошлые оценки той же моделью; внутри каждой группы — разные языки.
        for successful in (True, False):
            group = [(c, prev) for c, prev in candidates if bool(prev) == successful]
            for diverse in (True, False):
                for c, prev in group:
                    if len(selected) == 3:
                        break
                    if c["id"] in [cid for cid, _ in selected]:
                        continue
                    language = (c["language"] or "").lower()
                    if diverse and language in languages:
                        continue
                    selected.append((c["id"], prev))
                    languages.add(language)
    if not selected:
        raise ValueError("Нет диалогов для проверки: сначала загрузите набор.")

    async def one(cid, previous):
        record = db.record(cid)
        changed, _ = partition(config, model, previous, "incremental")
        only = criteria if criteria is not None else changed or all_ids
        # Полный вызов: критерий оценивается в контексте всего чек-листа (отдельный вызов хуже — замер 7.11);
        # показываем только проверяемые критерии. Один повтор при сбое — проверка интерактивная.
        for attempt in range(2):
            try:
                result = await judge.evaluate(record, config, provider)
            except Exception as e:  # noqa: BLE001 — сбой одного диалога не роняет всю проверку
                result = {"status": "error", "error": f"{type(e).__name__}: {e}", "answers": {}, "cost_usd": 0.0}
            if result["status"] == "ok" or attempt:
                break
            await asyncio.sleep(3)
        old = previous["answers"] if previous else {}
        item = {"id": cid, "language": record["language"], "channel": record["channel"],
                "error": None if result["status"] == "ok" else result["error"],
                "criteria": [{"id": c["id"], "title": c["title"],
                              **{k: answer[k] for k in ("answer", "explanation", "evidence")},
                              "previous_answer": old.get(c["id"], {}).get("answer")}
                             for c in config["criteria"]
                             if c["id"] in only and (answer := result["answers"].get(c["id"]))]}
        return item, result

    started = time.monotonic()
    results = await asyncio.gather(*(one(cid, prev) for cid, prev in selected))
    if all(res["status"] != "ok" for _, res in results):  # упали все — это ошибка проверки, иначе показываем что есть
        raise judge.ProviderError("; ".join(f"{item['id']}: {res['error']}" for item, res in results))
    return {"provider": provider.pid, "model": model, "items": [item for item, _ in results],
            "cost_usd": round(sum(res.get("cost_usd") or 0 for _, res in results), 6),
            "latency_s": round(time.monotonic() - started, 2)}
