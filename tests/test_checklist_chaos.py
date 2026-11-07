"""Давление на редактор: импорт → версия → plan → incremental/full → review → try/assist.

Только временная SQLite и fake. Формат запросов и clean_config повторяют
web/js/components/checklist-editor.js; арифметический oracle независим от cqa.scoring.
Без xfail: регрессии продукта должны оставаться красными.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
import time
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient

from cqa import api, judge, runner
from cqa.judge.providers import FakeProvider


DATASET = "CHAOS"
IDS = [f"chaos-{i}" for i in range(4)]
RECORDS = [
    {"conversation_id": cid, "channel": "agent chat", "language": language,
     "audio_url": None, "transcript": [
         {"timestamp": "00:00", "speaker": "client", "text": question},
         {"timestamp": "00:02", "speaker": "bot", "text": "Соединяю с оператором."},
         {"timestamp": "00:05", "speaker": "operator", "text": greeting},
         {"timestamp": "00:20", "speaker": "operator", "text": "Проверю перевод и вернусь в чат."},
         {"timestamp": "00:40", "speaker": "client", "text": "Спасибо!"},
         {"timestamp": "00:45", "speaker": "operator", "text": "Перевод зачислен. Всего доброго!"},
     ]}
    for cid, language, question, greeting in zip(
        IDS, ["RU", "RU", "KZ", "EN"],
        ["Где перевод?", "Какие тарифы?", "Аударым қайда?", "Where is my transfer?"],
        ["Здравствуйте, я Дина.", "Здравствуйте!", "Сәлеметсіз бе!", "Hello, I am Dina."],
        strict=True,
    )
]


class ChaosFake(FakeProvider):
    """Та же модель fake; разные допустимые ответы выявляют ошибки перерасчёта.

