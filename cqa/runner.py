"""Пакетные прогоны: оценка выбранных диалогов в фоне (asyncio внутри процесса сервера).

Каждый диалог — живой вызов модели (`judge.evaluate`), ответы кешем по id диалога не подменяются.
Ошибка — одна повторная попытка, затем оценка сохраняется со статусом error и текстом ошибки.
Стоимость и токены оценки — сумма всех её вызовов (так требует ТЗ: «стоимость одной оценки со всеми вызовами»).
"""
from __future__ import annotations

import asyncio
import logging

from . import judge
from .scoring import ScoreError, score

log = logging.getLogger("cqa.runner")
CONCURRENCY = 10
RETRY_PAUSES = (5, 15, 30)  # секунды между повторными попытками оценки одного диалога


class Runner:
    def __init__(self, db, concurrency: int = CONCURRENCY):
        self.db = db
        self.concurrency = concurrency
        self.tasks: set[asyncio.Task] = set()

    def start(self, conversation_ids: list[str], checklist_id: int, provider_id: str) -> int:
        """Создаёт прогон и запускает его в фоне; вызывать из работающего цикла событий."""
        cl = self.db.checklist(checklist_id)
        if not cl:
            raise ValueError("Чек-лист не найден.")
        provider = judge.get_provider(provider_id)
        run_id = self.db.create_run(checklist_id, provider_id, judge.model_name(provider_id), conversation_ids)
        task = asyncio.get_running_loop().create_task(
            self._run(run_id, conversation_ids, cl["config"], checklist_id, provider))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run_id

    async def wait(self) -> None:
        """Дождаться всех фоновых прогонов (для тестов и остановки сервера)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _one(self, run_id: int, cid: str, config: dict, checklist_id: int, provider, sem: asyncio.Semaphore):
        # слот семафора берём только на сам вызов модели, чтобы паузы между попытками не держали очередь
        rec = self.db.record(cid)
        if rec is None:
            self.db.set_item(run_id, cid, "error", error="Диалог не найден в базе.")
            self.db.bump_run(run_id, False, 0.0)
            return
        self.db.set_item(run_id, cid, "running")
        tokens_in = tokens_out = 0
        cost = latency = 0.0
        res: dict = {}
        for attempt in range(len(RETRY_PAUSES) + 1):
            if attempt:  # лимит запросов или сбой ответа: пережидаем и пробуем снова
                await asyncio.sleep(RETRY_PAUSES[attempt - 1])
            async with sem:
                try:
                    res = await judge.evaluate(rec, config, provider)
                except Exception as e:  # noqa: BLE001 — любую ошибку сохраняем в оценку, прогон идёт дальше
                    log.exception("evaluate %s", cid)
                    res = {"status": "error", "error": f"{type(e).__name__}: {e}", "answers": {},
                           "provider": provider.pid, "model": judge.model_name(provider.pid)}
            tokens_in += res.get("input_tokens") or 0
            tokens_out += res.get("output_tokens") or 0
            cost += res.get("cost_usd") or 0.0
            latency += res.get("latency_s") or 0.0
            if res.get("status") == "ok":
                break
        total = gross = None
        status, error = res.get("status", "error"), res.get("error")
        if status == "ok":
            try:
                s = score(config, {k: v["answer"] for k, v in res["answers"].items()})
                total, gross = s["total"], s["gross"]
            except ScoreError as e:
                status, error = "error", f"Итог не посчитан: {e}"
        ev_id = self.db.insert_evaluation({
            "run_id": run_id, "conversation_id": cid, "checklist_id": checklist_id,
            "provider": res.get("provider") or provider.pid, "model": res.get("model"),
            "prompt_version": res.get("prompt_version"), "status": status, "error": error,
            "latency_s": round(latency, 2), "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(cost, 6), "answers": res.get("answers") or {}, "pii_masked": res.get("pii_masked") or [],
            "raw": res.get("raw"), "total": total, "gross": gross})
        self.db.set_item(run_id, cid, "ok" if status == "ok" else "error", ev_id, None if status == "ok" else error)
        self.db.bump_run(run_id, status == "ok", cost)

    async def _run(self, run_id: int, ids: list[str], config: dict, checklist_id: int, provider) -> None:
        sem = asyncio.Semaphore(self.concurrency)
        try:
            await asyncio.gather(*(self._one(run_id, cid, config, checklist_id, provider, sem) for cid in ids))
            r = self.db.run(run_id)
            self.db.finish_run(run_id, "done" if (r and r["done"]) or not ids else "error")
        except Exception:  # noqa: BLE001
            log.exception("run %s", run_id)
            self.db.finish_run(run_id, "error")
