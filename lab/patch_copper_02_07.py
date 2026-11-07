"""Медный эталон v2: критерии 02 и 07 переразмечены по уточнённым правилам (40 чатов — Opus xhigh,
первые 20 — Codex gpt-6.1-sol high; согласие Codex с Opus проверено на тех 40, см. lab/compare_02_07.py).

Почему: 02 — ответ организаторов в чате (Максим №2814: «если данных изначально достаточно — N/A»);
07 — в v1 пачка A размечена мягче остальных, теперь одно правило: (итог или предложение помощи) И прощание.
Вход: runs/judges-60/out-02-07/*.json (задание — как в lab/judge_prompt.md, только 02 и 07).
Выход: lab/reference_copper.json (version copper-v2); прежняя версия сохраняется в lab/reference_copper_v1.json.
Запуск: .venv/bin/python lab/patch_copper_02_07.py
"""
import collections, json, pathlib, shutil, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = ROOT / "lab" / "reference_copper.json"
V1 = ROOT / "lab" / "reference_copper_v1.json"
OUT = ROOT / "runs" / "judges-60" / "out-02-07"


def main():
    if not V1.exists():
        shutil.copy(REF, V1)
    ref = json.loads(V1.read_text())
    new, judge = {}, {}
    for p in sorted(OUT.glob("*.json")):
        d = json.loads(p.read_text())
        for ch in d["chats"]:
            new[ch["id"]] = {str(x["id"]).zfill(2): x for x in ch["criteria"]}
            judge[ch["id"]] = d.get("judge", p.stem)
    missing = sorted(set(ref["chats"]) - set(new))
    if missing:
        raise SystemExit(f"нет новой разметки для {len(missing)} чатов: {missing[:5]}…")
    changed = collections.Counter()
    for cid, crit in ref["chats"].items():
        for k in ("02", "07"):
            x = new[cid][k]
            a = str(x["answer"]).strip()
            if a not in ("Да", "Нет", "N/A"):
                raise SystemExit(f"{cid} {k}: недопустимый ответ {a!r}")
            if a != crit[k]["answer"]:
                changed[k] += 1
            crit[k] = {"answer": a, "source": judge[cid], "agree": "1/1", "doubt": bool(x.get("doubt")),
                       "explanation": x.get("explanation", "")}
    ref["version"], ref["built"] = "copper-v2", time.strftime("%Y-%m-%d %H:%M")
    REF.write_text(json.dumps(ref, ensure_ascii=False, indent=1))
    dist = {k: collections.Counter(ref["chats"][c][k]["answer"] for c in ref["chats"]) for k in ("02", "07")}
    print(f"copper-v2: изменено 02 — {changed['02']}, 07 — {changed['07']} из {len(ref['chats'])}; распределение {dist}; "
          f"судьи {dict(collections.Counter(judge[c] for c in ref['chats']))}")


if __name__ == "__main__":
    main()
