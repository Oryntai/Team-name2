"""Независимые оценки и детерминированное объединение ответов без изменения исходных оценок."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from decimal import Decimal

from . import judge
from .checklist import NA


def medoid_index(results: list[dict]) -> int:
    """Самый согласованный с остальными вызов; при равенстве — первый."""
    def agreement(i):
        return sum(
            answer["answer"] == other["answers"][cid]["answer"]
            for j, other in enumerate(results) if j != i
            for cid, answer in results[i]["answers"].items() if cid in other["answers"]
        )

    return max(range(len(results)), key=agreement)


def merge_answers(results: list[dict], config: dict) -> dict:
    """Объединяет успешные полные ответы. N/A при ничьей шкалы разрешается медоидом."""
    if not results:
        return {}
    medoid = medoid_index(results)
    answers = {}
    for criterion in config["criteria"]:
        cid = criterion["id"]
        if cid not in results[0]["answers"]:
            continue
        votes = [result["answers"][cid]["answer"] for result in results]
        counts = Counter(votes)
        top = max(counts.values())
        leaders = {value for value, count in counts.items() if count == top}
        if len(leaders) == 1:
            winner = next(iter(leaders))
        elif top == 1 and criterion["type"] == "scale" and NA not in votes:
            winner = sorted(votes, key=Decimal)[(len(votes) - 1) // 2]
        else:
            # При ничьей нескольких мод (2/2/1) выбираем ответ медоида, если он среди лидеров.
            winner = votes[medoid] if votes[medoid] in leaders else next(v for v in votes if v in leaders)
        source = medoid if votes[medoid] == winner else votes.index(winner)
        answers[cid] = {**results[source]["answers"][cid], "votes": votes,
                        "agreement": f"{counts[winner]}/{len(votes)}"}
    return answers


def merge_results(results: list[dict], config: dict) -> dict:
    """Объединяет успешные вызовы, учитывая расходы, задержку и raw также у неудачных."""
    if not results:
        raise ValueError("Нужен хотя бы один вызов.")
    successful = [result for result in results if result.get("status") == "ok"]
    base = successful[medoid_index(successful)] if successful else results[0]
    return {
        **base,
        "status": "ok" if successful else "error",
        "error": None if successful else "; ".join(str(r.get("error") or "Неизвестная ошибка") for r in results),
        "answers": merge_answers(successful, config),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in results),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in results),
        "cost_usd": sum(r.get("cost_usd") or 0.0 for r in results),
        "latency_s": max(r.get("latency_s") or 0.0 for r in results),
        "model_calls": len(results),
        "raw": json.dumps([r.get("raw") or "" for r in results], ensure_ascii=False),
        "thinking": [r.get("thinking") or "" for r in successful],  # в порядке votes
    }


async def evaluate_voted(record, config, provider, n, sem=None, only=None, precedents=None) -> dict:
    """Делает n параллельных оценок; каждый вызов отдельно занимает слот sem."""
    if type(n) is not int or n < 1:
        raise ValueError("Число голосов должно быть положительным целым.")

    async def evaluate_one():
        started = time.monotonic()
        try:
            kwargs = {} if only is None else {"only": only}
            if precedents is not None:
                kwargs["precedents"] = precedents
            return await judge.evaluate(record, config, provider, **kwargs)
        except Exception as exc:  # noqa: BLE001 — сбой одного вызова не отменяет остальные
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "answers": {},
                    "provider": provider.pid, "model": judge.model_name(provider.pid),
                    "prompt_version": judge.PROMPT_VERSION, "pii_masked": [],
                    "latency_s": round(time.monotonic() - started, 2), "raw": ""}

    async def call():
        if sem is None:
            return await evaluate_one()
        async with sem:
            return await evaluate_one()

    return merge_results(await asyncio.gather(*(call() for _ in range(n))), config)
