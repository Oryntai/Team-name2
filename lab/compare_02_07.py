"""Согласие двух разметчиков по 02 и 07 на одних и тех же чатах (например, Opus и Codex).

Запуск: .venv/bin/python lab/compare_02_07.py [папка_A] [папка_B]
По умолчанию A = runs/judges-60/out-02-07 (Opus), B = runs/judges-60/codex-02-07 (Codex, контрольная разметка).
Сравниваются только чаты, которые есть в обеих папках.
"""
import collections, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(d):
    out = {}
    for p in sorted(pathlib.Path(d).glob("*.json")):
        for ch in json.loads(p.read_text())["chats"]:
            out[ch["id"]] = {str(x["id"]).zfill(2): x for x in ch["criteria"]}
    return out


def main():
    a_dir = sys.argv[1] if len(sys.argv) > 1 else ROOT / "runs/judges-60/out-02-07"
    b_dir = sys.argv[2] if len(sys.argv) > 2 else ROOT / "runs/judges-60/codex-02-07"
    a, b = load(a_dir), load(b_dir)
    common = sorted(set(a) & set(b))
    if not common:
        raise SystemExit("нет общих чатов")
    for k in ("02", "07"):
        same = sum(a[c][k]["answer"] == b[c][k]["answer"] for c in common)
        pairs = collections.Counter((a[c][k]["answer"], b[c][k]["answer"]) for c in common if a[c][k]["answer"] != b[c][k]["answer"])
        print(f"{k}: совпало {same}/{len(common)} ({100 * same / len(common):.1f}%); расхождения A→B {dict(pairs)}")
        for c in common:
            if a[c][k]["answer"] != b[c][k]["answer"]:
                print(f"   {c}: A {a[c][k]['answer']}{' (сомн.)' if a[c][k].get('doubt') else ''} · "
                      f"B {b[c][k]['answer']}{' (сомн.)' if b[c][k].get('doubt') else ''} — B: {b[c][k].get('explanation', '')[:140]}")


if __name__ == "__main__":
    main()
