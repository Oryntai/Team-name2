"""Инкрементальная оценка: только fake, временные базы, без сети и внешних наборов."""
import asyncio
import copy
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from cqa import api, judge, runner
from cqa.checklist import load_default
from cqa.db import DB, SCHEMA
from cqa.incremental import fingerprints
from cqa.judge.providers import FakeProvider, ProviderError


RECORD = {"conversation_id": "one", "channel": "chat", "language": "RU", "transcript": [
    {"timestamp": "00:00", "speaker": "operator", "text": "Здравствуйте!"}]}


class CountingFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.answers = {"01": "Нет"}
        self.failures = 0

    async def complete(self, system, user, schema, **kwargs):
        self.calls.append((system, user, schema))
        if self.failures:
            self.failures -= 1
            raise ProviderError("Временная ошибка fake")
        reply = await super().complete(system, user, schema)
        data = json.loads(reply.text)
        for cid, item in data["criteria"].items():
            if cid in self.answers:
                item["answer"] = self.answers[cid]
        reply.text = json.dumps(data)
        return reply


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = DB(tmp_path / "incremental.db")
    db.seed_default_checklist()
    db.save_dataset("TEST", "fake", [RECORD])
    provider = CountingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: provider)
    monkeypatch.setattr(runner, "RETRY_PAUSES", (0, 0, 0))
    yield db, provider
    db.close()


def run(db, checklist_id=1, mode="incremental"):
    async def execute():
        r = runner.Runner(db)
        rid = r.start(["one"], checklist_id, "fake", mode)
        await r.wait()
        return rid
    rid = asyncio.run(execute())
    assert db.run(rid)["failed"] == 0
    return db.evaluation(db.run_items(rid)[0]["evaluation_id"])


def new_config(db, change):
    cfg = db.checklist(1)["config"]
    change(cfg)
    return db.create_checklist(cfg, parent_id=1)[0]


def test_evaluate_only_keeps_full_system_and_filters_answers():
    p = CountingFake()
    cfg = load_default()
    res = asyncio.run(judge.evaluate(RECORD, cfg, p, only=["05"]))
    assert res["status"] == "ok" and set(res["answers"]) == {"05"}
    system, user, schema = p.calls[0]
    assert system == judge.build_system(cfg)
    assert "Оцени только критерии: 05. Остальные не оценивай" in user
    assert schema["properties"]["criteria"]["required"] == ["05"]
    assert set(schema["properties"]["criteria"]["properties"]) == {"05"}
    assert res["answers"]["05"]["fingerprint"] == fingerprints(cfg, "fake")["05"]
    assert res["answers"]["05"]["reused_from"] is None
    empty = asyncio.run(judge.evaluate(RECORD, cfg, p, only=[]))
    assert empty["answers"] == {} and len(p.calls) == 1
    with pytest.raises(ValueError):
        asyncio.run(judge.evaluate(RECORD, cfg, p, only=["unknown"]))


def test_weights_recalculate_without_model(setup):
    db, p = setup
    old = run(db)
    cl = new_config(db, lambda c: c["criteria"][0].update(weight=20, section="Другая секция"))
    p.calls.clear()
    ev = run(db, cl)
    assert not p.calls and ev["model_calls"] == 0 and ev["cost_usd"] == 0
    assert ev["input_tokens"] == ev["output_tokens"] == ev["latency_s"] == 0
    assert ev["mode"] == "incremental" and ev["evaluated"] == [] and len(ev["reused"]) == 8
    assert old["total"] == 90 and ev["total"] == 81.82
    assert {a["reused_from"] for a in ev["answers"].values()} == {old["id"]}
    for cid, a in ev["answers"].items():
        assert {**old["answers"][cid], "reused_from": old["id"]} == a


def test_one_definition_changes_only_one_answer(setup):
    db, p = setup
    old = run(db)
    cl = new_config(db, lambda c: c["criteria"][4].update(yes="Новое определение вежливости."))
    p.calls.clear()
    p.answers["05"] = "Нет"
    ev = run(db, cl)
    assert len(p.calls) == 1
    assert len(p.calls[0][2]["properties"]["criteria"]["required"]) == 8  # полный вызов: критерий в контексте всего чек-листа
    assert ev["evaluated"] == ["05"] and len(ev["reused"]) == 7
    assert ev["answers"]["05"]["answer"] == "Нет"
    assert ev["answers"]["05"]["reused_from"] is None
    for cid in ev["reused"]:
        assert ev["answers"][cid] == {**old["answers"][cid], "reused_from": old["id"]}
    # Источник следующего переноса — последняя оценка, даже если она сама была составной.
    again = run(db, cl)
    assert again["model_calls"] == 0
    assert {a["reused_from"] for a in again["answers"].values()} == {ev["id"]}


