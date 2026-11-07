"""Опыт «ИИ-суд» на спорных местах: там, где три независимые оценки модели разошлись по критерию.

Обвинение отстаивает более строгий из спорных ответов, защита — более мягкий; каждая сторона приводит доводы
со ссылками на реплики. Судья видит весь чек-лист, транскрипт и доводы обеих сторон и выносит любой
допустимый ответ. Сравниваем с простым большинством голосов против эталона.

Источники спорных мест (оценки с голосами уже в базе, модель заново не оценивает диалоги):
  DEV — var/cqa.db, прогон --dev-run (эталон — медный, lab/reference_copper.json);
  второй DEV — база --sim-db (эталон — lab/sim/sim_gold.json, засчитываем и допустимые варианты alt).
Запуск: .venv/bin/python lab/court.py --dev-run 5 --sim-db <путь к базе прогона lab/sim>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cqa import checklist as ck  # noqa: E402
from cqa.judge import get_provider  # noqa: E402
from cqa.judge.prompt import build_system, build_user  # noqa: E402
from cqa.judge.providers import cost_usd  # noqa: E402

CFG = ck.load_default()
CRIT = {c["id"]: c for c in CFG["criteria"]}


def strictness(c: dict, a: str) -> float:
    """Чем меньше — тем строже к оператору: нарушение, «Нет», низкая ступень."""
    if c["type"] == "flag":
        return 0 if a == "Да" else 1
    if a == "N/A":
        return 1.5
    if c["type"] == "bool":
        return 0 if a == "Нет" else 1
    return float(a) / max(float(s["value"]) for s in c["steps"])


def disputed(db_path: pathlib.Path, where: str, args: tuple) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    items = []
    for r in con.execute(f"SELECT e.conversation_id, e.answers, c.transcript, c.channel, c.language FROM evaluations e "
                         f"JOIN conversations c ON c.id = e.conversation_id WHERE {where}", args):
        answers = json.loads(r["answers"])
        rec = {"conversation_id": r["conversation_id"], "channel": r["channel"], "language": r["language"],
               "transcript": json.loads(r["transcript"])}
        for k, a in answers.items():
            votes = a.get("votes") or []
            if len(set(votes)) > 1 and k in CRIT:
                items.append({"rec": rec, "crit": k, "votes": votes, "majority": a["answer"]})
    return items


ARG_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["argument"],
              "properties": {"argument": {"type": "string"}}}


def verdict_schema(c: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["reasoning", "answer"],
            "properties": {"reasoning": {"type": "string"}, "answer": {"type": "string", "enum": ck.allowed_answers(c)}}}


async def court(item: dict, prov, sem: asyncio.Semaphore) -> dict:
    c = CRIT[item["crit"]]
    pos = sorted(set(item["votes"]), key=lambda a: strictness(c, a))
    pro, contra = pos[0], pos[-1]
    system = build_system(CFG)
    head = build_user(item["rec"]) + f"\n\n# Спорный критерий {c['id']}. {c['title']}\nНезависимые оценки разошлись: {' · '.join(item['votes'])}."
    cost = 0.0

    async def call(user: str, schema: dict):
        nonlocal cost
        async with sem:
            r = await prov.complete(system, user, schema, max_tokens=8000)
        cost += cost_usd(prov.pid, r)
        return json.loads(r.text)

    side = ("Твоя роль — {role}. Приведи самые сильные доводы за ответ «{ans}» по этому критерию: что именно в репликах "
            "это подтверждает, со ссылками [n]. Только факты из реплик и правила чек-листа, ничего не выдумывай. 3–5 предложений.")
    a_pro, a_contra = await asyncio.gather(
        call(head + "\n\n" + side.format(role="обвинение (строже к оператору)", ans=pro), ARG_SCHEMA),
        call(head + "\n\n" + side.format(role="защита оператора", ans=contra), ARG_SCHEMA))
    judge = await call(head + f"\n\nОбвинение отстаивает «{pro}»: {a_pro['argument']}\n\nЗащита отстаивает «{contra}»: "
                       f"{a_contra['argument']}\n\nТы — судья. Проверь доводы обеих сторон по репликам и вынеси ответ по "
                       f"этому критерию строго по правилам чек-листа; можно выбрать любой допустимый ответ.",
                       verdict_schema(c))
    return {**item, "court": judge["answer"], "pro": pro, "contra": contra, "cost": cost,
            "arguments": {"pro": a_pro["argument"], "contra": a_contra["argument"], "judge": judge["reasoning"]}}


def score(name: str, done: list[dict], truth) -> None:
    rows = [(d, truth(d)) for d in done]
    rows = [(d, t) for d, t in rows if t is not None]
    maj = sum(d["majority"] in t for d, t in rows)
    cou = sum(d["court"] in t for d, t in rows)
    fixed = sum(d["majority"] not in t and d["court"] in t for d, t in rows)
    broke = sum(d["majority"] in t and d["court"] not in t for d, t in rows)
    print(f"{name}: спорных решений с эталоном {len(rows)} — большинство право в {maj}, суд — в {cou}; "
          f"суд исправил {fixed}, испортил {broke}; стоимость ${sum(d['cost'] for d, _ in rows):.3f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev-run", type=int, default=5)
    ap.add_argument("--sim-db", default="")
    ap.add_argument("--conc", type=int, default=12)
    args = ap.parse_args()
    prov = get_provider("haiku")
    sem = asyncio.Semaphore(args.conc)
    ref = json.loads((ROOT / "lab/reference_copper.json").read_text())["chats"]
    dev = [d for d in disputed(ROOT / "var/cqa.db", "e.run_id = ? AND e.status = 'ok'", (args.dev_run,))
           if d["rec"]["conversation_id"] in ref]
    sim = []
    if args.sim_db:
        sim = disputed(pathlib.Path(args.sim_db), "e.id IN (SELECT MAX(id) FROM evaluations WHERE status='ok' GROUP BY conversation_id)", ())
    gold = {g["conversation_id"]: g for g in json.loads((ROOT / "lab/sim/sim_gold.json").read_text())}
    print(f"спорных: DEV {len(dev)}, второй DEV {len(sim)}", flush=True)
    done = await asyncio.gather(*(court(d, prov, sem) for d in dev + sim))
    dev_done, sim_done = done[:len(dev)], done[len(dev):]

    def dev_truth(d):
        return {ref[d["rec"]["conversation_id"]][d["crit"]]["answer"]}

    def sim_truth(d):
        g = gold.get(d["rec"]["conversation_id"])
        if not g:
            return None
        want = g["gold"][d["crit"]]
        return {str(want) if not isinstance(want, str) else want} | {str(x) for x in g["alt"].get(d["crit"], [])}

    score("DEV (медь)", dev_done, dev_truth)
    if sim_done:
        score("второй DEV (эталон)", sim_done, sim_truth)
    out = ROOT / "runs" / "court.json"
    out.write_text(json.dumps([{k: v for k, v in d.items() if k != "rec"} | {"conversation_id": d["rec"]["conversation_id"]}
                               for d in done], ensure_ascii=False, indent=1))
    print(f"подробности: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
