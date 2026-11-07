"""Похожие утверждённые диалоги: эталон и решения проверяющих, без самоцитирования.

Похожесть считается по русскому тексту: казахские и английские диалоги сравниваются по переводу (если он есть
в базе), поэтому к казахскому чату находится русский или английский диалог на ту же тему. Без перевода TF-IDF
не видит, что «Жоғарыда жаздым» и «я же писал выше» — одно и то же, и подбирает диалог на чужую тему
(замер 8.11 на 60 чатах DEV: у английских той же темы 3 из 30 соседей, с переводом — 28 из 30).
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import math
import os
from pathlib import Path
import re

from .checklist import load_default
from .incremental import fingerprints

log = logging.getLogger("cqa.precedents")
REFERENCE = Path(__file__).with_suffix("") / "dev_reference.json"
# API различает подтверждённый и изменённый ответ; принимаем также общий статус decided.
DECIDED = {"confirmed", "changed", "decided"}
ROLE = {"client": "клиент", "operator": "оператор", "system": "событие"}
_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _is_ru(language) -> bool:
    return (language or "").strip().lower() == "ru"


def _ru_texts(db, cid: str, transcript: list) -> list[str] | None:
    """Перевод реплик на русский из базы (cqa/translate.py) или None, если его нет или он не по этому транскрипту."""
    saved = db.translation(cid)["translation"]
    if not saved or len(saved["data"]) != len(transcript):
        return None
    return [item.get("text", "") for item in saved["data"]]


def _words(record: dict, texts: list[str] | None = None) -> Counter:
    text = " ".join(texts[i] if texts else t.get("text", "") for i, t in enumerate(record["transcript"])
                    if t.get("speaker") in ("client", "operator"))
    return Counter(re.findall(r"\w+", text.lower()))


async def prepare(db, record: dict, provider) -> tuple[float, int]:
    """Переводит для поиска похожих оцениваемый диалог и диалоги пула не на русском, если перевода ещё нет.

    Только для облачной модели: своя модель наружу ничего не отправляет и ищет похожие по исходному тексту.
    Перевод сохраняется в базе (его же показывает карточка) и делается один раз. Возвращает цену и число вызовов.
    Сбой перевода не мешает оценке: диалог сравнивается по исходному тексту.
    """
    from . import judge, translate
    from .judge.providers import PROVIDERS

    if os.environ.get("CQA_PRECEDENTS_TRANSLATE", "1") == "0" or int(os.environ.get("CQA_PRECEDENTS", "2")) <= 0:
        return 0.0, 0
    if PROVIDERS.get(provider.pid, {}).get("kind") != "anthropic" or not judge.available("haiku"):
        return 0.0, 0
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["dialogs"]
    reviewed = {row["conversation_id"] for row in db.q(
        "SELECT DISTINCT e.conversation_id FROM appeals a JOIN evaluations e ON e.id = a.evaluation_id")}
    pool = [db.record(cid) for cid in sorted(set(reference) | reviewed) if cid != record["conversation_id"]]
    todo = [r for r in [record, *pool] if r and r.get("transcript") and not _is_ru(r.get("language"))
            and _ru_texts(db, r["conversation_id"], r["transcript"]) is None]
    if not todo:
        return 0.0, 0
    translator = judge.get_provider("haiku")
    loop = id(asyncio.get_running_loop())

    async def one(rec) -> tuple[float, int]:
        cid = rec["conversation_id"]
        async with _LOCKS.setdefault((loop, cid), asyncio.Lock()):  # параллельные оценки не платят дважды
            if _ru_texts(db, cid, rec["transcript"]) is not None:
                return 0.0, 0
            try:
                result = await translate.translate(db, rec, translator)
            except Exception as e:  # noqa: BLE001 — без перевода сравним по исходному тексту
                log.warning("перевод %s для поиска похожих не получен: %s", cid, e)
                return 0.0, 1
            return float(result.get("cost_usd") or 0.0), 1

    results = await asyncio.gather(*(one(r) for r in todo))
    # В цену оценки идёт перевод самого диалога; пул переводится один раз на базу (видно в журнале переводов).
    return results[0] if todo[0] is record else (0.0, 0)


def select(db, record: dict, config: dict, k: int | None = None) -> list[dict]:
    """Свежий индекс небольшого корпуса; исправления видны уже следующему прогону."""
    k = int(os.environ.get("CQA_PRECEDENTS", "2")) if k is None else k
    if k <= 0:
        return []
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["dialogs"]
    defaults = {c["id"]: c for c in load_default()["criteria"]}
    compatible = {c["id"] for c in config["criteria"]
                  if c["id"] in defaults
                  and all(c.get(f) == defaults[c["id"]].get(f) for f in ("id", "title", "type"))}
    labels = {cid: {key: dict(value) for key, value in answers.items() if key in compatible}
              for cid, answers in reference.items()}
    sources = dict.fromkeys(labels, "reference")
    # Последняя успешная оценка диалога, независимо от модели. Более старые решения не подмешиваем.
    latest = db.q("SELECT conversation_id, MAX(id) AS id FROM evaluations WHERE status='ok' GROUP BY conversation_id")
    hashes = {}
    for row in latest:
        appeals = [a for a in db.appeals_for(row["id"]) if a["status"] in DECIDED]
        if not appeals:
            continue
        ev = db.evaluation(row["id"])
        # Отпечаток включает модель: сравниваем определения в контексте модели исходной оценки.
        model = ev["model"]
        if model not in hashes:
            hashes[model] = fingerprints(config, model)
        answers = {key: dict(value) for key, value in ev["answers"].items()
                   if key in hashes[model] and value.get("fingerprint") == hashes[model][key]}
        for appeal in sorted(appeals, key=lambda a: (a["decided_at"] or "", a["id"])):
            answer = answers.get(appeal["criterion_id"])
            if answer is not None:
                if appeal["new_answer"] is not None:
                    answer["answer"] = appeal["new_answer"]
                answer["explanation"] = appeal["reason"] or ""
        cid = row["conversation_id"]
        labels[cid], sources[cid] = answers, "reviewer"

    corpus = {}
    for cid, answers in labels.items():
        conv = db.conversation(cid)
        if conv is not None:
            corpus[cid] = {"conversation_id": cid, "channel": conv["channel"], "language": conv["language"],
                           "transcript": conv["transcript"], "labels": answers, "source": sources[cid]}
    if not corpus:
        return []
    # По-русски сравниваем, только если оцениваемый диалог есть по-русски; иначе — всё по исходному тексту, как раньше.
    query_ru = None if _is_ru(record.get("language")) else _ru_texts(db, record["conversation_id"],
                                                                      record["transcript"])
    in_russian = _is_ru(record.get("language")) or query_ru is not None
    docs = {cid: _words(conv, None if not in_russian or _is_ru(conv["language"])
                        else _ru_texts(db, cid, conv["transcript"]))
            for cid, conv in corpus.items()}
    df = Counter(word for counts in docs.values() for word in counts)
    idf = {word: math.log(len(docs) / n) for word, n in df.items()}

    def vec(counts):
        values = {word: n * idf.get(word, math.log(len(docs))) for word, n in counts.items()}
        norm = math.sqrt(sum(x * x for x in values.values())) or 1.0
        return {word: x / norm for word, x in values.items()}

    vectors = {cid: vec(counts) for cid, counts in docs.items()}
    query = vectors.get(record["conversation_id"]) or vec(_words(record, query_ru))
    sims = sorted(((sum(query.get(word, 0) * x for word, x in vector.items()), cid)
                   for cid, vector in vectors.items() if cid != record["conversation_id"]), reverse=True)
    return [corpus[cid] for _, cid in sims[:k]]


def _strip_refs(text: str) -> str:
    text = re.sub(r"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]", "", text)
    return re.sub(r"\s*\((?:реплик[аи]?|replies)\s*[\d,\s–-]+\)", "", text)


def block(precedents: list[dict], config: dict) -> str:
    if not precedents:
        return ""
    titles = {c["id"]: c["title"] for c in config["criteria"]}
    parts = ["Прецеденты — похожие диалоги, уже оценённые проверяющими по этому же чек-листу. Используй их, чтобы понять, "
             "как трактуются критерии. Оцениваемый диалог может отличаться в важном: оценивай только по его репликам, "
             "оценки прецедентов механически не переноси, ссылайся только на номера реплик оцениваемого диалога."]
    for i, precedent in enumerate(precedents, 1):
        lines = [f"--- Прецедент {i}: {precedent['channel']} · {precedent['language']}"]
        lines += [f"{ROLE.get(t.get('speaker'), t.get('speaker', ''))}: {t.get('text', '')}"
                  for t in precedent["transcript"] if t.get("speaker") != "bot"]
        lines.append("Утверждённая оценка:")
        lines += [f"- {key} {titles[key]}: {a['answer']} — {_strip_refs(a.get('explanation') or '')[:260]}"
                  for key, a in sorted(precedent["labels"].items()) if key in titles]
        parts.append("\n".join(lines))
    parts.append("=== Оцениваемый диалог ===\n")
    return "\n\n".join(parts)
