"""Сборка «медного эталона» DEV из ответов судей и замер моделей против него.

Запуск: .venv/bin/python lab/build_copper.py
Источник: runs/judges-60/out20/*.json — 4 судьи на 20 чатах (большинство; ничья → opus-xhigh),
          runs/judges-60/out40/opus-xhigh-*.json — Opus xhigh на остальных 40 чатах.
Выход: lab/reference_copper.json. Это мнение судей Opus, а не эталон организаторов:
трактовки спорных пунктов (02/04/05/07/08) сверять с их ответами.
"""
import collections, importlib.util, json, pathlib, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
J = ROOT / "runs" / "judges-60"
_spec = importlib.util.spec_from_file_location("probe", ROOT / "lab" / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
CFG = json.loads((ROOT / "lab" / "checklist_v1.json").read_text())
CRIT = {c["id"]: c for c in CFG["criteria"]}


def load(path):
    data = json.loads(path.read_text())
    out = {}
    for ch in data["chats"]:
        crit = {str(x.get("id")).zfill(2): x for x in ch["criteria"]}
        out[ch["id"]] = {k: {"answer": probe.norm_answer(crit.get(k, {}).get("answer"), CRIT[k]),
                             "doubt": bool(crit.get(k, {}).get("doubt")), "confidence": crit.get(k, {}).get("confidence"),
                             "explanation": crit.get(k, {}).get("explanation", "")} for k in CRIT}
    return data.get("judge") or path.stem, out


def main():
    ref = {}
    judges20 = dict(load(p) for p in sorted((J / "out20").glob("*.json")))
    if judges20:
        chats = set.intersection(*(set(v) for v in judges20.values()))
        for c in sorted(chats):
            ref[c] = {}
            for k in CRIT:
                votes = collections.Counter(str(judges20[j][c][k]["answer"]) for j in judges20)
                top, n = votes.most_common(1)[0]
                tie = sum(1 for v in votes.values() if v == n) > 1
                best = judges20.get("opus-xhigh", next(iter(judges20.values())))[c][k]
                ans = str(best["answer"]) if tie else top
                ref[c][k] = {"answer": ans, "source": f"большинство {len(judges20)} судей" + (" (ничья → opus-xhigh)" if tie else ""),
                             "agree": f"{votes[ans]}/{len(judges20)}", "doubt": best["doubt"], "explanation": best["explanation"]}
    for p in sorted((J / "out40").glob("opus-xhigh-*.json")):
        _, rows = load(p)
        for c, crit in rows.items():
            ref[c] = {k: {"answer": str(v["answer"]), "source": "opus-xhigh", "agree": "1/1", "doubt": v["doubt"],
                          "explanation": v["explanation"]} for k, v in crit.items()}
    (ROOT / "lab" / "reference_copper.json").write_text(json.dumps(
        {"version": "copper-v1", "built": time.strftime("%Y-%m-%d %H:%M"), "chats": ref}, ensure_ascii=False, indent=1))
    print(f"Медный эталон: {len(ref)} чатов из 60 → lab/reference_copper.json")
    if len(ref) < 60:
        print("  не хватает чатов: дождаться или перезапустить судью по lab/judge_prompt.md")

    # замер: заложенные нарушения в самом эталоне и совпадение моделей с ним
    checks = [(c, k, ref[c][k]["answer"], exp) for c, d in probe.PLANTED.items() if c in ref for k, exp in d.items()]
    print(f"  эталон на заложенных нарушениях: {sum(str(a) in {str(e) for e in exp} for *_, a, exp in checks)}/{len(checks)}")
    models = {}
    for run in sorted((ROOT / "runs").glob("2026*/results.json")):
        data = json.loads(run.read_text())
        for m in data.get("results", []):
            if m.get("tag") in ("kimi", "ds") and len(m["rows"]) == 60:
                models[m["tag"]] = {r["id"]: r["answers"] for r in m["rows"]}
        rows = data.get("rows") or []
        if rows and len(rows) == 60 and all(r.get("tag") == "haiku" for r in rows):
            models["haiku"] = {r["id"]: r["answers"] for r in rows}
    models["база «всё выполнено»"] = {c: {"01": "Да", "02": "Да", "03": "Да", "04": 20, "05": "Да", "06": 10, "07": "Да",
                                          "08": "Нет"} for c in ref}
    print("\nСовпадение с медным эталоном (решения по критериям):")
    for name, ans in models.items():
        both = [c for c in ref if c in ans]
        same = sum(str(ans[c][k]) == ref[c][k]["answer"] for c in both for k in CRIT)
        per_k = {k: sum(str(ans[c][k]) == ref[c][k]["answer"] for c in both) for k in CRIT}
        per_l = collections.defaultdict(lambda: [0, 0])
        for c in both:
            for k in CRIT:
                per_l[c.split("-")[1]][0] += str(ans[c][k]) == ref[c][k]["answer"]
                per_l[c.split("-")[1]][1] += 1
        print(f"  {name}: {same}/{len(both) * 8} = {same / (len(both) * 8):.1%} | "
              + ", ".join(f"{k} {v}/{len(both)}" for k, v in per_k.items())
              + " | " + ", ".join(f"{l} {a / b:.0%}" for l, (a, b) in sorted(per_l.items())))


if __name__ == "__main__":
    main()
