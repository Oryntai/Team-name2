"""Сводка прогонов судьи продукта против ТЕКУЩЕГО медного эталона — без повторных вызовов моделей.

Чек-лист и эталон основная сессия меняет по ходу дня, поэтому сравниваем прогоны на одной линейке:
берём ответы из runs/<прогон>/results.json и считаем совпадение с lab/reference_copper.json как он есть сейчас.

  .venv/bin/python lab/local/score_runs.py runs/2026...-product-local-train-... runs/2026...-product-haiku-train-v5a
"""
import collections
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cqa import checklist as ck  # noqa: E402
from cqa.scoring import score  # noqa: E402

CRIT = ["01", "02", "03", "04", "05", "06", "07", "08"]


def summarize(run: pathlib.Path, ref: dict, cfg: dict) -> dict:
    d = json.loads((run / "results.json").read_text())
    res = d["results"]
    ok = [c for c, r in res.items() if r["status"] == "ok" and all(k in r["answers"] for k in CRIT)]
    same = {k: sum(res[c]["answers"][k]["answer"] == ref[c][k]["answer"] for c in ok) for k in CRIT}
    by_lang = collections.defaultdict(lambda: [0, 0])
    for c in ok:
        for k in CRIT:
            by_lang[c.split("-")[1]][0] += res[c]["answers"][k]["answer"] == ref[c][k]["answer"]
            by_lang[c.split("-")[1]][1] += 1
    diffs = []
    for c in ok:
        ours = score(cfg, {k: res[c]["answers"][k]["answer"] for k in CRIT})["total"] or 0
        theirs = score(cfg, {k: ref[c][k]["answer"] for k in CRIT})["total"] or 0
        diffs.append(abs(ours - theirs))
    lat = [r["latency_s"] for r in res.values() if r.get("latency_s")]
    n = len(ok) * len(CRIT)
    return {"run": run.name, "provider": d["args"].get("provider"), "ok": f"{len(ok)}/{len(res)}",
            "agree": round(100 * sum(same.values()) / n, 1) if n else None,
            "no07": round(100 * sum(v for k, v in same.items() if k != "07") / (len(ok) * 7), 1) if ok else None,
            "crit": {k: f"{v}/{len(ok)}" for k, v in same.items()},
            "lang": {lang: round(100 * a / b) for lang, (a, b) in sorted(by_lang.items())},
            "total_diff_mean": round(statistics.mean(diffs), 1) if diffs else None,
            "within3": f"{sum(x <= 3 for x in diffs)}/{len(diffs)}",
            "latency_median_s": round(statistics.median(lat), 1) if lat else None,
            "wall_s": round(d.get("wall", 0)), "cost_usd": round(d.get("cost", 0), 4)}


def versus(a: pathlib.Path, b: pathlib.Path):
    """Совпадение двух прогонов между собой по каждому критерию (например, новый критерий: своя модель против Haiku)."""
    ra = json.loads((a / "results.json").read_text())["results"]
    rb = json.loads((b / "results.json").read_text())["results"]
    ids = [c for c in ra if ra[c]["status"] == "ok" and rb.get(c, {}).get("status") == "ok"]
    crit = list(ra[ids[0]]["answers"])
    print(f"\n{a.name}  против  {b.name}: {len(ids)} диалогов")
    print("  " + ", ".join(f"{k} {sum(ra[c]['answers'][k]['answer'] == rb[c]['answers'][k]['answer'] for c in ids)}"
                         f"/{len(ids)}" for k in crit))


def main():
    if sys.argv[1] == "--vs":
        versus(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
        return
    ref = json.loads((ROOT / "lab" / "reference_copper.json").read_text())
    cfg = ck.load_default()
    print(f"эталон: {ref.get('version')} ({ref.get('built')})")
    for p in sys.argv[1:]:
        s = summarize(pathlib.Path(p), ref["chats"], cfg)
        print(f"\n{s['run']}  [{s['provider']}]  оценено {s['ok']}")
        print(f"  совпадение {s['agree']}% (без 07: {s['no07']}%); итог ±3 — {s['within3']}, "
              f"ср. расхождение итога {s['total_diff_mean']}")
        print("  по критериям: " + ", ".join(f"{k} {v}" for k, v in s["crit"].items()))
        print(f"  по языкам: {s['lang']}; задержка медиана {s['latency_median_s']} с; весь прогон {s['wall_s']} с; "
              f"${s['cost_usd']}")


if __name__ == "__main__":
    main()
