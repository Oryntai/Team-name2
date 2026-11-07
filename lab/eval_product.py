"""Замер судьи ПРОДУКТА (cqa.judge, тот же код, что в сервере) против медного эталона DEV.

Запуск: .venv/bin/python lab/eval_product.py --provider haiku --split train   # 39 чатов, с разбором расхождений
        .venv/bin/python lab/eval_product.py --provider haiku --split holdout # 21 отложенный: только сводка
        .venv/bin/python lab/eval_product.py --provider haiku --split all
Платно (ключ владельца): Haiku ≈ $0,001 за чат, Sonnet ≈ $0,012. Сплит — lab/holdout.json: на отложенных не подгоняем.
Эталон — lab/reference_copper.json (мнение судей Opus, не эталон организаторов; 07 в пачке A мягче — смотреть отдельно).
Результат — runs/<время>-product-<provider>-<split>/results.json.
"""
import argparse, asyncio, collections, importlib.util, json, pathlib, statistics, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cqa import checklist as ck  # noqa: E402
from cqa.judge import evaluate, get_provider  # noqa: E402
from cqa.scoring import score  # noqa: E402

_spec = importlib.util.spec_from_file_location("probe", ROOT / "lab" / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def agreement(rows, ref, ids, crit_ids):
    same = sum(rows[c][k] == ref[c][k]["answer"] for c in ids for k in crit_ids)
    return same, len(ids) * len(crit_ids)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="haiku")
    ap.add_argument("--split", choices=["train", "holdout", "all"], default="train")
    ap.add_argument("--conc", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="первые N чатов (для проверки пути)")
    ap.add_argument("--checklist", default="", help="путь к чек-листу (по умолчанию — начальный из cqa/checklists)")
    ap.add_argument("--tag", default="", help="метка прогона в имени папки")
    args = ap.parse_args()

    ref = json.loads((ROOT / "lab" / "reference_copper.json").read_text())["chats"]
    split = json.loads((ROOT / "lab" / "holdout.json").read_text())
    ids = {"train": split["train"], "holdout": split["holdout"], "all": split["train"] + split["holdout"]}[args.split]
    ids = sorted(ids)[: args.limit or None]
    recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]}
    cfg = ck.normalize(json.loads(pathlib.Path(args.checklist).read_text())) if args.checklist else ck.load_default()
    crit_ids = [c["id"] for c in cfg["criteria"]]
    prov = get_provider(args.provider)
    sem = asyncio.Semaphore(args.conc)

    async def one(cid):
        async with sem:
            return await evaluate(recs[cid], cfg, prov)

    t0 = time.monotonic()
    res = dict(zip(ids, await asyncio.gather(*[one(c) for c in ids])))
    wall = time.monotonic() - t0
    ok = [c for c in ids if res[c]["status"] == "ok"]
    errors = {c: res[c]["error"] for c in ids if res[c]["status"] != "ok"}
    ours = {c: {k: res[c]["answers"][k]["answer"] for k in crit_ids} for c in ok}
    base = {c: {"01": "Да", "02": "Да", "03": "Да", "04": "20", "05": "Да", "06": "10", "07": "Да", "08": "Нет"} for c in ok}
    refa = {c: {k: ref[c][k]["answer"] for k in crit_ids} for c in ok}

    cost = sum(r["cost_usd"] for r in res.values())
    lat = sorted(r["latency_s"] for r in res.values() if r.get("latency_s"))
    print(f"Судья продукта: {args.provider}, сплит {args.split}: {len(ok)}/{len(ids)} чатов оценено за {wall:.1f} с; "
          f"задержка медиана {statistics.median(lat):.1f} с, макс {lat[-1]:.1f} с; ${cost:.4f} "
          f"(≈ ${cost / max(len(ids), 1):.5f} за чат); ошибок {len(errors)}")
    for c, e in errors.items():
        print(f"  ошибка {c}: {e[:200]}")

    no07 = [k for k in crit_ids if k != "07"]
    for name, ans in (("продукт", ours), ("база «всё выполнено»", base)):
        s, n = agreement(ans, ref, ok, crit_ids)
        s7, n7 = agreement(ans, ref, ok, no07)
        print(f"\n{name}: совпадение с медным эталоном {s}/{n} = {s / n:.1%} (без 07: {s7 / n7:.1%})")
        print("  по критериям: " + ", ".join(f"{k} {sum(ans[c][k] == refa[c][k] for c in ok)}/{len(ok)}" for k in crit_ids))
        per_l = collections.defaultdict(lambda: [0, 0])
        for c in ok:
            for k in crit_ids:
                per_l[c.split("-")[1]][0] += ans[c][k] == refa[c][k]
                per_l[c.split("-")[1]][1] += 1
        print("  по языкам: " + ", ".join(f"{l} {a / b:.0%}" for l, (a, b) in sorted(per_l.items())))
        diffs = [abs((score(cfg, ans[c])["total"] or 0) - (score(cfg, refa[c])["total"] or 0)) for c in ok]
        gross_ok = sum((ans[c]["08"] == "Да") == (refa[c]["08"] == "Да") for c in ok)
        print(f"  итог расходится в среднем на {statistics.mean(diffs):.1f} балла; в пределах ±3 — {sum(d <= 3 for d in diffs)}"
              f"/{len(ok)}; >10 — {sum(d > 10 for d in diffs)}; флаг грубой ошибки совпал {gross_ok}/{len(ok)}")

    checks = [(c, k, ours[c][k], {str(x) for x in exp}) for c, d in probe.PLANTED.items() if c in ours for k, exp in d.items()]
    print(f"\nзаложенные нарушения (в этом сплите): {sum(a in e for *_, a, e in checks)}/{len(checks)}")
    for c, k, a, e in checks:
        if a not in e:
            print(f"  мимо: {c} {k}: {a}, ждали {sorted(e)} — {res[c]['answers'][k]['explanation'][:160]}")

    if args.split == "train":  # разбор расхождений — только на обучающей части
        print("\nрасхождения (наш / эталон):")
        for k in crit_ids:
            d = [f"{c[3:]} {ours[c][k]}/{refa[c][k]}" for c in ok if ours[c][k] != refa[c][k]]
            if d:
                print(f"  {k}: " + "; ".join(d))

    out = ROOT / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-product-{args.provider}-{args.split}{"-" + args.tag if args.tag else ""}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"args": vars(args), "wall": wall, "cost": cost, "results": res},
                                                 ensure_ascii=False, indent=1))
    print(f"\nПодробности: {out.relative_to(ROOT)}/results.json")


if __name__ == "__main__":
    asyncio.run(main())
