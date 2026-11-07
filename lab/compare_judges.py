"""Сравнение судей (субагенты Claude) на одних и тех же чатах: кого брать «медным эталоном».

Запуск: .venv/bin/python lab/compare_judges.py <папка с ответами судей *.json>
Каждый файл: {"judge": имя, "chats": [{"id": ..., "criteria": [{"id", "answer", "evidence", "confidence", "doubt", "explanation"}]}]}
Сравнивает: заложенные нарушения (PLANTED из lab/probe.py), недопустимые ответы, попарное совпадение судей,
совпадение с большинством остальных судей, совпадение с прогонами моделей из runs/ (Kimi, DeepSeek, Haiku).
"""
import collections, importlib.util, itertools, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("probe", ROOT / "lab" / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
CFG = json.loads((ROOT / "lab" / "checklist_v1.json").read_text())
CRIT = {c["id"]: c for c in CFG["criteria"]}
IDS = list(CRIT)


def load_judges(folder):
    judges = {}
    for f in sorted(pathlib.Path(folder).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            print(f"!! {f.name}: невалидный JSON ({e})")
            continue
        name = data.get("judge") or f.stem
        rows = {}
        for ch in data.get("chats", []):
            crit = {str(x.get("id")).zfill(2): x for x in ch.get("criteria", []) if isinstance(x, dict)}
            rows[ch["id"]] = {k: {"answer": probe.norm_answer(crit.get(k, {}).get("answer"), CRIT[k]),
                                  "raw": crit.get(k, {}).get("answer"), "doubt": bool(crit.get(k, {}).get("doubt")),
                                  "conf": crit.get(k, {}).get("confidence"), "expl": crit.get(k, {}).get("explanation", "")}
                              for k in IDS}
        judges[name] = rows
    return judges


def model_refs():
    refs = {}
    for run in sorted((ROOT / "runs").glob("2026*/results.json")):
        data = json.loads(run.read_text())
        for m in data.get("results", []):
            if m.get("tag") in ("kimi", "ds") and len(m["rows"]) == 60:
                refs[m["tag"]] = {r["id"]: r["answers"] for r in m["rows"]}
        rows = data.get("rows") or []
        if rows and all(r.get("tag") == "haiku" for r in rows) and len(rows) == 60:
            refs["haiku-api"] = {r["id"]: r["answers"] for r in rows}
    return refs


def main():
    judges = load_judges(sys.argv[1])
    names = sorted(judges)
    chats = sorted(set.intersection(*(set(j) for j in judges.values())))
    print(f"Судей: {len(names)} ({', '.join(names)}); общих чатов: {len(chats)}; решений на судью: {len(chats) * len(IDS)}\n")
    ans = {n: {(c, k): judges[n][c][k]["answer"] for c in chats for k in IDS} for n in names}
    for n in names:
        a = ans[n]
        checks = [(c, k, a[(c, k)], exp) for c, d in probe.PLANTED.items() if c in chats for k, exp in d.items()]
        ok = sum(got in exp for *_, got, exp in checks)
        bad = sum(v is None for v in a.values())
        doubts = collections.Counter(k for (c, k) in a if judges[n][c][k]["doubt"])
        others = [m for m in names if m != n]
        maj_agree = 0
        for key in a:
            votes = collections.Counter(str(ans[m][key]) for m in others)
            top, cnt = votes.most_common(1)[0]
            maj_agree += str(a[key]) == top
        totals = [probe.total({k: a[(c, k)] for k in IDS}, CFG) for c in chats]
        print(f"== {n}: заложенные {ok}/{len(checks)}; недопустимых ответов {bad}; с большинством остальных "
              f"{maj_agree}/{len(a)} = {maj_agree / len(a):.0%}; сомнений {sum(doubts.values())} "
              f"({', '.join(f'{k}:{v}' for k, v in sorted(doubts.items()))}); средний итог {sum(t or 0 for t in totals) / len(totals):.1f}")
        for c, k, got, exp in checks:
            if got not in exp:
                print(f"   мимо заложенного: {c} кр.{k}: {got!r}, ждали {sorted(map(str, exp))} — {judges[n][c][k]['expl'][:140]}")
    print("\nПопарное совпадение судей:")
    for x, y in itertools.combinations(names, 2):
        same = sum(str(ans[x][k]) == str(ans[y][k]) for k in ans[x])
        print(f"  {x} ~ {y}: {same}/{len(ans[x])} = {same / len(ans[x]):.0%}")
    print("\nСовпадение по критериям (все судьи одинаково / из чатов):")
    for k in IDS:
        unanimous = sum(len({str(ans[n][(c, k)]) for n in names}) == 1 for c in chats)
        print(f"  {k} {CRIT[k]['title']}: {unanimous}/{len(chats)}")
    refs = model_refs()
    if refs:
        print("\nСовпадение с прогонами моделей на тех же чатах:")
        for n in names:
            parts = []
            for r, rmap in refs.items():
                same = sum(str(ans[n][(c, k)]) == str(rmap[c][k]) for c in chats if c in rmap for k in IDS)
                tot = sum(1 for c in chats if c in rmap) * len(IDS)
                parts.append(f"{r} {same / tot:.0%}" if tot else f"{r} —")
            print(f"  {n}: " + ", ".join(parts))
    print("\nГде судьи разошлись (чат кр.: " + " / ".join(names) + "):")
    for c in chats:
        for k in IDS:
            vals = [str(ans[n][(c, k)]) for n in names]
            if len(set(vals)) > 1:
                print(f"  {c} {k}: " + " / ".join(vals))


if __name__ == "__main__":
    main()
