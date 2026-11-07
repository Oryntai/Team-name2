"""Проба моделей на DEV: один запрос на диалог (все критерии сразу), запросы параллельно.

Запуск (из корня проекта):
  .venv/bin/python lab/probe.py --models kimi,ds,glm --set planted
  .venv/bin/python lab/probe.py --models ds --set chats --conc 32

Наборы: planted — 12 чатов с заведомо известными нарушениями; chats — все 60 чатов.
Звонки (реальные данные) во внешние модели не отправляем, пока организаторы не ответят.
Ответы кэшируются в runs/cache: повтор без изменений не тратит ни времени, ни денег.
"""
import argparse, asyncio, hashlib, json, os, pathlib, random, re, time
from decimal import Decimal, ROUND_HALF_UP

import openai
from openai import AsyncOpenAI

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

# имя эндпоинта Modal, адрес, цена $ за 1М токенов: вход, выход (modal billing rates, 7.11)
ENDPOINTS = {k: v for k, v in json.loads((ROOT / "lab" / "endpoints.json").read_text()).items() if not k.startswith("_")}

# Заложенные нарушения, в которых уверены (прочитано глазами 7.11). Не полный эталон.
PLANTED = {
    "p1-en-015": {"01": {"Нет"}},
    "p1-kz-008": {"01": {"Нет"}, "02": {"Нет"}},
    "p1-ru-014": {"01": {"Нет"}},
    "p1-en-014": {"03": {"Нет"}},
    "p1-ru-022": {"08": {"Да"}},
    "p1-kz-013": {"08": {"Да"}},
    "p1-ru-029": {"08": {"Да"}, "05": {"Нет"}},
    "p1-ru-025": {"04": {"N/A"}},
    "p1-ru-018": {"07": {"Нет"}, "04": {0, 5}},
    "p1-ru-010": {"03": {"Да"}},
    "p1-en-006": {"03": {"Да"}},
    "p1-ru-001": {"01": {"Да"}, "03": {"N/A"}, "08": {"Нет"}},
}


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def build_system(cfg):
    lines = ["Ты — специалист по контролю качества контакт-центра Freedom Broker. "
             "Оцени работу ОПЕРАТОРА в диалоге строго по чек-листу.", "", "КРИТЕРИИ:"]
    for c in cfg["criteria"]:
        if c["type"] == "scale":
            kind = "/".join(sorted(c["steps"], key=int)) + (" или N/A" if c["na"] else "")
        else:
            kind = "Да/Нет" + ("/N/A" if c["na"] else "")
        s = f'{c["id"]} {c["title"]} [{kind}]: '
        s += ("Да — " + c["yes"]) if c["type"] != "scale" else c["yes"][0].upper() + c["yes"][1:]
        if c["type"] == "scale":
            s += " " + "; ".join(f"{k} — {v}" for k, v in sorted(c["steps"].items(), key=lambda kv: -int(kv[0])))
        if c.get("no"):
            s += " Нет — " + c["no"]
        if c["na"]:
            s += " N/A — " + c["na_when"]
        lines.append(s)
    lines += ["", "ПРАВИЛА:"] + [f"- {r}" for r in cfg["rules"]]
    lines += ["", "Реплики пронумерованы: [номер]. В evidence укажи номера реплик или системных сообщений, "
              "которые подтверждают вывод; если действие пропущено — номера участка, где оно ожидалось.",
              "Ответь ТОЛЬКО JSON без пояснений вокруг:",
              '{"criteria":[{"id":"01","answer":"Да","evidence":[9],"explanation":"кратко по-русски"}, '
              '... все критерии по порядку ...]}',
              'answer — строка: "Да", "Нет", "N/A" или ступень шкалы цифрами, например "15".']
    return "\n".join(lines)


def build_user(rec):
    head = f'Диалог {rec["conversation_id"]}, канал: {rec["channel"]}, язык: {rec["language"]}.'
    body = "\n".join(f'[{i}] {t["timestamp"]} {t["speaker"]}: {t["text"]}' for i, t in enumerate(rec["transcript"]))
    return head + "\n\n" + body


SCHEMA = {"type": "object", "required": ["criteria"], "properties": {"criteria": {"type": "array", "items": {
    "type": "object", "required": ["id", "answer", "evidence", "explanation"],
    "properties": {"id": {"type": "string"}, "answer": {"type": "string"},
                   "evidence": {"type": "array", "items": {"type": "integer"}}, "explanation": {"type": "string"}}}}}}
RF = {"schema": {"type": "json_schema", "json_schema": {"name": "cqa", "schema": SCHEMA}},
      "json": {"type": "json_object"}, "none": None}


