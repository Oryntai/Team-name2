"""Результаты для организаторов — results/dev/results.json и results/prod/results.json («Формат JSON 1.0»).

Правила — приложение к гайду (data/CQA_Student_Guide_Appendix_Results.pdf): один объект на файл, в results — все
диалоги набора в порядке API. У оценённого диалога восемь критериев с кодами организаторов, значение до учёта веса,
обоснование, номера реплик (с 1 по исходному массиву, вместе с ботом и событиями), итог 0–100 и флаг грубой ошибки.
Диалог, который оценить не удалось, — status "error" с причиной, без подмены нулём.

В файл идут только ответы модели (решения проверяющих по апелляциям — нет) и только оценки по действующему
чек-листу: отпечатки всех восьми определений совпадают с cqa/checklists/default.json. Оценки по изменённому
чек-листу (например, после замены критерия на защите) в выгрузку не попадают. Итог считается по весам действующего
чек-листа той же формулой, что в интерфейсе, и сверяется с независимым расчётом по правилам приложения.

  uv run python -m cqa.results dev                       # DEV: оценить недостающее, выгрузить results/dev/results.json
  uv run python -m cqa.results prod --group-id <ID>      # контроль: загрузить по API, оценить, выгрузить (ровно 10)
  uv run python -m cqa.results prod --file набор.json    # то же из сохранённого ответа API
  uv run python -m cqa.results check results/prod/results.json   # только проверить файл

База — та же, что у сервера (var/cqa.db или CQA_DB); сервер может работать параллельно, прогон виден в журнале.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import pathlib
import sys
from decimal import ROUND_HALF_UP, Decimal

from . import checklist as ck
from . import sources
from .db import DB, DEFAULT_DB, ROOT
from .incremental import fingerprints
from .judge import PROVIDERS, model_name
from .runner import Runner
from .scoring import score

SCHEMA_VERSION = "1.0"
TEAM_NAME = "Название команды"  # так называется команда (владелец, 7.11); переопределяется CQA_TEAM_NAME или --team
CODES = {"01": "contact", "02": "discovery", "03": "identification", "04": "consultation",
         "05": "empathy", "06": "dialogue", "07": "closure", "08": "critical"}
# Таблица «Правила заполнения» (стр. 3 приложения): код → (допустимые value, вес). True/False — «Да»/«Нет», None — N/A.
SPEC = {
    "contact": ((True, False), 10),
    "discovery": ((True, False, None), 15),
    "identification": ((True, False, None), 20),
    "consultation": ((0, 5, 10, 15, 20, None), 20),
    "empathy": ((True, False), 15),
    "dialogue": ((0, 4, 7, 10), 15),
    "closure": ((True, False, None), 5),
    "critical": ((True, False), 0),
}
MODES = ("local", "external_api", "hybrid")
EXPECTED = {"dev": 65, "prod": 10}  # DEV — 60 чатов и 5 звонков; контроль — ровно 10 записей, включая ошибки
NOT_EVALUATED = "Нет оценки по действующему чек-листу."


# --- проверка по правилам приложения (не зависит от cqa/scoring.py) ---

def _value_ok(code: str, v) -> bool:
    # type(...) is: в Python True == 1 и False == 0, а приложение различает true и число
    return any(type(v) is type(x) and v == x for x in SPEC[code][0])


def spec_score(values: dict) -> float | None:
    """Итог по правилам приложения: N/A исключаем, веса применимых масштабируем до 100, два знака (половина вверх)."""
    num = den = Decimal(0)
    for code, (allowed, w) in SPEC.items():
        v = values.get(code)
        if code == "critical" or v is None:
            continue
        top = max(x for x in allowed if type(x) is int) if type(v) is int else None
        num += Decimal(w) if v is True else Decimal(0) if v is False else Decimal(w) * Decimal(v) / Decimal(top)
        den += w
    if not den:
        return None
    return float((num * 100 / den).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def validate(doc, expected_ids: list[str] | None = None, expect_count: int | None = None,
             n_turns: dict[str, int] | None = None) -> list[str]:
    """Нарушения правил приложения; пустой список — файл можно сдавать."""
    errs: list[str] = []

    def fields(obj, need: tuple, where: str) -> bool:
        if not isinstance(obj, dict):
            errs.append(f"{where}: ожидался объект.")
            return False
        miss, extra = [k for k in need if k not in obj], [k for k in obj if k not in need]
        if miss:
            errs.append(f"{where}: нет полей {', '.join(miss)}.")
        if extra:
            errs.append(f"{where}: лишние поля {', '.join(extra)}.")
        return not miss

    if not fields(doc, ("schema_version", "team_name", "model", "results"), "Файл"):
        return errs
    if doc["schema_version"] != SCHEMA_VERSION:
        errs.append(f"schema_version: {doc['schema_version']!r}, нужно {SCHEMA_VERSION!r}.")
    if not isinstance(doc["team_name"], str) or not doc["team_name"].strip():
        errs.append("team_name: пусто.")
    if fields(doc["model"], ("mode", "name"), "model"):
        if doc["model"]["mode"] not in MODES:
            errs.append(f"model.mode: {doc['model']['mode']!r}, допустимо {', '.join(MODES)}.")
        if not isinstance(doc["model"]["name"], str) or not doc["model"]["name"].strip():
            errs.append("model.name: пусто.")
    results = doc["results"]
    if not isinstance(results, list):
        return errs + ["results: ожидался массив."]
    seen: list[str] = []
    for i, r in enumerate(results, 1):
        cid = r.get("conversation_id") if isinstance(r, dict) else None
        if not isinstance(cid, str) or not cid:
            errs.append(f"results[{i}]: нет conversation_id.")
            continue
        seen.append(cid)
        status = r.get("status")
        if status == "error":
            if fields(r, ("conversation_id", "status", "criteria", "score", "error"), cid):
                if r["criteria"] != []:
                    errs.append(f"{cid}: при status error criteria — пустой массив.")
                if r["score"] is not None:
                    errs.append(f"{cid}: при status error score — null.")
                if not isinstance(r["error"], str) or not r["error"].strip():
                    errs.append(f"{cid}: при status error нужна причина в error.")
            continue
        if status != "ok":
            errs.append(f"{cid}: status {status!r}, допустимо ok или error.")
            continue
        if not fields(r, ("conversation_id", "status", "criteria", "score"), cid):
            continue
        if not isinstance(r["criteria"], list):
            errs.append(f"{cid}: criteria — массив.")
            continue
        values = {}
        for c in r["criteria"]:
            if not fields(c, ("criterion_id", "value", "rationale", "evidence_turns"), f"{cid}, критерий"):
                continue
            code = c["criterion_id"]
            if code not in SPEC:
                errs.append(f"{cid}: неизвестный criterion_id {code!r}.")
                continue
            if code in values:
                errs.append(f"{cid}: {code} указан дважды.")
                continue
            values[code] = c["value"]
            if not _value_ok(code, c["value"]):
                allowed = ", ".join("null" if x is None else json.dumps(x) for x in SPEC[code][0])
                errs.append(f"{cid}: {code} = {json.dumps(c['value'], ensure_ascii=False)}, допустимо {allowed}.")
            if not isinstance(c["rationale"], str) or not c["rationale"].strip():
                errs.append(f"{cid}: {code} — пустое обоснование.")
            turns = c["evidence_turns"]
            if not isinstance(turns, list) or any(type(n) is not int or n < 1 for n in turns):
                errs.append(f"{cid}: {code} — evidence_turns: массив целых номеров реплик с 1.")
            elif n_turns and cid in n_turns and any(n > n_turns[cid] for n in turns):
                errs.append(f"{cid}: {code} — реплики {[n for n in turns if n > n_turns[cid]]} вне диалога "
                            f"(в нём {n_turns[cid]}).")
        missing = [k for k in SPEC if k not in values]
        if missing:
            errs.append(f"{cid}: нет критериев {', '.join(missing)}.")
        if not fields(r["score"], ("final_score", "critical_flag"), f"{cid}, score"):
            continue
        fs, flag = r["score"]["final_score"], r["score"]["critical_flag"]
        if type(fs) not in (int, float) or not 0 <= fs <= 100 or round(fs, 2) != fs:
            errs.append(f"{cid}: final_score {fs!r} — число от 0 до 100 с двумя знаками.")
        elif not missing and all(_value_ok(k, values[k]) for k in SPEC):
            want = spec_score(values)
            if want is None or abs(fs - want) > 0.001:
                errs.append(f"{cid}: final_score {fs}, по правилам приложения {want}.")
        if type(flag) is not bool:
            errs.append(f"{cid}: critical_flag — true или false.")
        elif "critical" in values and flag is not values["critical"]:
            errs.append(f"{cid}: critical_flag не совпадает с value критерия critical.")
    dups = sorted({c for c in seen if seen.count(c) > 1})
    if dups:
        errs.append(f"Повторяются ID: {', '.join(dups)}.")
    if expected_ids is not None:
        miss = [c for c in expected_ids if c not in seen]
        extra = sorted(set(seen) - set(expected_ids))
        if miss:
            errs.append(f"Нет записей для: {', '.join(miss)}.")
        if extra:
            errs.append(f"Лишние записи: {', '.join(extra)}.")
    if expect_count is not None and len(results) != expect_count:
        errs.append(f"Записей {len(results)}, нужно ровно {expect_count}.")
    return errs


def load_strict(path: str | os.PathLike) -> dict:
    """Чтение как у организаторов: UTF-8 без BOM, строгий JSON (без комментариев, лишних запятых и NaN)."""
    text = pathlib.Path(path).read_bytes().decode("utf-8")
    if text.startswith("﻿"):
        raise ValueError("в начале файла BOM")

    def bad(const):
        raise ValueError(f"недопустимое значение {const}")

    return json.loads(text, parse_constant=bad)


def dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


# --- выгрузка из базы ---

def official_config() -> dict:
    return ck.normalize(ck.load_default())


def _canon(config: dict) -> str:
    return json.dumps(ck.normalize(config), sort_keys=True, ensure_ascii=False)


def official_checklist_id(db: DB, config: dict) -> int:
    """Версия действующего чек-листа в базе; если её нет (база создана до правки default.json) — сохраняем новой."""
    want = _canon(config)
    for row in reversed(db.list_checklists()):
        cl = db.checklist(row["id"])
        if cl and _canon(cl["config"]) == want:
            return cl["id"]
    cl_id, _ = db.create_checklist(config, note="Действующий чек-лист организаторов (для results.json)")
    return cl_id


def eligible(ev: dict, config: dict, allow_fake: bool = False) -> bool:
    """Успешный ответ модели по действующему чек-листу: совпадают отпечатки всех восьми определений."""
    kind = PROVIDERS.get(ev.get("provider") or "", {}).get("kind")
    if ev.get("status") != "ok" or kind is None or (kind == "fake" and not allow_fake):
        return False
    want = fingerprints(config, ev.get("model") or "")
    got = ev.get("answers") or {}
    return set(got) == set(want) and all((got[k] or {}).get("fingerprint") == h for k, h in want.items())


def pick(db: DB, cid: str, config: dict, allow_fake: bool = False,
         min_id: int = 0) -> tuple[dict | None, dict | None]:
    """(последняя годная оценка, последняя оценка вообще) — вторая нужна для причины ошибки."""
    evs = [e for e in db.evaluations_for(cid) if e["id"] > min_id]  # новые первыми; min_id — для --fresh
    return next((e for e in evs if eligible(e, config, allow_fake)), None), (evs[0] if evs else None)


def to_value(answer: str):
    if answer == ck.NA:
        return None
    if answer == ck.YES:
        return True
    if answer == ck.NO:
        return False
    return int(answer)


def record(cid: str, ev: dict | None, last: dict | None, config: dict, n_turns: int) -> dict:
    if ev is None:
        reason = last.get("error") if last and last.get("status") == "error" and last.get("error") else NOT_EVALUATED
        return {"conversation_id": cid, "status": "error", "criteria": [], "score": None, "error": reason}
    answers = ev["answers"]
    s = score(config, {k: a["answer"] for k, a in answers.items()})
    criteria = []
    for c in config["criteria"]:
        a = answers[c["id"]]
        turns: list[int] = []
        for n in a.get("evidence") or []:  # ссылки на несуществующие реплики ничего не подтверждают
            if type(n) is int and 1 <= n <= n_turns and n not in turns:
                turns.append(n)
        criteria.append({"criterion_id": CODES[c["id"]], "value": to_value(a["answer"]),
                         "rationale": (a.get("explanation") or "").strip(), "evidence_turns": turns})
    return {"conversation_id": cid, "status": "ok", "criteria": criteria,
            "score": {"final_score": s["total"], "critical_flag": bool(s["gross"])}}


def model_block(evaluations: list[dict], fallback_provider: str) -> dict:
    """mode: local — своя модель (OpenAI-совместимый сервер), external_api — Claude, обе сразу — hybrid."""
    used = [(PROVIDERS[e["provider"]]["kind"], e["model"]) for e in evaluations] or \
           [(PROVIDERS[fallback_provider]["kind"], model_name(fallback_provider))]
    modes = {"external_api" if kind == "anthropic" else "local" for kind, _ in used}
    return {"mode": modes.pop() if len(modes) == 1 else "hybrid", "name": ", ".join(sorted({m for _, m in used}))}


def turn_counts(db: DB, ids: list[str]) -> dict[str, int]:
    out = {}
    for cid in ids:
        c = db.conversation(cid)
        if c:
            out[cid] = len(c["transcript"])
    return out


def build(db: DB, ids: list[str], config: dict, team: str, provider: str = "haiku",
          allow_fake: bool = False, min_id: int = 0) -> dict:
    turns = turn_counts(db, ids)
    results, used = [], []
    for cid in ids:
        ev, last = pick(db, cid, config, allow_fake, min_id)
        results.append(record(cid, ev, last, config, turns.get(cid, 0)))
        if ev:
            used.append(ev)
    return {"schema_version": SCHEMA_VERSION, "team_name": team, "model": model_block(used, provider),
            "results": results}


# --- командная строка ---

async def _dataset_ids(db: DB, args) -> list[str]:
    if args.cmd == "dev":
        ds = args.dataset or "DEV"
        if not db.conversation_ids(ds):
            gid, total, records = sources.parse_bytes((ROOT / "data" / "dev.json").read_bytes())
            ds = gid or ds
            db.save_dataset(ds, "file:data/dev.json", records, total)
        ids = db.conversation_ids(ds)
    elif args.file:
        gid, total, records = sources.parse_bytes(pathlib.Path(args.file).read_bytes())
        ds = gid or args.group_id or "PROD"
        db.save_dataset(ds, f"file:{pathlib.Path(args.file).name}", records, total)
        ids = [r["conversation_id"] for r in records]
    elif args.group_id:
        url = args.url or os.environ.get("CQA_DATASET_URL") or db.get_setting("dataset_url")
        gid, total, records = await sources.fetch(url, args.group_id)
        ds = gid or args.group_id
        db.save_dataset(ds, f"api:{args.group_id}", records, total)
        ids = [r["conversation_id"] for r in records]
    elif args.dataset:
        ds, ids = args.dataset, db.conversation_ids(args.dataset)
    else:
        raise sources.SourceError("Укажите --group-id (загрузить по API), --file или --dataset (уже в базе).")
    channels = collections.Counter((db.conversation(c) or {}).get("channel") or "?" for c in ids)
    print(f"Набор {ds}: {len(ids)} диалогов ({', '.join(f'{k} {v}' for k, v in sorted(channels.items()))}).", flush=True)
    return ids


async def _evaluate(db: DB, ids: list[str], checklist_id: int, provider: str) -> None:
    runner = Runner(db)
    run_id = runner.start(ids, checklist_id, provider, mode="full")
    print(f"Прогон №{run_id}: {len(ids)} диалогов, модель {model_name(provider)}.", flush=True)
    shown = None
    while runner.tasks:
        await asyncio.sleep(3)
        r = db.run(run_id)
        line = f"  готово {r['done']}, ошибок {r['failed']} из {r['total']} · ${r['cost_usd'] or 0:.4f}"
        if line != shown:
            print(line, flush=True)
            shown = line
    await runner.wait()


def _report(out: pathlib.Path, doc: dict, errs: list[str]) -> None:
    res = doc["results"]
    ok = [r for r in res if r["status"] == "ok"]
    bad = [r for r in res if r["status"] != "ok"]
    mean = sum(r["score"]["final_score"] for r in ok) / len(ok) if ok else 0.0
    flags = sum(r["score"]["critical_flag"] for r in ok)
    empty = sum(not c["evidence_turns"] for r in ok for c in r["criteria"])
    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"{rel}: записей {len(res)} — ok {len(ok)}, error {len(bad)}; модель {doc['model']['mode']} · "
          f"{doc['model']['name']}; команда «{doc['team_name']}»; средний итог {mean:.2f}; грубых ошибок {flags}; "
          f"критериев без реплик-доказательств {empty}.")
    for r in bad:
        print(f"  error {r['conversation_id']}: {r['error']}")
    if errs:
        print("Проверка по правилам приложения НЕ пройдена:")
        for e in errs:
            print(f"  - {e}")
    else:
        print("Проверка по правилам приложения пройдена.")
    if not errs and not bad:
        print(f"Дальше: git add {rel} && git commit -m 'Результаты {rel.parent.name}' && git push")


async def _run_set(args) -> int:
    allow_fake = os.environ.get("CQA_ALLOW_FAKE") == "1"
    if args.provider == "fake" and not allow_fake:
        print("Заглушка fake — только для тестов (CQA_ALLOW_FAKE=1).")
        return 2
    db = DB(args.db)
    db.seed_default_checklist()
    config = official_config()
    try:
        ids = await _dataset_ids(db, args)
    except (sources.SourceError, OSError) as e:
        print(f"Набор не загружен: {e}")
        return 1
    if not ids:
        print("Набор пуст.")
        return 1
    checklist_id = official_checklist_id(db, config)
    # --fresh: в файл идут только оценки этого запуска (старые и новые не смешиваем)
    min_id = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM evaluations")["m"] if args.fresh else 0
    if not args.no_evaluate:
        for _ in range(2):  # второй заход — для диалогов, не прошедших и после повторов внутри прогона
            todo = [cid for cid in ids if pick(db, cid, config, allow_fake, min_id)[0] is None]
            if not todo:
                break
            await _evaluate(db, todo, checklist_id, args.provider)
    doc = build(db, ids, config, args.team, args.provider, allow_fake, min_id)
    errs = validate(doc, expected_ids=ids, expect_count=args.expect, n_turns=turn_counts(db, ids))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump(doc), encoding="utf-8")
    _report(out.resolve(), doc, errs)
    return 1 if errs or any(r["status"] != "ok" for r in doc["results"]) else 0


def _check(args) -> int:
    path = pathlib.Path(args.path)
    try:
        doc = load_strict(path)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"{path}: не читается как строгий JSON в UTF-8 — {e}")
        return 1
    expect = args.expect if args.expect is not None else EXPECTED.get(path.resolve().parent.name)
    db_path = pathlib.Path(args.db or os.environ.get("CQA_DB") or DEFAULT_DB)
    turns = None
    if db_path.exists() and isinstance(doc, dict) and isinstance(doc.get("results"), list):
        ids = [r.get("conversation_id") for r in doc["results"] if isinstance(r, dict)]
        turns = turn_counts(DB(db_path), [c for c in ids if isinstance(c, str)])
    errs = validate(doc, expect_count=expect, n_turns=turns)
    if errs:
        print(f"{path}: проверка по правилам приложения НЕ пройдена:")
        for e in errs:
            print(f"  - {e}")
        return 1
    _report(path.resolve(), doc, [])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m cqa.results", description="Результаты для организаторов (формат JSON 1.0).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, about in (("dev", "DEV: оценить недостающее и выгрузить results/dev/results.json"),
                        ("prod", "контроль: загрузить набор, оценить, выгрузить results/prod/results.json")):
        s = sub.add_parser(name, help=about)
        s.add_argument("--team", default=os.environ.get("CQA_TEAM_NAME") or TEAM_NAME, help="название команды")
        s.add_argument("--provider", default="haiku", choices=list(PROVIDERS), help="модель (как в интерфейсе)")
        s.add_argument("--dataset", help="id набора, уже загруженного в базу")
        s.add_argument("--no-evaluate", action="store_true", help="не вызывать модель, выгрузить то, что уже оценено")
        s.add_argument("--fresh", action="store_true", help="переоценить весь набор заново: в файл — только оценки этого запуска")
        s.add_argument("--out", default=str(ROOT / "results" / name / "results.json"))
        s.add_argument("--expect", type=int, default=EXPECTED[name], help="сколько записей должно быть")
        s.add_argument("--db", help="путь к базе (по умолчанию var/cqa.db или CQA_DB)")
        if name == "prod":
            s.add_argument("--group-id", help="group_id контрольного набора (ссылка API — CQA_DATASET_URL в .env)")
            s.add_argument("--url", help="ссылка GET организаторов, если отличается от CQA_DATASET_URL")
            s.add_argument("--file", help="JSON-ответ API, сохранённый в файл")
    c = sub.add_parser("check", help="проверить готовый results.json по правилам приложения")
    c.add_argument("path")
    c.add_argument("--expect", type=int, help="сколько записей должно быть (по умолчанию по папке: dev 65, prod 10)")
    c.add_argument("--db", help="база для проверки номеров реплик")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return _check(args)
    return asyncio.run(_run_set(args))


if __name__ == "__main__":
    from .__main__ import load_env

    load_env()
    sys.exit(main())
