"""Расхождения судьи продукта с медным эталоном по нескольким прогонам: что систематично, что шум, что даст голосование.

Запуск: .venv/bin/python lab/disagreements.py RUN_DIR [RUN_DIR ...] [--out dump.md]
RUN_DIR — папки runs/<ts>-product-… с results.json (один сплит, один чек-лист).
По каждому критерию: «стабильно» — мимо во всех прогонах, «шум» — только в части; голосование — большинство прогонов
(при ничьей — ответ первого). В --out — каждое расхождение с обоснованиями эталона и модели.
"""
import argparse, collections, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out")
    a = ap.parse_args()
    ref = json.loads((ROOT / "lab/reference_copper.json").read_text())["chats"]
    runs = [json.loads((pathlib.Path(r) / "results.json").read_text())["results"] for r in a.runs]
    ok = [{k for k, v in r.items() if v.get("status") == "ok"} for r in runs]
    chats = sorted(set.intersection(*ok) & set(ref))
    crits = sorted(ref[chats[0]])
    stable, noisy, vote_ok = collections.Counter(), collections.Counter(), collections.Counter()
    lines = []
    for c in chats:
        for k in crits:
            gold = ref[c][k]["answer"]
            got = [r[c]["answers"][k]["answer"] for r in runs]
            cnt = collections.Counter(got)
            vote = next(x for x in got if cnt[x] == max(cnt.values()))
            vote_ok[k] += vote == gold
            miss = [g != gold for g in got]
            if not any(miss):
                continue
            (stable if all(miss) else noisy)[k] += 1
            lines.append(f"### {c} · {k} · эталон {gold} · модель {' / '.join(got)} · {'стабильно' if all(miss) else 'шум'}\n"
                         f"- эталон: {ref[c][k].get('explanation', '')}\n"
                         + "".join(f"- прогон {i + 1} ({got[i]}): {r[c]['answers'][k]['explanation']} "
                                   f"[реплики {r[c]['answers'][k].get('evidence')}]\n" for i, r in enumerate(runs) if miss[i]))
    n, m = len(chats), len(chats) * len(crits)
    print(f"чатов {n}, прогонов {len(runs)}")
    for k in crits:
        print(f"  {k}: стабильно мимо {stable[k]}, шум {noisy[k]}, голосование {vote_ok[k]}/{n}")
    single = [sum(r[c]["answers"][k]["answer"] == ref[c][k]["answer"] for c in chats for k in crits) / m for r in runs]
    print(f"отдельные прогоны: {', '.join(f'{x:.1%}' for x in single)}; голосование: {sum(vote_ok.values()) / m:.1%}; "
          f"стабильно мимо {sum(stable.values())}, шум {sum(noisy.values())}")
    if a.out:
        pathlib.Path(a.out).write_text("\n".join(lines))
        print("подробно:", a.out)


if __name__ == "__main__":
    main()