def parse_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    for s in (m.group(0), re.sub(r"\]\s*,\s*\[", ",", m.group(0))):  # вторая попытка чинит "[4],[9]"
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return None


def norm_answer(a, c):
    """Приводит ответ модели к виду чек-листа; None — недопустимый ответ."""
    if isinstance(a, str):
        s = a.strip().lower().replace(" ", "")
        if s in ("n/a", "na", "н/д", "неприменимо"):
            return "N/A" if c["na"] else None
        if s in ("да", "yes", "иә"):
            return "Да" if c["type"] != "scale" else None
        if s in ("нет", "no", "жоқ"):
            return "Нет" if c["type"] != "scale" else None
        if re.fullmatch(r"\d+", s):
            a = int(s)
    if isinstance(a, (int, float)) and c["type"] == "scale":
        return int(a) if str(int(a)) in c["steps"] else None
    return None


def total(answers, cfg):
    """Итог по правилам чек-листа: N/A исключаем, вес перераспределяем, округляем до 0.01."""
    num, den = Decimal(0), Decimal(0)
    for c in cfg["criteria"]:
        a = answers.get(c["id"])
        if not c["weight"] or a in (None, "N/A"):
            continue
        w = Decimal(c["weight"])
        if c["type"] == "bool":
            pts = w if a == "Да" else Decimal(0)
        else:
            pts = w * Decimal(a) / Decimal(max(int(k) for k in c["steps"]))
        num, den = num + pts, den + w
    return None if den == 0 else float((num / den * 100).quantize(Decimal("0.01"), ROUND_HALF_UP))


async def ask(client, model, sysmsg, usermsg, sem, nocache, max_tokens, rf):
    key = hashlib.sha256(json.dumps([model, sysmsg, usermsg, 0, max_tokens, rf], ensure_ascii=False).encode()).hexdigest()[:24]
    cpath = RUNS / "cache" / f"{key}.json"
    if cpath.exists() and not nocache:
        return json.loads(cpath.read_text()) | {"cached": True}
    async with sem:
        t0 = time.monotonic()
        extra, n429 = ({"response_format": RF[rf]} if RF[rf] else {}), 0
        for attempt in range(8):
            try:
                r = await client.chat.completions.create(
                    model=model, temperature=0, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}], **extra)
                break
            except openai.RateLimitError:
                n429 += 1
                await asyncio.sleep(min(30, 2 ** attempt) + random.random())
            except openai.BadRequestError as e:
                if extra:  # сервер не принял response_format — повторяем без него
                    extra, rf = {}, f"{rf}->none"
                    continue
                return {"error": f"BadRequest: {str(e)[:300]}", "latency": time.monotonic() - t0}
            except Exception as e:  # noqa: BLE001 — сохраняем любую ошибку как результат
                return {"error": f"{type(e).__name__}: {str(e)[:300]}", "latency": time.monotonic() - t0}
        else:
            return {"error": "rate limit: retries exhausted", "latency": time.monotonic() - t0, "n429": n429}
        u, msg = r.usage, r.choices[0].message
        text = msg.content or ""
        if not text.strip():  # DeepSeek в режиме схемы кладёт JSON в reasoning_content
            rc = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning_content")
            if rc and parse_json(rc) is not None:
                text = rc
        det, pdet = getattr(u, "completion_tokens_details", None), getattr(u, "prompt_tokens_details", None)
        out = {"text": text, "latency": round(time.monotonic() - t0, 2), "rf": rf, "n429": n429,
               "finish": r.choices[0].finish_reason,
               "in_tok": getattr(u, "prompt_tokens", 0) or 0, "out_tok": getattr(u, "completion_tokens", 0) or 0,
               "cached_tok": (getattr(pdet, "cached_tokens", 0) or 0) if pdet else 0,
               "reason_tok": (getattr(det, "reasoning_tokens", 0) or 0) if det else 0}
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cpath.write_text(json.dumps(out, ensure_ascii=False))
    return out | {"cached": False}


