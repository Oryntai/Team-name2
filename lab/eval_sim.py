"""Замер версии чек-листа на втором DEV (lab/sim: 50 новых чатов, эталон другой модели).

Как в продукте: прецеденты из DEV (транскрипты — var/cqa.db), но один вызов на диалог (без голосования) — для
сравнения версий чек-листа между собой. Строго — бесспорные решения эталона; мягко — с допустимыми вариантами (alt).
Запуск: .venv/bin/python lab/eval_sim.py <чек-лист.json> [ещё чек-листы…] [--conc 12]
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cqa import checklist as ck  # noqa: E402
from cqa import precedents  # noqa: E402
from cqa.db import DB  # noqa: E402
from cqa.judge import evaluate, get_provider  # noqa: E402


async def run(path: str, recs: list[dict], gold: dict, db: DB, prov, sem) -> None:
    cfg = ck.normalize(json.loads(pathlib.Path(path).read_text()))

    async def one(rec):
        prec = precedents.select(db, rec, cfg, 2)
        for attempt in range(3):
            async with sem:
                res = await evaluate(rec, cfg, prov, precedents=prec)
            if res["status"] == "ok":
                return rec["conversation_id"], {k: a["answer"] for k, a in res["answers"].items()}, res.get("cost_usd") or 0
            await asyncio.sleep(5 * (attempt + 1))
        return rec["conversation_id"], None, 0

    out = await asyncio.gather(*(one(r) for r in recs))
    strict = strict_n = soft = soft_n = 0
    per, per_n, misses = collections.Counter(), collections.Counter(), []
    for cid, ans, _ in out:
        if not ans:
            continue
        g = gold[cid]
        for k, want in g["gold"].items():
            want = str(want)
            alt = {str(x) for x in g["alt"].get(k, [])}
            soft_n += 1
            soft += ans[k] == want or ans[k] in alt
            if alt:
                continue
            strict_n += 1; per_n[k] += 1
            if ans[k] == want:
                strict += 1; per[k] += 1
            else:
                misses.append(f"{cid} {k}: эталон {want}, модель {ans[k]}")
    ok = sum(1 for _, a, _ in out if a)
    print(f"{pathlib.Path(path).name}: оценено {ok}/{len(recs)}; строго {strict}/{strict_n} = {100 * strict / strict_n:.1f}%; "
          f"мягко {100 * soft / soft_n:.1f}%; ${sum(c for *_, c in out):.3f}")
    print("   по критериям: " + " ".join(f"{k} {per[k]}/{per_n[k]}" for k in sorted(per_n)))
    for m in misses:
        print("   мимо:", m)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("checklists", nargs="+")
    ap.add_argument("--conc", type=int, default=12)
    args = ap.parse_args()
    data = json.loads((ROOT / "lab/sim/sim_chats.json").read_text())
    recs = [{k: r[k] for k in ("conversation_id", "channel", "language", "transcript")} for r in data["data"]["records"]]
    gold = {g["conversation_id"]: g for g in json.loads((ROOT / "lab/sim/sim_gold.json").read_text())}
    db, prov, sem = DB(), get_provider("haiku"), asyncio.Semaphore(args.conc)
    for path in args.checklists:
        await run(path, recs, gold, db, prov, sem)


if __name__ == "__main__":
    asyncio.run(main())
