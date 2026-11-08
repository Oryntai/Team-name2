"""Замер щита до отправки (cqa/guard.py) на DEV.

Поймано: 10 заложенных нарушений — диалог обрезан перед репликой-нарушением, она подаётся черновиком;
пойман = среди нарушений есть ожидаемый критерий.
Ложные тревоги: 20 обычных реплик операторов из чатов, где по медному эталону 01 и 05 — «Да», 03 — не «Нет»,
08 — «Нет» (любое найденное нарушение — ложное); по одной реплике из чата, выбор с фиксированным зерном.

  uv run python lab/eval_guard.py [--only ID[:n] …] [--tag имя]   (~30 вызовов Haiku, ≈ $0,05)
Результат — runs/guard-<tag>.json (runs/ в git не идёт).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import guard  # noqa: E402
from cqa.checklist import load_default  # noqa: E402
from cqa.judge.providers import get_provider  # noqa: E402

# id диалога, номер реплики-нарушения (с 1), какие критерии щит должен назвать (любой из них = пойман)
VIOLATIONS = [
    ("p1-ru-022", 10, {"08"}), ("p1-ru-023", 12, {"08"}), ("p1-kz-013", 12, {"08"}), ("p1-en-013", 12, {"08"}),
    ("p1-ru-029", 10, {"05", "08"}), ("p1-en-014", 11, {"03", "08"}), ("p1-kz-015", 10, {"03", "08"}),
    ("p1-en-015", 10, {"01"}), ("p1-kz-008", 10, {"01"}), ("p1-ru-014", 10, {"01"}),
]
N_NORMAL, SEED = 20, 8


def load_records() -> dict[str, dict]:
    obj = json.loads((ROOT / "data" / "dev.json").read_text(encoding="utf-8"))
    return {r["conversation_id"]: r for r in obj["data"]["records"]}


def normal_cases(recs: dict[str, dict]) -> list[tuple[str, int]]:
    ref = json.loads((ROOT / "lab" / "reference_copper.json").read_text(encoding="utf-8"))["chats"]
    bad = {cid for cid, _, _ in VIOLATIONS}
    clean = sorted(cid for cid, a in ref.items() if cid not in bad and cid in recs
                   and a["01"]["answer"] == "Да" and a["05"]["answer"] == "Да"
                   and a["03"]["answer"] != "Нет" and a["08"]["answer"] == "Нет")
    rnd = random.Random(SEED)
    rnd.shuffle(clean)
    out = []
    for cid in clean[:N_NORMAL]:
        ops = [n for n, t in enumerate(recs[cid]["transcript"], 1) if t["speaker"] == "operator"]
        out.append((cid, rnd.choice(ops)))
    return out


async def run_one(sem, provider, config, rec, n):
    async with sem:
        try:
            return await guard.check(rec, n - 1, rec["transcript"][n - 1]["text"], config, provider)
        except Exception as e:  # noqa: BLE001 — замер: ошибку записываем и считаем отдельно
            return {"error": f"{type(e).__name__}: {e}"}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="только эти случаи: ID:n (n — номер реплики)")
    ap.add_argument("--tag", default=time.strftime("%H%M"))
    ap.add_argument("--conc", type=int, default=10)
    a = ap.parse_args()
    recs, config, provider = load_records(), load_default(), get_provider("haiku")
    cases = [("violation", cid, n, exp) for cid, n, exp in VIOLATIONS]
    cases += [("normal", cid, n, set()) for cid, n in normal_cases(recs)]
    if a.only:
        want = {tuple(x.split(":")) if ":" in x else (x,) for x in a.only}
        cases = [c for c in cases if (c[1], str(c[2])) in want or (c[1],) in want]
    sem = asyncio.Semaphore(a.conc)
    t0 = time.monotonic()
    res = await asyncio.gather(*(run_one(sem, provider, config, recs[cid], n) for _, cid, n, _ in cases))
    rows, cost = [], 0.0
    for (kind, cid, n, exp), r in zip(cases, res):
        found = {i["criterion_id"] for i in r.get("issues", [])}
        hit = bool(found & exp) if kind == "violation" else not found
        cost += r.get("cost_usd", 0.0)
        rows.append({"kind": kind, "id": cid, "n": n, "expected": sorted(exp), "found": sorted(found),
                     "pass": hit and "error" not in r, "draft": recs[cid]["transcript"][n - 1]["text"], "result": r})
        mark = "ok " if rows[-1]["pass"] else "ПРОМАХ" if kind == "violation" else "ТРЕВОГА"
        print(f"{mark:7} {kind:9} {cid}:{n:<3} ждали {sorted(exp) or '—'} нашёл {sorted(found) or '—'}"
              + (f"  ОШИБКА {r['error']}" if "error" in r else ""))
        for i in r.get("issues", []):
            print(f"          {i['criterion_id']}: {i['why'][:160]} | «{i['quote'][:80]}»")
    v = [r for r in rows if r["kind"] == "violation"]
    nrm = [r for r in rows if r["kind"] == "normal"]
    print(f"\nпоймано {sum(r['pass'] for r in v)} из {len(v)}; ложных тревог {sum(not r['pass'] for r in nrm)} из "
          f"{len(nrm)}; ${cost:.4f}; {time.monotonic() - t0:.0f} с")
    out = ROOT / "runs" / f"guard-{a.tag}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main())