В частности, chaos-2 действительно имеет N/A до его выключения. Event задерживает
только fake.complete, не Runner/SQLite/API, и гарантирует пересечение save/run.
"""

    def __init__(self):
        super().__init__()
        self.hold = threading.Event()
        self.entered = threading.Event()
        self.calls = []

    async def complete(self, system, user, schema, **kwargs):
        assert self.pid == "fake"
        self.calls.append(copy.deepcopy(schema))
        if "criteria" in schema["properties"]:
            if self.hold.is_set():
                self.entered.set()
                while self.hold.is_set():
                    await asyncio.sleep(0.005)
            reply = await super().complete(system, user, schema, **kwargs)
            result = json.loads(reply.text)
            variant = next(i for i, cid in enumerate(IDS) if f"Диалог {cid} ·" in user)
            for cid, item in result["criteria"].items():
                allowed = schema["properties"]["criteria"]["properties"][cid]["properties"]["answer"]["enum"]
                ordinary = [a for a in allowed if a != "N/A"]
                # Не все ответы максимальны: изменение веса должно менять итог.
                if variant == 1:
                    item["answer"] = ordinary[-1] if cid in {"01", "04", "08", "09"} else ordinary[0]
                elif variant == 2:
                    item["answer"] = "N/A" if "N/A" in allowed else ordinary[0]
                elif variant == 3:
                    item["answer"] = ordinary[len(ordinary) // 2]
            reply.text = json.dumps(result, ensure_ascii=False)
            return reply
        return await super().complete(system, user, schema, **kwargs)


def clean_config(config):
    """Python-эквивалент cleanConfig для используемых редактором значений."""
    def number(value):
        # JSON.stringify(Number(20)) пишет 20, а не Python-овское 20.0:
        # это существенно для отпечатка определения шкалы.
        value = float(value)
        return int(value) if value.is_integer() else value

    cfg = copy.deepcopy(config)
    cfg["rules"] = [s.strip() for s in cfg.get("rules", []) if s.strip()]
    for c in cfg["criteria"]:
        c["weight"] = number(c.get("weight") or 0)
        c["na"] = bool(c.get("na"))
        if c["type"] == "flag":
            c.update(weight=0, na=False)
        if not c["na"]:
            c["na_when"] = ""
        c["steps"] = (sorted(
            [{"value": number(s["value"]), "text": s["text"].strip()} for s in c.get("steps", [])],
            key=lambda s: -s["value"],
        ) if c["type"] == "scale" else [])
        for key in ("section", "title", "yes", "no", "na_when", "guidance"):
            c[key] = str(c.get(key) or "").strip()
    return cfg


def request(c, method, path, status=200, **kwargs):
    response = c.request(method, path, **kwargs)
    assert response.status_code == status, (
        f"{method} {path}: HTTP {response.status_code}, ожидался {status}: {response.text}"
    )
    return response.json()


def body(checklist_id=1, mode="incremental", **kwargs):
    return {"dataset_id": DATASET, "checklist_id": checklist_id,
            "provider": "fake", "mode": mode, **kwargs}


def wait_run(c, rid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = request(c, "GET", f"/api/runs/{rid}")
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    pytest.fail(f"Прогон {rid} не завершился за 10 секунд: {run}")


def execute(c, payload):
    rid = request(c, "POST", "/api/runs", json=payload)["run_id"]
    return wait_run(c, rid)


def healthy(run, expected_ids):
    assert run["status"] == "done" and run["failed"] == 0, run
    assert run["done"] == run["total"] == len(expected_ids), run
    assert {i["conversation_id"] for i in run["items"]} == set(expected_ids), run
    assert all(i["status"] == "ok" and i["error"] is None for i in run["items"]), run


def rounded(value, digits=2):
    return float(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def oracle(config, answers):
    """Независимо рассчитываем итог, секции и набор флагов из фактических ответов."""
    weights, earned, groups = {}, {}, {}
    flags = []
    for c in config["criteria"]:
        cid, answer = c["id"], answers[c["id"]]
        groups.setdefault(c["section"] or "Без секции", []).append(cid)
        if c["type"] == "flag":
            if answer == "Да":
                flags.append(cid)
        elif answer != "N/A":
            weights[cid] = Decimal(str(c["weight"]))
            factor = (Decimal(int(answer == "Да")) if c["type"] == "bool" else
                      Decimal(answer) / max(Decimal(str(s["value"])) for s in c["steps"]))
            earned[cid] = weights[cid] * factor
    denominator = sum(weights.values(), Decimal(0))
    total = rounded(sum(earned.values(), Decimal(0)) / denominator * 100, config["rounding"])
    sections = {}
    for name, ids in groups.items():
        maximum = sum((weights.get(cid, Decimal(0)) for cid in ids), Decimal(0))
        points = sum((earned.get(cid, Decimal(0)) for cid in ids), Decimal(0))
        sections[name] = {"ids": ids, "points_eff": rounded(points / denominator * 100),
                          "max_eff": rounded(maximum / denominator * 100),
                          "percent": rounded(points / maximum * 100) if maximum else None}
    return {"total": total, "gross_ids": flags,
            "gross": bool(flags) if any(c["type"] == "flag" for c in config["criteria"]) else None,
            "applicable_weight": float(denominator), "sections": sections}


def check_score(view, config, answers):
    expected = oracle(config, answers)
    assert view is not None
    for field in ("total", "gross", "gross_ids", "applicable_weight"):
        assert view[field] == expected[field], (field, view, expected)
    assert {s["name"]: {k: v for k, v in s.items() if k != "name"} for s in view["sections"]} == expected["sections"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "read_env", lambda key, default=None: default)
    provider = ChaosFake()

    def only_fake(pid):
        assert pid == "fake", f"Попытка обратиться к сетевой модели {pid}"
        return provider

    monkeypatch.setattr(judge, "get_provider", only_fake)
    monkeypatch.setattr(runner, "RETRY_PAUSES", (0, 0, 0))
    app = api.create_app(tmp_path / "chaos.db", web_dir=tmp_path / "web",
                         data_dir=tmp_path / "data", allow_fake=True)
    try:
        with TestClient(app) as c:
            payload = {"error": "", "isSuccess": True, "data": {
                "dataset_group_id": DATASET, "total_records": len(RECORDS), "records": RECORDS}}
            uploaded = request(c, "POST", "/api/datasets/upload", files={
                "file": ("chaos.json", json.dumps(payload, ensure_ascii=False).encode(), "application/json")})
            assert uploaded == {"dataset_id": DATASET, "imported": 4, "total": 4}
            # requestAssist в редакторе не передаёт provider: задаём fake через настройки API.
            request(c, "PUT", "/api/settings", json={"default_provider": "fake"})
            yield c, provider
    finally:
        provider.hold.clear()
        app.state.db.close()


def steps(values):
    return [{"value": v, "text": f"Действие выполнено на уровне {v}."} for v in values]


# name, допустим ли чек-лист, целевой критерий для appeal/assist, текст ошибки.
CASES = [
    ("rename", True, "01", ""),
    ("yes_no_wording", True, "01", ""),
    ("weight_only", True, "01", ""),
    ("weights_not_100", True, "01", ""),
    ("add_bool_09", True, "09", ""),
    ("add_scale_0_3_5", True, "09", ""),
    ("unordered_scale", True, "04", ""),
    ("top_04_20_to_10", True, "04", ""),
    ("top_04_duplicate_10", False, "04", "повторяются"),
    ("delete_criterion", True, "01", ""),
    ("one_criterion", True, "01", ""),
    ("bool_to_scale", True, "01", ""),
    ("scale_to_bool", True, "04", ""),
    ("na_without_condition", False, "01", "условие"),
    ("disable_na", True, "02", ""),
    ("zero_weight", False, "01", "больше 0"),
    ("two_gross_flags", True, "09", ""),
    ("delete_gross_flag", True, "01", ""),
    ("new_section_and_no_section", True, "01", ""),
    ("ids_10_a1_unordered", True, "a1", ""),
    ("title_5000", True, "01", ""),
    ("emoji_quotes_angles", True, "01", ""),
    ("empty_title", False, "01", "название"),
    ("duplicate_id", False, "01", "повторяется"),
    ("save_during_run", True, "01", ""),
    ("partly_unevaluated", True, "01", ""),
]


def mutate(config, name):
    cfg = copy.deepcopy(config)
    cs = cfg["criteria"]
    by_id = {c["id"]: c for c in cs}
    if name in {"rename", "save_during_run", "partly_unevaluated"}:
        by_id["01"]["title"] = "Приветствие и представление оператора"
    elif name == "yes_no_wording":
        by_id["01"].update(yes="Оператор назвал имя и уточнил удобную форму обращения.",
                           no="Оператор не назвал имя либо не уточнил форму обращения.")
    elif name == "weight_only":
        by_id["01"]["weight"] = 25
    elif name == "weights_not_100":
        for c in cs:
            if c["type"] != "flag":
                c["weight"] = 1
    elif name in {"add_bool_09", "add_scale_0_3_5", "two_gross_flags"}:
        new = {"id": "09", "section": "Новая секция", "title": "Проверка понимания",
               "type": "bool", "weight": 7, "na": False,
               "yes": "Оператор попросил клиента подтвердить понимание ответа.",
               "no": "Подтверждения понимания нет.", "steps": []}
        if name == "add_scale_0_3_5":
            new.update(type="scale", steps=steps([0, 3, 5]))
        elif name == "two_gross_flags":
            new.update(type="flag", weight=0, title="Угроза клиенту", yes="Оператор угрожал клиенту.")
        cs.append(new)
    elif name == "unordered_scale":
        by_id["04"]["steps"] = [by_id["04"]["steps"][i] for i in [4, 0, 2, 1, 3]]
    elif name == "top_04_20_to_10":
        # Уменьшаем диапазон всей шкалы: верхняя 10, остальные уникальны и ниже неё.
        for s in by_id["04"]["steps"]:
            s["value"] /= 2
    elif name == "top_04_duplicate_10":
        # Буквальная правка только 20 → 10: в исходной шкале уже есть 10.
        by_id["04"]["steps"][0]["value"] = 10
    elif name == "delete_criterion":
        cs.remove(by_id["05"])
    elif name == "one_criterion":
        cfg["criteria"] = [by_id["01"]]
    elif name == "bool_to_scale":
        by_id["01"].update(type="scale", steps=steps([0, 3, 5]))
    elif name == "scale_to_bool":
        by_id["04"].update(type="bool", yes="Дан полный ответ.", no="Полного ответа нет.")
    elif name == "na_without_condition":
        by_id["01"].update(na=True, na_when="")
    elif name == "disable_na":
        by_id["02"]["na"] = False
    elif name == "zero_weight":
        by_id["01"]["weight"] = 0
    elif name == "delete_gross_flag":
        cs.remove(by_id["08"])
    elif name == "new_section_and_no_section":
        by_id["01"]["section"] = "Совсем новая секция"
        by_id["02"]["section"] = ""
    elif name == "ids_10_a1_unordered":
        by_id["01"]["id"] = "a1"
        by_id["02"]["id"] = "10"
        cs.reverse()
    elif name == "title_5000":
        by_id["01"]["title"] = "Я" * 5000
    elif name == "emoji_quotes_angles":
        text = '🤖 «Да» / "Нет" <script>alert("тест")</script> & <критерий>'
        by_id["01"].update(title=text, yes=text, no=text, guidance=text, section=text)
    elif name == "empty_title":
        by_id["01"]["title"] = "  \n\t "
    elif name == "duplicate_id":
        by_id["02"]["id"] = "01"
    else:
        raise AssertionError(f"Неизвестный сценарий {name}")
    return cfg


def readable_evaluations(c, run, config):
    healthy(run, IDS)
    evaluations = {}
    for item in run["items"]:
        cid, eid = item["conversation_id"], item["evaluation_id"]
        ev = request(c, "GET", f"/api/evaluations/{eid}")
        assert ev["status"] == "ok" and ev["error"] is None, ev
        assert ev["checklist"]["id"] == run["checklist_id"]
        assert [k["id"] for k in ev["criteria"]] == [k["id"] for k in config["criteria"]]
        for actual, expected in zip(ev["criteria"], config["criteria"], strict=True):
            assert actual["title"] == expected["title"] and actual["type"] == expected["type"]
            assert actual["weight"] == expected["weight"] and actual["na"] == expected["na"]
            assert actual["original"]["answer"] in actual["allowed"]
            assert actual["original"]["explanation"] and actual["original"]["evidence"]
        answers = {k["id"]: k["original"]["answer"] for k in ev["criteria"]}
        check_score(ev["original"], config, answers)
        check_score(ev["final"], config, answers)
        assert item["total"] == ev["original"]["total"]
        conv = request(c, "GET", f"/api/conversations/{cid}")
        card = next(e for e in conv["evaluations"] if e["evaluation_id"] == eid)
        assert card["status"] == "ok" and card["total"] == ev["original"]["total"]
        evaluations[cid] = ev
    return evaluations


def check_diff(c, run, previous, current):
    diff = request(c, "GET", f"/api/runs/{run['id']}/diff")
    assert diff["run_id"] == run["id"] and len(diff["items"]) == len(IDS)
    counts = {}
    for item in diff["items"]:
        cid = item["conversation_id"]
        before = previous.get(cid)
        after = current[cid]
        assert item["status"] == "ok" and item["error"] is None
        assert item["previous"] == ({"id": before["id"], "total": before["original"]["total"],
                                      "gross": before["original"]["gross"], "status": "ok"} if before else None)
        assert item["current"] == {"id": after["id"], "total": after["original"]["total"],
                                   "gross": after["original"]["gross"], "status": "ok"}
        old = {k["id"]: k["original"]["answer"] for k in before["criteria"]} if before else {}
        new = {k["id"]: k["original"]["answer"] for k in after["criteria"]}
        expected = {k: (old.get(k), new.get(k)) for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
        assert {x["id"]: (x["before"], x["after"]) for x in item["changes"]} == expected
        for k in expected:
            counts[k] = counts.get(k, 0) + 1
    assert all(row["changed"] == counts.get(row["id"], 0) for row in diff["criteria"])
    return diff


def review(c, config, ev, target):
    criterion = next(k for k in ev["criteria"] if k["id"] == target)
    # Проверяем реальное N/A-решение, если разрешено; иначе ноль/Нет.
    alternative = next(a for a in reversed(criterion["allowed"]) if a != criterion["final_answer"])
    appeal = request(c, "POST", "/api/appeals", status=201, json={
        "evaluation_id": ev["id"], "criterion_id": target, "comment": "Проверка изменённого критерия по репликам."})
    assert appeal["allowed"] == criterion["allowed"] and appeal["status"] == "open"
    result = request(c, "POST", f"/api/appeals/{appeal['id']}/decision", json={
        "decision": "change", "new_answer": alternative, "reason": "Проверяющий сверил реплики 1–6."})
    assert result["appeal"]["status"] == "changed"
    updated = request(c, "GET", f"/api/evaluations/{ev['id']}")
    assert updated == result["evaluation"]
    assert updated["original"] == ev["original"]
    answers = {k["id"]: k["original"]["answer"] for k in ev["criteria"]}
    answers[target] = alternative
    check_score(updated["final"], config, answers)
    changed = next(k for k in updated["criteria"] if k["id"] == target)
    assert changed["final_answer"] == alternative and changed["appeal"]["status"] == "changed"
    conv = request(c, "GET", f"/api/conversations/{ev['conversation_id']}")
    summary = next(e for e in conv["evaluations"] if e["evaluation_id"] == ev["id"])
    assert summary["final_total"] == updated["final"]["total"]
    assert summary["final_gross"] == updated["final"]["gross"]
    assert result["appeal"]["total_final"] == updated["final"]["total"]


@pytest.mark.parametrize("name,valid,target,error_text", CASES, ids=[c[0] for c in CASES])
def test_checklist_chaos(setup, name, valid, target, error_text):
    c, provider = setup
    original = request(c, "GET", "/api/checklists/1")["config"]
    initial_ids = IDS[:2] if name == "partly_unevaluated" else IDS
    initial = execute(c, body(mode="full", conversation_ids=initial_ids))
    healthy(initial, initial_ids)
    previous = {item["conversation_id"]: request(c, "GET", f"/api/evaluations/{item['evaluation_id']}")
                for item in initial["items"]}
    for ev in previous.values():
        check_score(ev["original"], original, {k["id"]: k["original"]["answer"] for k in ev["criteria"]})
    if name == "disable_na":
        assert next(k for k in previous[IDS[2]]["criteria"] if k["id"] == "02")["original"]["answer"] == "N/A"
    if name == "partly_unevaluated":
        assert all(request(c, "GET", f"/api/conversations/{cid}")["evaluations"] == [] for cid in IDS[2:])

    draft = mutate(original, name)
    config = clean_config(draft)
    old_run = None
    try:
        if name == "save_during_run":
            provider.hold.set()
            old_run = request(c, "POST", "/api/runs", json=body(mode="full"))["run_id"]
            assert provider.entered.wait(3), "fake.complete не начался"
            assert request(c, "GET", f"/api/runs/{old_run}")["status"] == "running"
        versions_before = request(c, "GET", "/api/checklists")
        validation = request(c, "POST", "/api/checklists/validate", json={"config": config})
        response = c.post("/api/checklists", json={"config": config, "note": f"Хаос: {name}", "parent_id": 1})
        if not valid:
            assert 400 <= response.status_code < 500, f"Недопустимый чек-лист: HTTP {response.status_code}: {response.text}"
            rejected = response.json()
            assert re.search("[А-Яа-яЁё]", rejected["detail"]) and rejected["errors"], rejected
            assert error_text in " ".join(rejected["errors"]), rejected
            assert validation["ok"] is False and validation["errors"]
            assert request(c, "GET", "/api/checklists") == versions_before
            trial = request(c, "POST", "/api/checklists/try", status=422,
                            json={"config": config, "provider": "fake"})
            assert trial["errors"] and error_text in " ".join(trial["errors"])
            # Assist намеренно принимает невалидную семантику черновика, но не повтор id.
            hint = request(c, "POST", "/api/checklists/assist", status=422 if name == "duplicate_id" else 200,
                           json={"config": draft, "criterion_id": target})
            assert hint.get("errors") if name == "duplicate_id" else hint.get("guidance")
            return
        assert response.status_code == 201, f"POST /api/checklists: HTTP {response.status_code}: {response.text}"
        assert validation == {"ok": True, "errors": []}
        saved = response.json()
        new_id = saved["id"]
        assert saved["version"] == 2
        version = request(c, "GET", f"/api/checklists/{new_id}")
        assert version["parent_id"] == 1 and version["config"] == config
        assert request(c, "GET", "/api/checklists/1")["config"] == original
        if old_run is not None:
            # Сохранение не изменяет конфигурацию уже выполняющегося прогона.
            assert request(c, "GET", f"/api/runs/{old_run}")["status"] == "running"
    finally:
        provider.hold.clear()

    if old_run is not None:
        frozen = wait_run(c, old_run)
        previous = readable_evaluations(c, frozen, original)
        assert all(ev["checklist"]["id"] == 1 for ev in previous.values())

    # Ожидания плана формируем сравнением определений, без вызова partition/fingerprints.
    definition_fields = ("id", "title", "type", "yes", "no", "na", "na_when", "steps", "guidance")
    old_by_id = {k["id"]: k for k in original["criteria"]}
    reusable = {k["id"] for k in config["criteria"] if k["id"] in old_by_id
                and all(k.get(f) == old_by_id[k["id"]].get(f) for f in definition_fields)}
    n, old_n = len(config["criteria"]), len(previous)
    for mode in ("incremental", "full"):
        plan = request(c, "POST", "/api/runs/plan", json=body(new_id, mode))
        expected_reused = old_n * len(reusable) if mode == "incremental" else 0
        assert plan["total"] == 4 and plan["mode"] == mode
        assert plan["without_previous"] == 4 - old_n
        assert plan["reused"] == expected_reused and plan["evaluated"] == 4 * n - expected_reused, plan
        assert plan["model_calls"] == (4 - old_n if mode == "incremental" and len(reusable) == n else 4), plan
        run = execute(c, body(new_id, mode))
        assert all(run[key] == plan[key] for key in ("model_calls", "evaluated", "reused")), (run, plan)
        evaluations = readable_evaluations(c, run, config)
        diff = check_diff(c, run, previous, evaluations)
        for cid, ev in evaluations.items():
            expected_ids = reusable if mode == "incremental" and cid in previous else set()
            assert set(ev["reused"]) == expected_ids
            assert set(ev["evaluated"]) == {k["id"] for k in config["criteria"]} - expected_ids
            for k in ev["criteria"]:
                assert k["reused_from"] == (previous[cid]["id"] if k["id"] in expected_ids else None)
            review(c, config, ev, target)
        # Решения относятся к своей оценке: исходные ответы и diff неизменны.
        assert request(c, "GET", f"/api/runs/{run['id']}/diff") == diff
        previous, old_n, reusable = evaluations, 4, {k["id"] for k in config["criteria"]}

    # Песочница сначала ровно как в редакторе, затем явно только целевой критерий.
    before_runs = request(c, "GET", "/api/runs")
    before_versions = request(c, "GET", "/api/checklists")
    before_evaluations = {cid: request(c, "GET", f"/api/conversations/{cid}")["evaluations"] for cid in IDS}
    for selection in ({}, {"criteria": [target], "conversation_ids": IDS[:3]}):
        trial = request(c, "POST", "/api/checklists/try", json={"config": config, "provider": "fake", **selection})
        assert trial["model"] == trial["provider"] == "fake" and len(trial["items"]) == 3
        for item in trial["items"]:
            assert item["error"] is None, item
            assert target in {k["id"] for k in item["criteria"]}
            if selection:
                assert [k["id"] for k in item["criteria"]] == [target]
            for k in item["criteria"]:
                old = next(x for x in previous[item["id"]]["criteria"] if x["id"] == k["id"])
                assert k["answer"] in old["allowed"] and k["previous_answer"] == old["original"]["answer"]
    hint = request(c, "POST", "/api/checklists/assist", json={"config": draft, "criterion_id": target})
    assert hint["guidance"].strip() and isinstance(hint["warnings"], list) and hint["model"] == "fake"
    assert request(c, "GET", "/api/runs") == before_runs
    assert request(c, "GET", "/api/checklists") == before_versions
    assert {cid: request(c, "GET", f"/api/conversations/{cid}")["evaluations"] for cid in IDS} == before_evaluations
    for item in initial["items"]:
        old = request(c, "GET", f"/api/evaluations/{item['evaluation_id']}")
        assert old["checklist"]["id"] == 1 and all(k["appeal"] is None for k in old["criteria"])
