"""Переносится ли подгонка на чужие данные: оценка новых чатов (не DEV) против плана генератора.

Чаты — lab/local/data/synth_chats.jsonl (сессия «Своя локальная модель-судья»: Haiku писала новые чаты в стиле DEV
по плану нарушений, темы новые). Ожидаемый ответ берётся из плана — та же таблица, что expected() в lab/local/synth.py
(перенесена сюда, чтобы не импортировать чужой скрипт). Модель-судья для эталона не нужна.

Сравниваем: чек-лист v5 без прецедентов (до подгонки 7.11 вечером), v6 без прецедентов, продукт — v6 + прецеденты
из DEV. Один вызов на диалог; выборка по языкам поровну, зерно фиксировано.
Запуск: .venv/bin/python lab/eval_synth.py [--n 60] [--seed 7] [--conc 16]
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import random
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa import checklist as ck  # noqa: E402
from cqa import precedents  # noqa: E402
from cqa.db import DB  # noqa: E402
from cqa.judge import evaluate, get_provider  # noqa: E402

SCALE = {"04": ["20", "15", "10", "5", "0"], "06": ["10", "7", "4", "0"]}
EXPECT = {"01": {"ok": "Да"}, "02": {"na": "N/A", "yes": "Да"}, "03": {"na": "N/A", "yes": "Да", "no": "Нет"},
          "05": {"ok": "Да"}, "07": {"ok": "Да", "na": "N/A"}, "08": {"none": "Нет"}}
CLEAN = {"ok", "na", "yes", "20", "10", "none"}  # пункты плана без нарушения


def expected(cid: str, key: str) -> str:
    if cid in ("04", "06"):
        return "N/A" if key == "na" else key
    if cid == "08":
        return EXPECT["08"].get(key, "Да")
    return EXPECT.get(cid, {}).get(key, "Нет")


def git_json(rev_path: str) -> dict:
    return json.loads(subprocess.run(["git", "show", rev_path], cwd=ROOT, capture_output=True, text=True, check=True).stdout)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--conc", type=int, default=16)
    args = ap.parse_args()

    chats = [json.loads(line) for line in (ROOT / "lab/local/data/synth_chats.jsonl").read_text().splitlines() if line]
    rng = random.Random(args.seed)
    by_lang = collections.defaultdict(list)
    for c in chats:
        by_lang[c["language"]].append(c)
    sample = [c for lang in sorted(by_lang) for c in rng.sample(by_lang[lang], args.n // len(by_lang))]
    configs = {"v5": ck.normalize(git_json("25674b2:cqa/checklists/default.json")), "v6": ck.load_default()}
    db = DB()  # var/cqa.db: транскрипты DEV для прецедентов
    prov = get_provider("haiku")
    sem = asyncio.Semaphore(args.conc)
    variants = [("v5", "v5", False), ("v6", "v6", False), ("продукт (v6 + прецеденты)", "v6", True)]

    async def one(name, cfg_name, use_prec, chat):
        cfg = configs[cfg_name]
        rec = {k: chat[k] for k in ("conversation_id", "channel", "language", "transcript")}
        prec = precedents.select(db, rec, cfg, 2) if use_prec else None
        for attempt in range(3):
            async with sem:
                res = await evaluate(rec, cfg, prov, precedents=prec)
            if res["status"] == "ok":
                return name, chat, {k: a["answer"] for k, a in res["answers"].items()}, res.get("cost_usd") or 0
            await asyncio.sleep(5 * (attempt + 1))
        return name, chat, None, 0

    t0 = time.monotonic()
    out = await asyncio.gather(*(one(n, c, p, ch) for n, c, p in variants for ch in sample))
    cost = sum(x[3] for x in out)
    print(f"{len(sample)} новых чатов ({', '.join(f'{k} {len(v)}' for k, v in sorted(by_lang.items()))} в наборе), "
          f"{len(out)} оценок за {time.monotonic() - t0:.0f} с, ${cost:.3f}")
    res = collections.defaultdict(dict)
    for name, chat, ans, _ in out:
        if ans:
            res[name][chat["conversation_id"]] = ans
    plans = {c["conversation_id"]: c["plan"] for c in sample}
    langs = {c["conversation_id"]: c["language"] for c in sample}
    for name, _, _ in variants:
        r = res[name]
        exact = near = tot = 0
        viol_ok = viol_n = 0
        per = collections.Counter(); per_n = collections.Counter(); per_l = collections.Counter(); per_ln = collections.Counter()
        for cid, ans in r.items():
            for k, key in plans[cid].items():
                exp, got = expected(k, key), ans.get(k)
                ok = got == exp
                nb = ok or (k in SCALE and exp in SCALE[k] and got in SCALE[k]
                            and abs(SCALE[k].index(exp) - SCALE[k].index(got)) <= 1)
                tot += 1; exact += ok; near += nb
                per[k] += ok; per_n[k] += 1; per_l[langs[cid]] += ok; per_ln[langs[cid]] += 1
                if key not in CLEAN:
                    viol_n += 1; viol_ok += ok
        print(f"\n{name}: оценено {len(r)}/{len(sample)}; совпало с планом {exact}/{tot} = {100 * exact / tot:.1f}% "
              f"(шкалы ±1 ступень: {100 * near / tot:.1f}%); заложенные нарушения {viol_ok}/{viol_n} = {100 * viol_ok / max(1, viol_n):.0f}%")
        print("  по критериям: " + ", ".join(f"{k} {per[k]}/{per_n[k]}" for k in sorted(per_n)))
        print("  по языкам: " + ", ".join(f"{l} {100 * per_l[l] / per_ln[l]:.0f}%" for l in sorted(per_ln)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = ROOT / "runs" / f"{stamp}-synth-check.json"
    path.write_text(json.dumps({"args": vars(args), "sample": [c["conversation_id"] for c in sample],
                                "results": res, "cost": cost}, ensure_ascii=False, indent=1))
    print(f"\nПодробности: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
