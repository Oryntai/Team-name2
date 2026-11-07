"""Оценка одного диалога: маскировка ПДн → один вызов модели со строгой схемой → разбор ответа.

Итог здесь не считается: его считает cqa.scoring по ответам (формула чек-листа).
Ответ модели не подменяется и не «чинится» правилами: при недопустимом ответе — ошибка оценки.
"""
from __future__ import annotations

import json
import re
import time

from ..pii import mask_transcript
from .prompt import PROMPT_VERSION, build_schema, build_system, build_user, normalize_answer
from .providers import PROVIDERS, ProviderError, cost_usd, model_name


def _parse(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)  # локальные модели иногда обрамляют JSON текстом
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _evidence(raw, n_replies: int) -> list[int]:
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= n_replies and v not in out:
            out.append(v)
    return sorted(out)


async def evaluate(record: dict, config: dict, provider, *, only=None, mask_pii: bool = True) -> dict:
    from ..incremental import fingerprints

    pid = provider.pid
    ids = {c["id"] for c in config["criteria"]} if only is None else set(only)
    unknown = ids - {c["id"] for c in config["criteria"]}
    if unknown:
        raise ValueError("Неизвестные критерии: " + ", ".join(sorted(unknown)))
    selected = [c for c in config["criteria"] if c["id"] in ids]
    tr = record["transcript"]
    masked, pii = mask_transcript(tr) if mask_pii else (tr, [])
    system, user = build_system(config), build_user(record, masked)
    schema = build_schema({**config, "criteria": selected})
    if only is not None and len(selected) < len(config["criteria"]):  # все критерии — ровно прежний промпт
        user += "\n\nОцени только критерии: " + ", ".join(c["id"] for c in selected) + ". Остальные не оценивай"
    base = {"provider": pid, "model": model_name(pid), "prompt_version": PROMPT_VERSION, "pii_masked": pii,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "answers": {}, "raw": ""}
    t0 = time.monotonic()
    if not selected:
        return {**base, "status": "ok", "error": None, "latency_s": 0.0}
    try:
        reply = await provider.complete(system, user, schema)
    except ProviderError as e:
        return {**base, "status": "error", "error": str(e), "latency_s": round(time.monotonic() - t0, 2)}
    latency = round(time.monotonic() - t0, 2)
    out = {**base, "model": reply.model or base["model"], "latency_s": latency, "raw": reply.text,
           "input_tokens": reply.input_tokens + reply.cache_read + reply.cache_write,
           "output_tokens": reply.output_tokens, "cost_usd": cost_usd(pid, reply) if pid in PROVIDERS else 0.0}
    js = _parse(reply.text)
    crit = (js or {}).get("criteria") if isinstance(js, dict) else None
    if isinstance(crit, list):  # на случай модели без схемы: список вместо объекта
        crit = {str(x.get("id")): x for x in crit if isinstance(x, dict)}
    if not isinstance(crit, dict):
        return {**out, "status": "error", "error": "Модель вернула ответ не в формате JSON чек-листа."}
    answers, problems = {}, []
    hashes = fingerprints(config, out["model"])
    for c in selected:
        item = crit.get(c["id"])
        if not isinstance(item, dict):
            problems.append(f"{c['id']}: нет ответа")
            continue
        a = normalize_answer(c, item.get("answer"))
        if a is None:
            problems.append(f"{c['id']}: недопустимый ответ {item.get('answer')!r}")
            continue
        answers[c["id"]] = {"answer": a, "explanation": str(item.get("explanation") or "").strip(),
                            "evidence": _evidence(item.get("evidence"), len(tr)),
                            "fingerprint": hashes[c["id"]], "reused_from": None}
    if problems:
        return {**out, "answers": answers, "status": "error", "error": "Ответ модели неполный: " + "; ".join(problems)}
    return {**out, "answers": answers, "status": "ok", "error": None}