def test_rules_and_full_mode_evaluate_all(setup):
    db, p = setup
    run(db)
    cl = new_config(db, lambda c: c["rules"].append("Новое общее правило."))
    p.calls.clear()
    ev = run(db, cl)
    assert len(p.calls) == 1 and len(ev["evaluated"]) == 8 and ev["reused"] == []
    ev = run(db, cl, "full")
    assert len(p.calls) == 2 and ev["mode"] == "full" and len(ev["evaluated"]) == 8
    assert "Оцени только критерии" not in p.calls[-1][1]


@pytest.mark.parametrize("field,value", [
    ("id", "new"), ("title", "Новое название"), ("type", "scale"), ("yes", "Да иначе"),
    ("no", "Нет иначе"), ("na", True), ("na_when", "Иное условие"),
    ("steps", [{"value": 1, "text": "Новая ступень"}]), ("guidance", "Иное пояснение"),
])
def test_all_definition_fields_affect_fingerprint(field, value):
    cfg = load_default()
    old = fingerprints(cfg, "fake")["01"]
    cfg["criteria"][0][field] = value
    assert fingerprints(cfg, "fake")[cfg["criteria"][0]["id"]] != old


def test_model_and_prompt_version_affect_fingerprint(monkeypatch):
    from cqa.judge import prompt
    cfg = load_default()
    old = fingerprints(cfg, "fake")
    assert fingerprints(cfg, "other") != old
    monkeypatch.setattr(prompt, "PROMPT_VERSION", "new")
    assert fingerprints(cfg, "fake") != old


def test_incremental_retries_and_counts_all_calls(setup):
    db, p = setup
    run(db)
    cl = new_config(db, lambda c: c["criteria"][4].update(no="Новая формулировка Нет."))
    p.calls.clear()
    p.failures = 2
    ev = run(db, cl)
    assert ev["model_calls"] == len(p.calls) == 3
    assert all(len(c[2]["properties"]["criteria"]["required"]) == 8 for c in p.calls)
    assert db.run_counts(ev["run_id"]) == {"model_calls": 3, "evaluated": 1, "reused": 7}


def test_failed_incremental_keeps_source_and_retry_limit(setup):
    db, p = setup
    old = run(db)
    cl = new_config(db, lambda c: c["criteria"][4].update(title="Другое название"))
    p.calls.clear()
    p.failures = 4

    async def execute():
        r = runner.Runner(db)
        rid = r.start(["one"], cl, "fake", "incremental")
        await r.wait()
        return rid

    rid = asyncio.run(execute())
    ev = db.evaluation(db.run_items(rid)[0]["evaluation_id"])
    assert ev["status"] == "error" and ev["total"] is None
    assert ev["model_calls"] == 4 and ev["evaluated"] == ["05"] and len(ev["reused"]) == 7
    assert db.latest_success("one", "fake")["id"] == old["id"]
    assert db.run(rid)["failed"] == 1


def test_latest_success_skips_errors_and_other_model(setup):
    db, p = setup
    old = run(db)
    db.insert_evaluation({**old, "model": "other", "answers": {}})
    db.insert_evaluation({**old, "status": "error", "answers": {}})
    p.calls.clear()
    ev = run(db)
    assert not p.calls
    assert {a["reused_from"] for a in ev["answers"].values()} == {old["id"]}


def test_migration_and_legacy_without_hash_reassessed(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    db = DB(path)
    db.seed_default_checklist()
    db.save_dataset("TEST", "fake", [RECORD])
    p = CountingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: p)
    old = run(db, mode="full")
    answers = copy.deepcopy(old["answers"])
    for a in answers.values():
        a.pop("fingerprint")
    db.insert_evaluation({**old, "answers": answers})
    ev = run(db)
    assert ev["model_calls"] == 1 and len(ev["evaluated"]) == 8
    db.close()
    # Повторное открытие не пытается добавить колонки ещё раз.
    DB(path).close()


