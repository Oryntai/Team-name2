"""Проба Claude Sonnet 5.5 / Haiku 5.5 на чатах DEV (официальный SDK anthropic).

Запуск:
  .venv/bin/python lab/probe_claude.py                          # обе модели, 5 контрольных чатов
  .venv/bin/python lab/probe_claude.py --models haiku --set chats  # Haiku на всех 60 чатах
Промпт, чек-лист и разбор ответа — те же, что в lab/probe.py. Ключ — ANTHROPIC_API_KEY из .env.
Платит предоплаченный баланс ключа владельца; запускать только по его слову.
Звонки (реальные данные) не отправляем, пока организаторы не ответят.
"""
import argparse, asyncio, collections, importlib.util, json, pathlib, time

import anthropic

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
_spec = importlib.util.spec_from_file_location("probe", ROOT / "lab" / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# $ за 1М токенов: вход, выход (справка claude-api, кеш 2026-10-06)
MODELS = {"sonnet": ("claude-sonnet-5-5", 2.00, 10.00), "haiku": ("claude-haiku-5-5", 0.10, 0.50)}
DEFAULT_CHATS = "p1-kz-008,p1-en-014,p1-ru-022,p1-ru-025,p1-ru-018"
ANSWERS = ["Да", "Нет", "N/A", "0", "4", "5", "7", "10", "15", "20"]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["criteria"], "properties": {"criteria": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["id", "answer", "evidence", "explanation"],
                               "properties": {"id": {"type": "string"}, "answer": {"type": "string", "enum": ANSWERS},
                                              "evidence": {"type": "array", "items": {"type": "integer"}},
                                              "explanation": {"type": "string"}}}}}}


def env():
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def ask(client, sem, tag, rec, sysmsg, effort):
    model = MODELS[tag][0]
    kw = dict(model=model, max_tokens=16000, system=sysmsg,
              messages=[{"role": "user", "content": probe.build_user(rec)}],
              output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}})
    async with sem:
        t0 = time.monotonic()
        try:
            if tag == "sonnet":  # серверная страховка на случай отказа модели
                r = await client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            else:  # у Haiku 5.5 серверной страховки нет
                r = await client.messages.create(**kw)
        except anthropic.APIStatusError as e:
            return {"error": f"{e.status_code}: {str(e.message)[:300]}", "latency": time.monotonic() - t0}
        except anthropic.APIConnectionError as e:
            return {"error": f"connection: {e}", "latency": time.monotonic() - t0}
    text = next((b.text for b in r.content if b.type == "text"), "")
    fell_back = any(getattr(it, "type", "") == "fallback_message" for it in (getattr(r.usage, "iterations", None) or []))
    return {"text": text, "latency": round(time.monotonic() - t0, 2), "stop": r.stop_reason, "served_by": r.model,
            "fallback": fell_back, "in_tok": r.usage.input_tokens, "out_tok": r.usage.output_tokens}


