"""Попытка 4 своей модели: учитель — продукт целиком, плюс тонкие места DEV.

Прошлый адаптер учился у Claude Sonnet на старом чек-листе без прецедентов и на одной синтетике; на незнакомых
чатах DEV он близок к Haiku по медному эталону (87,5% против 91,1%), но отстаёт по замыслу авторов (23 против 32
из 35): пограничных случаев DEV (04, 06, 07, 02) в синтетике почти нет. Здесь:
  label — те же синтетические чаты размечает связка, которая сдаёт results.json: Haiku 5.5, действующий чек-лист,
          2 прецедента, голосование 3 оценок. 30% — варианты чек-листа (замена критерия, веса, без пояснений),
          чтобы ученик по-прежнему читал чек-лист. Пул прецедентов — без 21 отложенного чата DEV.
  build — обучающие примеры: синтетика (согласие с планом генератора, чаты с 0–1 нарушением дважды, с 5+ — без
          них) + 39 обучающих чатов DEV с утверждёнными ответами (copies раз). Отложенные 21 чат DEV и второй DEV
          (lab/sim) в обучение не идут — на них замер. --prec: в промпт ученика тоже идут 2 прецедента.

  .venv/bin/python lab/local/relabel.py label --conc 24 --budget 6     # → data/synth_labels_r4.jsonl
  .venv/bin/python lab/local/relabel.py build --name r4 --copies 3 [--prec]   # → data/train_<name>.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lab" / "local"))

import synth  # noqa: E402
from cqa import checklist as ck  # noqa: E402
from cqa import precedents  # noqa: E402
from cqa.db import DB  # noqa: E402
from cqa.judge import get_provider  # noqa: E402
from cqa.judge.prompt import build_system, build_user  # noqa: E402
from cqa.pii import mask_transcript  # noqa: E402
from cqa.vote import evaluate_voted  # noqa: E402

DATA = ROOT / "lab" / "local" / "data"
LABELS = DATA / "synth_labels_r4.jsonl"
HOLDOUT = set(json.loads((ROOT / "lab" / "holdout.json").read_text())["holdout"])
TRAIN_DEV = sorted(json.loads((ROOT / "lab" / "holdout.json").read_text())["train"])
CODES = {"contact": "01", "discovery": "02", "identification": "03", "consultation": "04", "empathy": "05",
         "dialogue": "06", "closure": "07", "critical": "08"}


def setup_precedents() -> DB:
    """Копия базы продукта (транскрипты DEV, решения проверяющих) и пул прецедентов без отложенных чатов."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="relabel-"))
    src = sqlite3.connect(ROOT / "var" / "cqa.db")
    dst = sqlite3.connect(tmp / "cqa.db")
    src.backup(dst)
    src.close()
    dst.close()
    ref = json.loads(precedents.REFERENCE.read_text(encoding="utf-8"))
    ref["dialogs"] = {cid: v for cid, v in ref["dialogs"].items() if cid not in HOLDOUT}
    (tmp / "reference.json").write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
    precedents.REFERENCE = tmp / "reference.json"
    return DB(tmp / "cqa.db")


def record_of(rec: dict) -> dict:
    return {k: rec[k] for k in ("conversation_id", "channel", "language", "transcript")}


async def cmd_label(args):
    budget = synth.Budget(args.budget)
    base = ck.load_default()
    db = setup_precedents()
    chats = synth.read_jsonl(synth.CHATS)[: args.limit or None]
    done = {r["conversation_id"] for r in synth.read_jsonl(LABELS)} if LABELS.exists() else set()
    rng = random.Random(args.seed)
    jobs = []
    for rec in chats:
        cfg, tag = (synth.make_variant(base, rng) if rng.random() < args.variant_share else (base, "default"))
        if rec["conversation_id"] not in done:
            jobs.append((rec, cfg, tag))
    prov = get_provider("haiku")
    sem, lock, n_ok, n_bad = asyncio.Semaphore(args.conc), asyncio.Lock(), [0], [0]
    print(f"разметка учителем-продуктом (Haiku ×3 + прецеденты): {len(jobs)} чатов (уже есть {len(done)}), "
          f"потрачено ${budget.spent:.3f} из ${budget.cap}", flush=True)

    async def one(rec, cfg, tag):
        if not budget.ok():
            return
        examples = precedents.select(db, record_of(rec), cfg)
        res = await evaluate_voted(record_of(rec), cfg, prov, 3, sem=sem, precedents=examples)
        await budget.add(res.get("cost_usd") or 0.0, "label-haiku-r4")
        if res["status"] != "ok" or len(res["answers"]) != len(cfg["criteria"]):
            n_bad[0] += 1
            print(f"  {rec['conversation_id']}: {(res.get('error') or 'неполный ответ')[:150]}", flush=True)
            return
        answers = {k: {"answer": a["answer"], "explanation": a.get("explanation") or "",
                       "evidence": a.get("evidence") or [], "agreement": a.get("agreement")}
                   for k, a in res["answers"].items()}
        out = {"conversation_id": rec["conversation_id"], "teacher": "claude-haiku-5-5 ×3 + прецеденты",
               "variant": tag, "checklist": cfg, "answers": answers, "cost_usd": res.get("cost_usd"),
               "pii_masked": res.get("pii_masked"), "precedents": [e["conversation_id"] for e in examples]}
        async with lock:
            with LABELS.open("a") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
            n_ok[0] += 1
            if n_ok[0] % 50 == 0:
                print(f"  {n_ok[0]} меток, ${budget.spent:.2f}", flush=True)

    t0 = time.monotonic()
    await asyncio.gather(*[one(*j) for j in jobs])
    print(f"готово: +{n_ok[0]} меток, сбоев {n_bad[0]}, {time.monotonic() - t0:.0f} с; всего потрачено ${budget.spent:.3f}")