async def run_model(tag, recs, cfg, sysmsg, conc, nocache, max_tokens, rf):
    ep = ENDPOINTS[tag]
    base = ep["url"]
    p_in, p_cached, p_out = ep.get("price") or (0, 0, 0)
    client = AsyncOpenAI(base_url=base + "/v1", api_key=os.environ["MODAL_PROXY_TOKEN"], timeout=600, max_retries=0)
    model = (await client.models.list()).data[0].id
    sem = asyncio.Semaphore(conc)
    t0 = time.monotonic()
    res = await asyncio.gather(*[ask(client, model, sysmsg, build_user(r), sem, nocache, max_tokens, rf) for r in recs])
    wall = time.monotonic() - t0
    rows = []
    for rec, o in zip(recs, res):
        row = {"id": rec["conversation_id"], "model": model, "text": (o.get("text") or "")[:4000],
               **{k: o.get(k) for k in ("latency", "in_tok", "cached_tok", "out_tok", "reason_tok", "cached", "error", "rf",
                                        "n429", "finish")}}
        js = parse_json(o.get("text"))
        crit = {str(x.get("id")).zfill(2): x for x in (js or {}).get("criteria", []) if isinstance(x, dict)}
        ans = {c["id"]: norm_answer(crit.get(c["id"], {}).get("answer"), c) for c in cfg["criteria"]}
        row.update(json_ok=js is not None, answers=ans, invalid=[k for k, v in ans.items() if v is None],
                   total=total(ans, cfg), raw=crit)
        rows.append(row)
    cost = sum(((r["in_tok"] or 0) - (r.get("cached_tok") or 0)) * p_in + (r.get("cached_tok") or 0) * p_cached
               + (r["out_tok"] or 0) * p_out for r in rows if not r.get("cached")) / 1e6
    return {"tag": tag, "model": model, "wall": round(wall, 1), "cost": round(cost, 4), "rows": rows}


def report(results, set_name):
    print(f"\nНабор: {set_name}")
    for m in results:
        rows = m["rows"]
        lat = sorted(r["latency"] for r in rows if r.get("latency") and not r.get("cached"))
        errs = [r for r in rows if r.get("error")]
        checks = [(r["id"], k, r["answers"].get(k), exp) for r in rows for k, exp in PLANTED.get(r["id"], {}).items()]
        ok = sum(1 for _, _, got, exp in checks if got in exp)
        print(f'\n== {m["tag"]} ({m["model"]}): {len(rows)} диалогов за {m["wall"]} с; '
              f'задержка медиана {lat[len(lat)//2] if lat else 0:.1f} с, макс {lat[-1] if lat else 0:.1f} с; '
              f'~${m["cost"]}; JSON ок {sum(r["json_ok"] for r in rows)}/{len(rows)}; ошибок {len(errs)}; '
              f'недопустимых ответов {sum(len(r["invalid"]) for r in rows)}; '
              f'токены вход/выход {sum(r["in_tok"] or 0 for r in rows)}/{sum(r["out_tok"] or 0 for r in rows)} '
              f'(рассуждения {sum(r.get("reason_tok") or 0 for r in rows)}); 429: {sum(r.get("n429") or 0 for r in rows)}; '
              f'обрезано по лимиту {sum(r.get("finish") == "length" for r in rows)}; формат {sorted({str(r.get("rf")) for r in rows})}')
        if checks:
            print(f"   заложенные нарушения: {ok}/{len(checks)}")
            for cid, k, got, exp in checks:
                if got not in exp:
                    expl = (next((r for r in rows if r["id"] == cid), {}).get("raw", {}).get(k, {}) or {}).get("explanation", "")
                    print(f"   мимо: {cid} кр.{k}: модель {got!r}, ждали {sorted(map(str, exp))} — {str(expl)[:140]}")
        for r in errs[:5]:
            print(f'   ошибка {r["id"]}: {r["error"][:200]}')


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="ds")
    ap.add_argument("--set", default="planted", choices=["planted", "chats"])
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--rf", default="schema", choices=list(RF))
    ap.add_argument("--allow-card", action="store_true", help="разрешить эндпоинты, которые платятся с карты")
    args = ap.parse_args()
    card = [t for t in args.models.split(",") if ENDPOINTS[t].get("pay") == "card"]
    if card and not args.allow_card:
        raise SystemExit(f"Эндпоинты {card} оплачиваются с карты. Владелец запретил траты: "
                         "запуск только с --allow-card и его явного разрешения.")
    load_env()
    cfg = json.loads((ROOT / "lab" / "checklist_v1.json").read_text())
    recs = json.loads((ROOT / "data" / "dev.json").read_text())["data"]["records"]
    recs = [r for r in recs if r["channel"] == "agent chat"]
    if args.set == "planted":
        recs = [r for r in recs if r["conversation_id"] in PLANTED]
    sysmsg = build_system(cfg)
    results = await asyncio.gather(*[run_model(t, recs, cfg, sysmsg, args.conc, args.no_cache, args.max_tokens, args.rf)
                                     for t in args.models.split(",")])
    out = RUNS / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"set": args.set, "args": vars(args), "results": results}, ensure_ascii=False, indent=1))
    report(results, args.set)
    print(f"\nПодробности: {out.relative_to(ROOT)}/results.json; всего ~${sum(m['cost'] for m in results):.4f}")


if __name__ == "__main__":
    asyncio.run(main())