def references():
    """Ответы Kimi K3.1 Pro и DeepSeek V4.1 Flash из прогона всех 60 чатов (для сравнения, это не эталон)."""
    refs = {}
    for run in sorted(RUNS.glob("2026*/results.json")):
        data = json.loads(run.read_text())
        for m in data.get("results", []):
            if m.get("tag") in ("kimi", "ds") and len(m["rows"]) == 60:
                refs[m["tag"]] = {row["id"]: row for row in m["rows"]}
    return refs


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="sonnet,haiku")
    ap.add_argument("--chats", default=DEFAULT_CHATS)
    ap.add_argument("--set", choices=["list", "chats"], default="list", help="chats — все 60 синтетических чатов")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--conc", type=int, default=20)
    args = ap.parse_args()
    tags = args.models.split(",")
    key = env().get("ANTHROPIC_API_KEY")
    # base_url задаём явно: в окружении Claude Code стоит ANTHROPIC_BASE_URL
    client = anthropic.AsyncAnthropic(api_key=key, base_url="https://api.anthropic.com", timeout=300, max_retries=4)
    try:  # бесплатная проверка ключа до любых платных запросов
        ids = [m.id async for m in client.models.list()]
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Ключ не принят API ({e.status_code}): {str(e.message)[:200]}")
    missing = [MODELS[t][0] for t in tags if MODELS[t][0] not in ids]
    if missing:
        raise SystemExit(f"Модели недоступны этому ключу: {missing}")
    cfg = json.loads((ROOT / "lab" / "checklist_v1.json").read_text())
    sysmsg = probe.build_system(cfg)
    recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]}
    want = (sorted(c for c, r in recs.items() if r["channel"] == "agent chat") if args.set == "chats"
            else args.chats.split(","))
    jobs = [(tag, cid) for tag in tags for cid in want]
    sem = asyncio.Semaphore(args.conc)
    t0 = time.monotonic()
    res = await asyncio.gather(*[ask(client, sem, tag, recs[cid], sysmsg, args.effort) for tag, cid in jobs])
    wall = time.monotonic() - t0
    refs = references()
    rows = []
    for (tag, cid), o in zip(jobs, res):
        js = probe.parse_json(o.get("text"))
        crit = {str(x.get("id")).zfill(2): x for x in (js or {}).get("criteria", []) if isinstance(x, dict)}
        ans = {c["id"]: probe.norm_answer(crit.get(c["id"], {}).get("answer"), c) for c in cfg["criteria"]}
        rows.append({"tag": tag, "id": cid, **{k: v for k, v in o.items() if k != "text"}, "answers": ans,
                     "total": probe.total(ans, cfg), "raw": crit})
    print(f"Чатов: {len(want)}; модели: {', '.join(tags)}; effort={args.effort}; параллельно до {args.conc}; "
          f"все запросы за {wall:.1f} с\n")
    crit_ids = [c["id"] for c in cfg["criteria"]]
    for tag in tags:
        rr = [r for r in rows if r["tag"] == tag]
        p_in, p_out = MODELS[tag][1:]
        cost = sum((r.get("in_tok") or 0) * p_in + (r.get("out_tok") or 0) * p_out for r in rr) / 1e6
        checks = [(r["id"], k, r["answers"].get(k), exp) for r in rr for k, exp in probe.PLANTED.get(r["id"], {}).items()]
        ok = sum(got in exp for _, _, got, exp in checks)
        lat = sorted(r["latency"] for r in rr if r.get("latency"))
        print(f"== {tag} ({MODELS[tag][0]}): заложенные {ok}/{len(checks)}; задержка медиана {lat[len(lat)//2]:.1f} с, "
              f"макс {lat[-1]:.1f} с; токены вход/выход {sum(r.get('in_tok') or 0 for r in rr)}/"
              f"{sum(r.get('out_tok') or 0 for r in rr)}; ~${cost:.4f}; ошибок {sum(1 for r in rr if r.get('error'))}; "
              f"отказов {sum(r.get('stop') == 'refusal' for r in rr)}; обрезано {sum(r.get('stop') == 'max_tokens' for r in rr)}; "
              f"недопустимых ответов {sum(v is None for r in rr for v in r['answers'].values())}; "
              f"страховка сработала {sum(bool(r.get('fallback')) for r in rr)}")
        for cid, k, got, exp in checks:
            if got not in exp:
                expl = (next(r for r in rr if r["id"] == cid)["raw"].get(k) or {}).get("explanation", "")
                print(f"   мимо заложенного: {cid} кр.{k}: {got!r}, ждали {sorted(map(str, exp))} — {str(expl)[:150]}")
        for r in rr:
            if r.get("error"):
                print(f"   ошибка {r['id']}: {r['error'][:200]}")
        for ref, rmap in refs.items():
            both = [r for r in rr if r["id"] in rmap]
            if not both:
                continue
            same = sum(r["answers"][k] == rmap[r["id"]]["answers"][k] for r in both for k in crit_ids)
            per_c = {k: sum(r["answers"][k] == rmap[r["id"]]["answers"][k] for r in both) for k in crit_ids}
            per_l = collections.defaultdict(lambda: [0, 0])
            for r in both:
                lang = r["id"].split("-")[1]
                for k in crit_ids:
                    per_l[lang][0] += r["answers"][k] == rmap[r["id"]]["answers"][k]
                    per_l[lang][1] += 1
            diff = [abs((r["total"] or 0) - (rmap[r["id"]]["total"] or 0)) for r in both]
            print(f"   против {ref}: совпало {same}/{len(both) * len(crit_ids)} = {same / (len(both) * len(crit_ids)):.1%}; "
                  f"итог расходится в среднем на {sum(diff) / len(diff):.1f} балла (>10 — в {sum(d > 10 for d in diff)} чатах)")
            print("     по критериям: " + ", ".join(f"{k} {v}/{len(both)}" for k, v in per_c.items()))
            print("     по языкам: " + ", ".join(f"{l} {a / b:.0%}" for l, (a, b) in sorted(per_l.items())))
            if ref == "kimi":
                for k in crit_ids:
                    d = [f"{r['id'][3:]} {r['answers'][k]}/{rmap[r['id']]['answers'][k]}" for r in both
                         if r["answers"][k] != rmap[r["id"]]["answers"][k]]
                    if d:
                        print(f"     {k} ({tag}/kimi): " + "; ".join(d))
    out = RUNS / (time.strftime("%Y%m%d-%H%M%S") + "-claude")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"args": vars(args), "wall": wall, "rows": rows}, ensure_ascii=False, indent=1))
    print(f"\nПодробности: {out.relative_to(ROOT)}/results.json")


if __name__ == "__main__":
    asyncio.run(main())