def violations(rec: dict) -> int:
    """Сколько нарушений заложено планом генератора (ответ ниже верхней ступени)."""
    top = {"Да", "N/A", "20", "10"}
    return sum(1 for cid, key in rec["plan"].items()
               if (synth.expected(cid, key) not in top) != (cid == "08"))


def dev_answers(cfg: dict) -> dict[str, dict]:
    """Утверждённые ответы 39 обучающих чатов DEV: ответ продукта, если он совпал с медным эталоном (у него есть
    реплики-доказательства), иначе ответ и объяснение эталона с репликами из ответа продукта."""
    copper = json.loads((ROOT / "lab" / "reference_copper.json").read_text())["chats"]
    prod = {r["conversation_id"]: r for r in json.loads((ROOT / "results" / "dev" / "results.json").read_text())["results"]}
    val = lambda v: "N/A" if v is None else ("Да" if v is True else "Нет" if v is False else str(v))  # noqa: E731
    out = {}
    for cid in TRAIN_DEV:
        p = {CODES[c["criterion_id"]]: c for c in prod[cid]["criteria"]}
        ans = {}
        for c in cfg["criteria"]:
            k, ref = c["id"], copper[cid][c["id"]]
            mine = p[k]
            same = val(mine["value"]) == ref["answer"]
            ans[k] = {"answer": ref["answer"], "explanation": mine["rationale"] if same else ref["explanation"],
                      "evidence": mine["evidence_turns"]}
        out[cid] = ans
    return out


def cmd_build(args):
    base = ck.load_default()
    db = setup_precedents() if args.prec else None
    chats = {r["conversation_id"]: r for r in synth.read_jsonl(synth.CHATS)}
    dev = {r["conversation_id"]: r for r in json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]}
    rows, dropped, heavy = [], 0, 0

    def add(rec: dict, cfg: dict, answers: dict, tag: str, times: int):
        masked, _ = mask_transcript(rec["transcript"])
        user = build_user(rec, masked)
        if db is not None:
            ex = precedents.select(db, record_of(rec), cfg)
            ex = [{**e, "transcript": mask_transcript(e["transcript"])[0]} for e in ex]
            user = precedents.block(ex, cfg) + user
        prompt = synth.render("qwen35", build_system(cfg), user)
        row = {"id": rec["conversation_id"], "lang": rec["language"], "variant": tag, "prompt": prompt,
               "completion": synth.canonical_json(cfg, answers) + "<|im_end|>"}
        rows.extend([row] * times)

    for lab in synth.read_jsonl(LABELS):
        rec = chats[lab["conversation_id"]]
        agree, total, miss = synth.plan_agreement(rec, lab)
        if total and total - agree > args.max_miss:
            dropped += 1
            continue
        n = violations(rec)
        if n >= 5:
            heavy += 1
            continue
        add(rec, lab["checklist"], lab["answers"], lab["variant"], 2 if n <= 1 else 1)
    n_syn = len(rows)
    for cid, ans in dev_answers(base).items():
        add(dev[cid], base, ans, "dev", args.copies)
    random.Random(5).shuffle(rows)
    out = DATA / f"train_{args.name}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    chars = sorted(len(r["prompt"]) + len(r["completion"]) for r in rows)
    print(f"примеров: {len(rows)} — синтетика {n_syn} (отброшено по несогласию с планом {dropped}, с 5+ нарушениями "
          f"{heavy}), DEV {len(rows) - n_syn} ({len(TRAIN_DEV)} чатов × {args.copies}); прецеденты в промпте: "
          f"{'да' if args.prec else 'нет'}; длина, символов: медиана {chars[len(chars) // 2]}, макс {chars[-1]} "
          f"→ {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lb = sub.add_parser("label")
    lb.add_argument("--conc", type=int, default=24, help="одновременных вызовов модели")
    lb.add_argument("--limit", type=int, default=0)
    lb.add_argument("--variant-share", type=float, default=0.3)
    lb.add_argument("--seed", type=int, default=11)
    lb.add_argument("--budget", type=float, default=6.0, help="потолок трат на разметку, $ (общий счётчик spend.json)")
    b = sub.add_parser("build")
    b.add_argument("--name", default="r4")
    b.add_argument("--copies", type=int, default=3, help="сколько раз повторить 39 обучающих чатов DEV")
    b.add_argument("--max-miss", type=int, default=2)
    b.add_argument("--prec", action="store_true", help="2 прецедента и в промпте ученика")
    args = ap.parse_args()
    if args.cmd == "label":
        asyncio.run(cmd_label(args))
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