def test_api_plan_diff_and_appeals_stay_on_original(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "read_env", lambda *args: None)
    p = CountingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: p)
    app = api.create_app(tmp_path / "api.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    app.state.db.save_dataset("TEST", "fake", [RECORD])
    with TestClient(app) as c:
        body = {"dataset_id": "TEST", "checklist_id": 1, "provider": "fake"}

        def execute(b):
            response = c.post("/api/runs", json=b)
            assert response.status_code == 200, response.text
            rid = response.json()["run_id"]
            for _ in range(200):
                r = c.get(f"/api/runs/{rid}").json()
                if r["status"] != "running":
                    assert r["failed"] == 0
                    return r
                time.sleep(0.005)
            pytest.fail("Прогон не завершился")

        plan = c.post("/api/runs/plan", json=body).json()
        assert plan["without_previous"] == plan["model_calls"] == 1 and plan["evaluated"] == 8
        assert app.state.db.list_runs() == [] and not p.calls
        initial = execute(body)
        old_id = initial["items"][0]["evaluation_id"]
        appeal = c.post("/api/appeals", json={"evaluation_id": old_id, "criterion_id": "01", "comment": "Есть приветствие"}).json()
        c.post(f"/api/appeals/{appeal['id']}/decision", json={"decision": "change", "new_answer": "Да", "reason": "Реплика 1"})
        cfg = c.get("/api/checklists/1").json()["config"]
        cfg["criteria"][0]["weight"] = 20
        cl = c.post("/api/checklists", json={"config": cfg, "parent_id": 1}).json()["id"]
        body["checklist_id"] = cl
        plan = c.post("/api/runs/plan", json=body).json()
        assert plan["model_calls"] == plan["evaluated"] == 0 and plan["reused"] == 8
        assert plan["criteria"][0]["weight_changed"] == 1
        weighted = execute(body)
        assert weighted["model_calls"] == weighted["evaluated"] == 0 and weighted["reused"] == 8
        diff = c.get(f"/api/runs/{weighted['id']}/diff").json()
        weighted_diff = copy.deepcopy(diff)
        item = diff["items"][0]
        assert item["previous"]["id"] == old_id and item["previous"]["total"] == 90
        assert item["current"]["total"] == 81.82 and item["changes"] == []
        ev = c.get(f"/api/evaluations/{item['current']['id']}").json()
        assert ev["criteria"][0]["final_answer"] == "Нет" and ev["criteria"][0]["appeal"] is None
        assert ev["criteria"][0]["reused_from"] == old_id
        assert c.get(f"/api/evaluations/{old_id}").json()["final"]["total"] == 100

        cfg["criteria"][4]["yes"] = "Новое определение"
        body["checklist_id"] = c.post("/api/checklists", json={"config": cfg, "parent_id": cl}).json()["id"]
        plan = c.post("/api/runs/plan", json=body).json()
        assert plan["evaluated"] == plan["model_calls"] == 1 and plan["reused"] == 7
        assert plan["criteria"][4]["definition_changed"] == 1
        p.answers["05"] = "Нет"
        changed = execute(body)
        diff = c.get(f"/api/runs/{changed['id']}/diff").json()
        assert diff["items"][0]["changes"] == [{"id": "05", "before": "Да", "after": "Нет"}]
        assert next(x for x in diff["criteria"] if x["id"] == "05")["changed"] == 1
        assert changed["evaluated"] == changed["model_calls"] == 1 and changed["reused"] == 7
        assert c.get(f"/api/runs/{weighted['id']}/diff").json() == weighted_diff
        full_plan = c.post("/api/runs/plan", json={**body, "mode": "full"}).json()
        assert full_plan["evaluated"] == 8 and full_plan["reused"] == 0
        # Смешанный набор: старый диалог переносится, новый требует полной оценки.
        app.state.db.save_dataset("TEST", "fake", [{**RECORD, "conversation_id": "new"}])
        mixed = c.post("/api/runs/plan", json=body).json()
        assert mixed["total"] == 2 and mixed["without_previous"] == 1
        assert mixed["model_calls"] == 1 and mixed["evaluated"] == mixed["reused"] == 8
        assert all(x["evaluated"] == x["reused"] == 1 for x in mixed["criteria"])
        # Неудачная переоценка не изображает отсутствующий ответ как изменение.
        monkeypatch.setattr(runner, "RETRY_PAUSES", (0, 0, 0))
        p.failures = 4
        response = c.post("/api/runs", json={**body, "conversation_ids": ["one"], "mode": "full"})
        rid = response.json()["run_id"]
        for _ in range(200):
            if c.get(f"/api/runs/{rid}").json()["status"] != "running":
                break
            time.sleep(0.005)
        failed_diff = c.get(f"/api/runs/{rid}/diff").json()
        assert failed_diff["items"][0]["current"]["status"] == "error"
        assert failed_diff["items"][0]["changes"] == []
        for path in ("/api/runs", "/api/runs/plan"):
            assert c.post(path, json={**body, "mode": "wrong"}).status_code == 422
        assert c.get("/api/runs/999/diff").status_code == 404
    app.state.db.close()
