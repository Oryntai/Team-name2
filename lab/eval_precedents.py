"""Опыт «судья с прецедентами»: к оцениваемому диалогу добавляем k самых похожих размеченных диалогов DEV
с ответами медного эталона — без самого диалога (leave-one-out), как будет на контрольном наборе.

Запуск (флаги — как у lab/eval_product.py, плюс PREC_K в окружении, по умолчанию 2):
  .venv/bin/python lab/eval_precedents.py --split holdout --conc 8 --tag prec-h1
Продукт не меняется: на время прогона подменяется только сборка сообщения модели (build_user).
Похожесть — TF-IDF по репликам клиента и оператора (бот и системные события у всех одинаковые).
У прецедентов нет номеров реплик и ссылок [n], чтобы модель не путала их с репликами оцениваемого диалога.
"""
from __future__ import annotations

import collections
import importlib
import json
import math
import os
import pathlib
import re
import runpy
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
K = int(os.environ.get("PREC_K", "2"))
ROLE = {"client": "клиент", "operator": "оператор", "bot": "бот", "system": "событие"}

recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]}
ref = json.loads((ROOT / "lab" / "reference_copper.json").read_text())["chats"]
titles = {c["id"]: c["title"] for c in json.loads((ROOT / "cqa" / "checklists" / "default.json").read_text())["criteria"]}


def words(r: dict) -> list[str]:
    return re.findall(r"\w+", " ".join(t["text"] for t in r["transcript"] if t["speaker"] in ("client", "operator")).lower())


docs = {cid: collections.Counter(words(recs[cid])) for cid in ref}
df = collections.Counter(w for c in docs.values() for w in c)
idf = {w: math.log(len(docs) / n) for w, n in df.items()}


def vec(counts: collections.Counter) -> dict[str, float]:
    v = {w: n * idf.get(w, math.log(len(docs))) for w, n in counts.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


vecs = {cid: vec(c) for cid, c in docs.items()}


def neighbors(cid: str, k: int = K) -> list[str]:
    q = vecs.get(cid) or vec(collections.Counter(words(recs[cid])))
    sims = sorted(((sum(q.get(w, 0) * x for w, x in vecs[o].items()), o) for o in vecs if o != cid), reverse=True)
    return [o for _, o in sims[:k]]


def strip_refs(s: str) -> str:
    s = re.sub(r"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]", "", s)
    return re.sub(r"\s*\((?:реплик[аи]?|replies)\s*[\d,\s–-]+\)", "", s)


def block(i: int, cid: str) -> str:
    r = recs[cid]
    lines = [f"--- Прецедент {i}: {r['channel']} · {r['language']}"]
    lines += [f"{ROLE.get(t['speaker'], t['speaker'])}: {t['text']}" for t in r["transcript"]
              if t["speaker"] != "bot"]  # шаблонные реплики бота одинаковы во всех чатах
    lines.append("Утверждённая оценка:")
    lines += [f"- {k} {titles.get(k, '')}: {a['answer']} — {strip_refs(a.get('explanation') or '')[:260]}"
              for k, a in sorted(ref[cid].items())]
    return "\n".join(lines)


ev = importlib.import_module("cqa.judge.evaluate")
_orig = ev.build_user


def build_user(record: dict, transcript=None) -> str:
    nb = neighbors(record["conversation_id"])
    pre = ("Прецеденты — похожие диалоги, уже оценённые проверяющими по этому же чек-листу. Используй их, чтобы понять, "
           "как трактуются критерии. Оцениваемый диалог может отличаться в важном: оценивай только по его репликам, "
           "оценки прецедентов механически не переноси, ссылайся только на номера реплик оцениваемого диалога.\n\n"
           + "\n\n".join(block(i, n) for i, n in enumerate(nb, 1)) + "\n\n=== Оцениваемый диалог ===\n")
    return pre + _orig(record, transcript)


ev.build_user = build_user

if __name__ == "__main__":
    if "--show" in sys.argv:  # проверить соседей без вызова модели
        for cid in sorted(ref):
            print(cid, "→", ", ".join(neighbors(cid)))
        sys.exit(0)
    sys.argv[0] = str(ROOT / "lab" / "eval_product.py")
    runpy.run_path(sys.argv[0], run_name="__main__")
