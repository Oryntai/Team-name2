"""Сравнение results.json (формат JSON 1.0) с эталоном симуляции lab/sim/sim_gold.json.

Строго — только бесспорные решения (без alt); мягко — засчитываем и допустимые варианты из alt.
Запуск: .venv/bin/python lab/sim/compare.py <results.json>
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
CODE = {"contact": "01", "discovery": "02", "identification": "03", "consultation": "04",
        "empathy": "05", "dialogue": "06", "closure": "07", "critical": "08"}


def norm(v):
    if v is None:
        return "N/A"
    if v is True:
        return "Да"
    if v is False:
        return "Нет"
    return int(v)


def main() -> None:
    res = json.loads(pathlib.Path(sys.argv[1]).read_text())
    by_id = {r["conversation_id"]: r for r in res["results"]}
    gold = json.loads((HERE / "sim_gold.json").read_text())
    strict = collections.Counter()
    strict_n = collections.Counter()
    soft = 0
    lang = collections.Counter()
    lang_n = collections.Counter()
    diffs, misses = [], []
    for g in gold:
        r = by_id[g["conversation_id"]]
        got = {CODE[c["criterion_id"]]: norm(c["value"]) for c in r["criteria"]}
        for code, want in g["gold"].items():
            ok_soft = got[code] == want or got[code] in g["alt"].get(code, [])
            soft += ok_soft
            if code in g["alt"]:
                continue
            strict_n[code] += 1
            lang_n[g["language"]] += 1
            if got[code] == want:
                strict[code] += 1
                lang[g["language"]] += 1
            else:
                misses.append((g["conversation_id"], code, want, got[code],
                               next(c["rationale"] for c in r["criteria"] if CODE[c["criterion_id"]] == code)))
        if r["score"] and g["final_score"] is not None:
            diffs.append(abs(r["score"]["final_score"] - g["final_score"]))
    total = sum(strict.values())
    n = sum(strict_n.values())
    print(f"строго (бесспорные): {total}/{n} = {100 * total / n:.1f}%")
    print(f"мягко (с допустимыми): {soft}/{8 * len(gold)} = {100 * soft / (8 * len(gold)):.1f}%")
    print("по критериям:", " ".join(f"{c} {strict[c]}/{strict_n[c]}" for c in sorted(strict_n)))
    print("по языкам:", " ".join(f"{k} {100 * lang[k] / lang_n[k]:.1f}%" for k in sorted(lang_n)))
    print(f"итог: средняя разница {sum(diffs) / len(diffs):.2f}; в пределах ±3 — {sum(d <= 3 for d in diffs)}/{len(diffs)}")
    print("\nпромахи по бесспорным:")
    for cid, code, want, got, why in misses:
        print(f"  {cid} {code}: эталон {want}, модель {got} — {why[:220]}")


if __name__ == "__main__":
    main()
