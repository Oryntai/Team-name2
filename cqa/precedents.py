"""Похожие утверждённые диалоги: эталон и решения проверяющих, без самоцитирования."""
from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import re

from .checklist import load_default
from .incremental import fingerprints

REFERENCE = Path(__file__).with_suffix("") / "dev_reference.json"
# API различает подтверждённый и изменённый ответ; принимаем также общий статус decided.
DECIDED = {"confirmed", "changed", "decided"}
ROLE = {"client": "клиент", "operator": "оператор", "system": "событие"}


def _words(record: dict) -> Counter:
    text = " ".join(t.get("text", "") for t in record["transcript"]
                    if t.get("speaker") in ("client", "operator"))
    return Counter(re.findall(r"\w+", text.lower()))


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
    docs = {cid: _words(conv) for cid, conv in corpus.items()}
    df = Counter(word for counts in docs.values() for word in counts)
    idf = {word: math.log(len(docs) / n) for word, n in df.items()}

    def vec(counts):
        values = {word: n * idf.get(word, math.log(len(docs))) for word, n in counts.items()}
        norm = math.sqrt(sum(x * x for x in values.values())) or 1.0
        return {word: x / norm for word, x in values.items()}

    vectors = {cid: vec(counts) for cid, counts in docs.items()}
    query = vectors.get(record["conversation_id"]) or vec(_words(record))
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
