"""Слепая калибровка: fake, временная SQLite, без сети и изменения оценок ИИ."""
import asyncio
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from cqa import judge
from cqa.api import create_app
from cqa.db import DB
from cqa.runner import Runner
from cqa.scoring import score


RECORD = {"conversation_id": "cal-one", "channel": "agent chat", "language": "RU", "transcript": [
    {"timestamp": "00:00", "speaker": "client", "text": "Помогите проверить перевод"},
    {"timestamp": "00:03", "speaker": "operator", "text": "Здравствуйте!"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "cal.db"
    app = create_app(path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    db = app.state.db
    db.save_dataset("TEST", "fake", [RECORD, {**RECORD, "conversation_id": "unrated"}])

    async def evaluate():
        runner = Runner(db, votes=1)
        rid = runner.start(["cal-one"], 1, "fake", "full")
        await runner.wait()
        return db.evaluation(db.run_items(rid)[0]["evaluation_id"])

    ev = asyncio.run(evaluate())
    assert ev["status"] == "ok"

    def forbid_provider(*args, **kwargs):
        pytest.fail("Калибровка не должна вызывать модель")

    monkeypatch.setattr(judge, "get_provider", forbid_provider)
    with TestClient(app) as client:
        yield client, db, ev, path
    db.close()


def answers(ev):
    return {cid: a["answer"] for cid, a in ev["answers"].items()}


def save(client, ans, **kwargs):
    return client.post("/api/calibrations", json={"conversation_id": "cal-one", "answers": ans, **kwargs})


def test_save_compare_persist_and_leave_model_untouched(setup):
    client, db, ev, path = setup
    tables = ["evaluations", "runs", "run_items", "checklists", "appeals"]
    before = {t: db.q(f"SELECT * FROM {t}") for t in tables}
    human = {**answers(ev), "02": "N/A", "03": "N/A", "06": "7", "08": "Нет"}
    response = save(client, human, reviewer="  Жюри  ", comment="  Проверка  ")
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["evaluation_id"] == ev["id"] and result["checklist_id"] == 1
    assert result["human"] == {"total": 93.08, "gross": False}
    assert result["model"] == {"total": 100.0, "gross": True}
    assert result["delta"] == -6.92
    assert result["reviewer"] == "Жюри" and result["comment"] == "Проверка" and result["created_at"]
    by = {c["id"]: c for c in result["criteria"]}
    assert by["01"]["matched"] is True
    assert by["06"]["human_answer"] == "7" and by["06"]["model_answer"] == "10"
    assert by["06"]["matched"] is False and by["06"]["explanation"] and by["06"]["evidence"]
    assert client.get("/api/calibrations?conversation_id=cal-one").json()["items"] == [result]
    assert {t: db.q(f"SELECT * FROM {t}") for t in tables} == before
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT answers, reviewer, comment FROM calibrations WHERE id=?", (result["id"],)).fetchone()
    assert json.loads(row[0]) == human and row[1:] == ("Жюри", "Проверка")


@pytest.mark.parametrize("changes,remove", [
    ({"01": "N/A"}, None), ({"01": "да"}, None), ({"01": True}, None),
    ({"01": None}, None), ({"01": " Да "}, None), ({"04": "17"}, None),
    ({"04": 15}, None), ({"08": "N/A"}, None), ({"99": "Да"}, None), ({}, "01"),
])
def test_invalid_answers_rejected_without_writing(setup, changes, remove):
    client, db, ev, _ = setup
    human = {**answers(ev), **changes}
    if remove:
        human.pop(remove)
    response = save(client, human)
    assert response.status_code == 422, response.text
    assert response.json()["errors"]
    assert db.list_calibrations() == []


def test_missing_conversation_or_model_and_invalid_checklist(setup):
    client, db, ev, _ = setup
    assert save(client, answers(ev), conversation_id="missing").status_code == 404
    assert save(client, answers(ev), conversation_id="unrated").status_code == 409
    assert save(client, answers(ev), checklist_id=999).status_code == 422
    clid, _ = db.create_checklist(db.checklist(1)["config"], parent_id=1)
    assert save(client, answers(ev), checklist_id=clid).status_code == 409
    assert db.list_calibrations() == []


def test_latest_success_same_checklist_and_historical_comparison(setup):
    client, db, ev, _ = setup
    changed = {**ev["answers"], "05": {**ev["answers"]["05"], "answer": "Нет"}}
    second = db.insert_evaluation({**ev, "answers": changed, "total": 85})
    cfg2 = db.checklist(1)["config"]
    cfg2["criteria"][0]["title"] = "Новое определение"
    clid, _ = db.create_checklist(cfg2, parent_id=1)
    third = db.insert_evaluation({**ev, "checklist_id": clid})
    db.insert_evaluation({**ev, "status": "error", "error": "fake failure"})
    result = save(client, answers(ev), checklist_id=1).json()
    assert result["evaluation_id"] == second and result["model"]["total"] == 85
    assert save(client, answers(ev)).json()["evaluation_id"] == third
    # Новый прогон не меняет сохранённую пару человек–ИИ.
    db.insert_evaluation(ev)
    history = client.get("/api/calibrations?conversation_id=cal-one").json()
    assert history["items"][-1] == result
    assert len(history["summary"]["criteria"]) == 16  # версии не смешаны
    assert {c["checklist_id"] for c in history["summary"]["criteria"]} == {1, clid}


def test_appeals_do_not_replace_model_answers(setup):
    client, _, ev, _ = setup
    appeal = client.post("/api/appeals", json={"evaluation_id": ev["id"], "criterion_id": "05", "comment": "Не согласен"}).json()
    decision = client.post(f"/api/appeals/{appeal['id']}/decision", json={
        "decision": "change", "new_answer": "Нет", "reason": "Проверено человеком"})
    assert decision.status_code == 200 and decision.json()["evaluation"]["final"]["total"] == 85
    result = save(client, {**answers(ev), "05": "Нет"}).json()
    c05 = next(c for c in result["criteria"] if c["id"] == "05")
    assert c05["model_answer"] == "Да" and c05["matched"] is False
    assert result["model"]["total"] == 100 and result["human"]["total"] == 85


def test_list_filters_and_summary(setup):
    client, db, ev, _ = setup
    first = save(client, {**answers(ev), "05": "Нет"}, reviewer="А").json()
    second = save(client, answers(ev), reviewer="Б").json()
    db.save_dataset("OTHER", "fake", [{**RECORD, "conversation_id": "other"}])
    db.insert_evaluation({**ev, "conversation_id": "other", "total": 85,
                          "answers": {**ev["answers"], "05": {**ev["answers"]["05"], "answer": "Нет"}}})
    assert save(client, answers(ev), conversation_id="other").status_code == 201
    history = client.get("/api/calibrations?dataset_id=TEST").json()
    assert history["items"] == [second, first]
    summary = history["summary"]
    assert summary["count"] == summary["scored_count"] == 2
    assert summary["mean_delta"] == -7.5 and summary["mean_absolute_delta"] == 7.5
    by = {c["id"]: c for c in summary["criteria"]}
    assert (by["05"]["matched"], by["05"]["total"]) == (1, 2)
    assert (by["01"]["matched"], by["01"]["total"]) == (2, 2)
    assert client.get("/api/calibrations?conversation_id=other").json()["summary"]["count"] == 1
    all_summary = client.get("/api/calibrations").json()["summary"]
    assert all_summary["mean_delta"] == 0 and all_summary["mean_absolute_delta"] == 10
    empty = client.get("/api/calibrations?conversation_id=other&dataset_id=TEST").json()
    assert empty == {"items": [], "summary": {"count": 0, "criteria": [], "scored_count": 0,
                                               "mean_delta": None, "mean_absolute_delta": None}}


def test_no_applicable_score_and_flag(setup):
    client, db, ev, _ = setup
    cfg = {"name": "Только N/A", "criteria": [
        {"id": "q", "type": "bool", "title": "Вопрос", "yes": "Да", "weight": 100, "na": True, "na_when": "Не применимо"},
        {"id": "f", "type": "flag", "title": "Ошибка", "yes": "Ошибка есть", "weight": 0, "na": False}]}
    clid, _ = db.create_checklist(cfg)
    db.insert_evaluation({**ev, "checklist_id": clid, "answers": {"q": {"answer": "N/A"}, "f": {"answer": "Нет"}}, "total": None})
    result = save(client, {"q": "N/A", "f": "Да"}).json()
    assert result["human"] == {"total": None, "gross": True}
    assert result["model"] == {"total": None, "gross": False}
    assert result["delta"] is None
    summary = client.get("/api/calibrations").json()["summary"]
    assert summary["count"] == 1 and summary["scored_count"] == 0 and summary["mean_delta"] is None


def test_custom_scale_allowed_values_and_formula(setup):
    client, db, ev, _ = setup
    cfg = db.checklist(1)["config"]
    c04 = next(c for c in cfg["criteria"] if c["id"] == "04")
    c04["steps"] = [{"value": 3, "text": "Полностью"}, {"value": 1.5, "text": "Частично"}, {"value": 0, "text": "Нет"}]
    clid, _ = db.create_checklist(cfg)
    model = {**answers(ev), "04": "3"}
    db.insert_evaluation({**ev, "checklist_id": clid, "answers": {k: {"answer": v} for k, v in model.items()}})
    assert save(client, {**model, "04": "15"}).status_code == 422
    human = {**model, "04": "1.5"}
    result = save(client, human).json()
    assert result["human"]["total"] == score(cfg, human)["total"] == 90
    assert result["model"]["total"] == 100


def test_incomplete_model_does_not_save(setup):
    client, db, ev, _ = setup
    db.insert_evaluation({**ev, "answers": {}})
    assert save(client, answers(ev)).status_code == 409
    assert db.list_calibrations() == []


def test_existing_database_migration_is_idempotent(setup):
    client, db, ev, path = setup
    saved = save(client, answers(ev)).json()
    before = db.q("SELECT * FROM evaluations")
    with db.lock, db.conn:
        db.conn.execute("DROP TABLE calibrations")
    for _ in range(2):
        reopened = DB(path)
        assert reopened.list_calibrations() == []
        assert reopened.q("SELECT * FROM evaluations") == before
        reopened.close()
    assert save(client, answers(ev)).status_code == 201
    assert saved["evaluation_id"] == ev["id"]


def test_card_hides_model_and_waits_for_saved_comparison(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("Для проверки веб-компонента нужен Node.js")
    client, db, ev, _ = setup
    detail = client.get(f"/api/evaluations/{ev['id']}").json()
    comparison = save(client, {**answers(ev), "05": "Нет"}).json()
    payload = {"ev": detail, "cfg": db.checklist(1)["config"], "comparison": comparison,
               "conv": client.get("/api/conversations/cal-one").json()}
    script = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
globalThis.HTMLElement = class {};
let Card;
globalThis.customElements = { define: (name, type) => { Card = type; } };
globalThis.window = {};
const { fmtScore } = await import('./web/js/util.js');
await import('./web/js/components/card.js');
const { ev, cfg, comparison, conv } = JSON.parse(readFileSync(0, 'utf8'));
const card = new Card();
card._connected = true;
card.convId = 'cal-one';
card.s = { ev, cfg, conv,
  meta: {}, checklists: [], evals: [{ evaluation_id: ev.id, total: 9876.54 }, { evaluation_id: 999, total: 9876.54 }],
  blind: false, comparison: null, rerunOpen: false, active: null, calAnswers: {}, run: null };
card.querySelectorAll = () => [];
card.querySelector = () => null;
card.hasAttribute = () => false;
card.bindAudio = () => {};
ev.criteria[0].original.explanation = 'MODEL_EXPLANATION_SECRET';
ev.criteria[0].original.answer = 'MODEL_ANSWER_SECRET';
card.startCalibration();
assert.equal(card.s.blind, true);
assert.deepEqual(card.s.calAnswers, {});
assert(!card.innerHTML.includes('MODEL_EXPLANATION_SECRET'));
assert(!card.innerHTML.includes('MODEL_ANSWER_SECRET'));
assert(!card.innerHTML.includes(fmtScore(9876.54))); // история оценок в шапке тоже скрыта
assert(!card.innerHTML.includes('data-mark='));
assert(!card.innerHTML.includes('data-act="thinking-open"'));
assert(!card.innerHTML.includes('data-act="rerun-toggle"'));
assert(card.innerHTML.includes('data-cal-criterion="04"'));
assert(card.innerHTML.includes('data-part="cal-submit" disabled'));
card.s.calAnswers = Object.fromEntries(comparison.criteria.map(c => [c.id, c.human_answer]));
let finish;
Object.defineProperty(card, 'api', { value: { createCalibration: body => {
  assert.equal(body.checklist_id, ev.checklist.id);
  assert.deepEqual(body.answers, card.s.calAnswers);
  return new Promise(resolve => { finish = resolve; });
} } });
globalThis.FormData = class { get() { return ''; } };
const pending = card.submitCalibration({ querySelectorAll: () => [] });
assert.equal(card.s.blind, true); // до ответа сервера ИИ не раскрывается
finish(comparison);
await pending;
assert.equal(card.s.blind, false);
assert.equal(card.s.comparison, comparison);
assert(card.innerHTML.includes('Совпало 7 из 8'));
assert(card.innerHTML.includes('data-matched="false"'));
assert(card.innerHTML.includes('data-ref="1"'));
assert(!card.innerHTML.includes('data-mark='));
card.s.comparison = null;
card.s.ev = null;
card.s.cfg = null;
card.render();
assert(card.innerHTML.includes('data-act="cal-start" disabled'));
assert(card.innerHTML.includes('Нужна успешная оценка ИИ'));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(payload),
                            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
