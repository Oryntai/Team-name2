"""Пакетные прогоны: оценка выбранных диалогов в фоне (asyncio внутри процесса сервера).

В incremental переносим ответы только при совпадении отпечатков определений и модели.
Ошибки повторяем с паузами RETRY_PAUSES, затем сохраняем статус error и текст ошибки.
Стоимость и токены оценки — сумма всех её вызовов (так требует ТЗ: «стоимость одной оценки со всеми вызовами»).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import judge, precedents
from .incremental import fingerprints, partition
from .scoring import ScoreError, score
from .vote import evaluate_voted

log = logging.getLogger("cqa.runner")
CONCURRENCY = 24  # вызовов модели одновременно: при 3 голосах — 8 диалогов; 429 ловили только при ~40
RETRY_PAUSES = (5, 15, 30)  # секунды между повторными попытками оценки одного диалога


class Runner:
    def __init__(self, db, concurrency: int = CONCURRENCY, votes: int | None = None):
        self.db = db
        self.concurrency = concurrency
        self.votes = int(os.environ.get("CQA_VOTES", "3")) if votes is None else votes
        self._validate_votes(self.votes)
        self.tasks: set[asyncio.Task] = set()

    @staticmethod
    def _validate_votes(votes):
        if type(votes) is not int or votes < 1:
            raise ValueError("Число голосов должно быть положительным целым.")

    def start(self, conversation_ids: list[str], checklist_id: int, provider_id: str, mode: str = "full",
              *, votes: int | None = None) -> int:
        """Создаёт прогон и запускает его в фоне; вызывать из работающего цикла событий."""
        cl = self.db.checklist(checklist_id)
        if not cl:
            raise ValueError("Чек-лист не найден.")
        if mode not in ("full", "incremental"):
            raise ValueError("Режим — full или incremental.")
        votes = self.votes if votes is None else votes
        self._validate_votes(votes)
        provider = judge.get_provider(provider_id)
        run_id = self.db.create_run(checklist_id, provider_id, judge.model_name(provider_id), conversation_ids, mode)
        previous = {i["conversation_id"]: self.db.evaluation(i["previous_evaluation_id"])
                    if i["previous_evaluation_id"] else None for i in self.db.run_items(run_id)}
        task = asyncio.get_running_loop().create_task(
            self._run(run_id, conversation_ids, cl["config"], checklist_id, provider, mode, previous, votes))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run_id

    async def wait(self) -> None:
        """Дождаться всех фоновых прогонов (для тестов и остановки сервера)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _one(self, run_id: int, cid: str, config: dict, checklist_id: int, provider, sem: asyncio.Semaphore,
                   mode: str = "full", previous: dict | None = None, votes: int | None = None):
        # слот семафора берём только на сам вызов модели, чтобы паузы между попытками не держали очередь
        rec = self.db.record(cid)
        if rec is None:
            self.db.set_item(run_id, cid, "error", error="Диалог не найден в базе.")
            self.db.bump_run(run_id, False, 0.0)
            return
        self.db.set_item(run_id, cid, "running")
        tokens_in = tokens_out = 0
        model = judge.model_name(provider.pid)
        only, reused = partition(config, model, previous, mode)
        examples = precedents.select(self.db, rec, config) if only else []
        calls = 0
        votes = self.votes if votes is None else votes
        raw_calls = []
        cost = latency = 0.0
        res = {"status": "ok", "error": None, "answers": {}, "model": model,
               "prompt_version": judge.PROMPT_VERSION, "pii_masked": (previous or {}).get("pii_masked", [])}
        for attempt in range(len(RETRY_PAUSES) + 1 if only else 0):
            if attempt:  # лимит запросов или сбой ответа: пережидаем и пробуем снова
                await asyncio.sleep(RETRY_PAUSES[attempt - 1])
            if votes == 1:
                async with sem:
                    try:
                        calls += 1
                        # Всегда полный вызов: критерий оценивается в контексте всего чек-листа — отдельный вызов
                        # по критерию на DEV хуже (замер 7.11: 92,5% против 94,2%). Из ответа берём только `only`.
                        res = await judge.evaluate(rec, config, provider, precedents=examples)
                    except Exception as e:  # noqa: BLE001 — любую ошибку сохраняем в оценку, прогон идёт дальше
                        log.exception("evaluate %s", cid)
                        res = {"status": "error", "error": f"{type(e).__name__}: {e}", "answers": {},
                               "provider": provider.pid, "model": judge.model_name(provider.pid)}
            else:
                # only фильтрует сохранение ниже; все голоса оценивают полный чек-лист.
                res = await evaluate_voted(rec, config, provider, votes, sem=sem, precedents=examples)
                calls += res["model_calls"]
                raw_calls.extend(json.loads(res["raw"]))
            tokens_in += res.get("input_tokens") or 0
            tokens_out += res.get("output_tokens") or 0
            cost += res.get("cost_usd") or 0.0
            latency += res.get("latency_s") or 0.0
            if res.get("status") == "ok":
                break
        total = gross = None
        if reused and res.get("model") != model:
            res = {**res, "status": "error", "error": "Провайдер вернул другую модель; запустите полную оценку."}
        hashes = fingerprints(config, res.get("model") or model)
        answers = {k: {**v, "fingerprint": hashes[k], "reused_from": None}
                   for k, v in (res.get("answers") or {}).items() if k in only}
        answers.update(reused)
        status, error = res.get("status", "error"), res.get("error")
        if status == "ok":
            try:
                s = score(config, {k: v["answer"] for k, v in answers.items()})
                total, gross = s["total"], s["gross"]
            except ScoreError as e:
                status, error = "error", f"Итог не посчитан: {e}"
        ev_id = self.db.insert_evaluation({
            "run_id": run_id, "conversation_id": cid, "checklist_id": checklist_id,
            "provider": res.get("provider") or provider.pid, "model": res.get("model") or model,
            "mode": mode, "evaluated": only, "reused": list(reused), "model_calls": calls,
            "precedents": [{"conversation_id": p["conversation_id"], "source": p["source"]} for p in examples],
            "prompt_version": res.get("prompt_version"), "status": status, "error": error,
            "latency_s": round(latency, 2), "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(cost, 6), "answers": answers, "pii_masked": res.get("pii_masked") or [],
            "raw": json.dumps(raw_calls, ensure_ascii=False) if votes > 1 and calls else res.get("raw"),
            "total": total, "gross": gross})
        self.db.set_item(run_id, cid, "ok" if status == "ok" else "error", ev_id, None if status == "ok" else error)
        self.db.bump_run(run_id, status == "ok", cost)

    async def _run(self, run_id: int, ids: list[str], config: dict, checklist_id: int, provider,
                   mode: str, previous: dict, votes: int | None = None) -> None:
        sem = asyncio.Semaphore(self.concurrency)
        try:
            await asyncio.gather(*(self._one(run_id, cid, config, checklist_id, provider, sem, mode, previous[cid], votes)
                                   for cid in ids))
            r = self.db.run(run_id)
            self.db.finish_run(run_id, "done" if (r and r["done"]) or not ids else "error")
        except Exception:  # noqa: BLE001
            log.exception("run %s", run_id)
            self.db.finish_run(run_id, "error")
