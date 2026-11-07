"""Прецеденты: без сети и DEV, с настоящей SQLite и заглушкой судьи."""
import asyncio
import copy
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cqa import api, judge, precedents, runner, sandbox
from cqa.checklist import load_default
from cqa.db import DB, SCHEMA
from cqa.incremental import fingerprints
from cqa.judge import prompt
from cqa.judge.providers import FakeProvider


def record(cid, text):
    return {"conversation_id": cid, "channel": "chat", "language": "RU", "transcript": [
        {"speaker": "bot", "text": "БОТ НЕ ДОЛЖЕН ПОПАСТЬ В ПРЕЦЕДЕНТ", "timestamp": "00:00"},
        {"speaker": "client", "text": text, "timestamp": "00:01"},
        {"speaker": "operator", "text": "Здравствуйте! Меня зовут Анна.", "timestamp": "00:02"},
        {"speaker": "system", "text": "Чат закрыт", "timestamp": "00:03"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = load_default()
    db = DB(tmp_path / "precedents.db")
    db.seed_default_checklist()
    records = [record("target", "Купить акции комиссия биржа"),
               record("near", "Купить акции комиссия биржа"),
               record("far", "Забыл пароль восстановить доступ")]
    db.save_dataset("TEST", "fake", records)
    labels = {c["id"]: {"answer": "Нет", "explanation": "Эталон [2], затем [3–4] (реплики 2, 3)."}
              for c in cfg["criteria"]}
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"dialogs": {cid: labels for cid in ("target", "near", "far", "absent")}}))
    monkeypatch.setattr(precedents, "REFERENCE", reference)
    monkeypatch.setenv("CQA_PRECEDENTS", "2")
    yield db, cfg
    db.close()


def examples(db, cfg, cid="target", **kwargs):
    return precedents.select(db, db.record(cid), cfg, **kwargs)


def reviewed(db, cfg, cid="near", *, status="changed", new_answer="Да", model="fake", definition=None):
    result = asyncio.run(judge.evaluate(db.record(cid), definition or cfg, FakeProvider()))
    if model != "fake":
        for key, value in result["answers"].items():
            value["fingerprint"] = fingerprints(definition or cfg, model)[key]
    ev_id = db.insert_evaluation({**result, "model": model, "conversation_id": cid, "checklist_id": 1})
    appeal_id = db.create_appeal(ev_id, "01", "Проверьте приветствие")
    if status != "open":
        db.decide_appeal(appeal_id, status, new_answer, "Решение проверяющего [3]")
    return ev_id


def test_self_excluded_missing_transcript_excluded_and_similarity_order(setup):
    db, cfg = setup
    selected = examples(db, cfg, k=100)
    assert [p["conversation_id"] for p in selected] == ["near", "far"]
    assert all(set(p) == {"conversation_id", "channel", "language", "transcript", "labels", "source"}
               for p in selected)
    assert selected[0]["transcript"] == db.conversation("near")["transcript"]
    assert all(p["source"] == "reference" for p in selected)


def test_similarity_uses_lowercase_words_and_ignores_bot_and_system(setup):
    db, cfg = setup
    records = [db.record(cid) for cid in ("target", "near", "far")]
    records[1]["transcript"][1]["text"] = "КУПИТЬ, акции! КОМИССИЯ? биржа."
    for index in (0, 2):
        misleading = records[2 - index]["transcript"][1]["text"] * 100
        records[index]["transcript"][0]["text"] = misleading
        records[index]["transcript"][3]["text"] = misleading
    db.save_dataset("TEST", "fake", records)
    assert [p["conversation_id"] for p in examples(db, cfg)] == ["near", "far"]


@pytest.mark.parametrize("field,value", [("title", "Другое название"), ("type", "flag"), ("id", "new")])
def test_reference_labels_require_same_id_title_type(setup, field, value):
    db, cfg = setup
    cfg["criteria"][0][field] = value
    cfg["criteria"].append({**cfg["criteria"][1], "id": "new-criterion"})
    selected = examples(db, cfg)
    assert all(set(p["labels"]) == {c["id"] for c in cfg["criteria"][1:-1]} for p in selected)
    text = precedents.block(selected, cfg)
    assert "- 01 " not in text and "- new-criterion " not in text


@pytest.mark.parametrize("status,new_answer", [("changed", "Да"), ("confirmed", None), ("decided", "Да")])
def test_reviewer_overrides_reference_and_preserves_other_model_answers(setup, status, new_answer):
    db, cfg = setup
    ev_id = reviewed(db, cfg, status=status, new_answer=new_answer, model="another-model")
    selected = examples(db, cfg)[0]
    assert selected["source"] == "reviewer"
    assert selected["labels"]["01"]["answer"] == "Да"
    assert selected["labels"]["01"]["explanation"] == "Решение проверяющего [3]"
    assert selected["labels"]["02"] == db.evaluation(ev_id)["answers"]["02"]
    assert "Решение проверяющего\n" in precedents.block([selected], cfg)


@pytest.mark.parametrize("change", ["title", "guidance", "rules", "new", "prompt"])
def test_reviewer_labels_require_current_fingerprints(setup, monkeypatch, change):
    db, cfg = setup
    reviewed(db, cfg)
    if change == "rules":
        cfg["rules"].append("Новое правило")
    elif change == "new":
        cfg["criteria"].append({**cfg["criteria"][0], "id": "new"})
    elif change == "prompt":
        monkeypatch.setattr(prompt, "PROMPT_VERSION", "next")
    else:
        cfg["criteria"][0][change] += " изменено"
    selected = examples(db, cfg)[0]
    assert selected["source"] == "reviewer"  # не откатываемся к эталону при несовместимости
    expected = set() if change in ("rules", "prompt") else {c["id"] for c in load_default()["criteria"]}
    if change in ("title", "guidance"):
        expected.remove("01")
    assert set(selected["labels"]) == expected


def test_only_latest_successful_evaluation_can_supply_reviewer_labels(setup):
    db, cfg = setup
    old_id = reviewed(db, cfg)
    db.insert_evaluation({**db.evaluation(old_id), "status": "error"})
    assert examples(db, cfg)[0]["source"] == "reviewer"
    db.insert_evaluation({**db.evaluation(old_id), "model": "different"})
    assert examples(db, cfg)[0]["source"] == "reference"


def test_open_appeals_are_not_precedents_but_decisions_refresh_index(setup):
    db, cfg = setup
    ev_id = reviewed(db, cfg, status="open")
    assert examples(db, cfg)[0]["source"] == "reference"
    appeal = db.appeals_for(ev_id)[0]
    db.decide_appeal(appeal["id"], "changed", "Да", "Утверждено")
    assert examples(db, cfg)[0]["source"] == "reviewer"
    db.ex("DELETE FROM conversations WHERE id='near'")
    assert all(p["conversation_id"] != "near" for p in examples(db, cfg))


def test_reviewer_dialog_outside_reference_is_also_a_precedent(setup):
    db, cfg = setup
    db.save_dataset("TEST", "fake", [record("reviewer-only", "Купить акции комиссия биржа")])
    reviewed(db, cfg, "reviewer-only")
    selected = {p["conversation_id"]: p for p in examples(db, cfg, k=10)}
    assert selected["reviewer-only"]["source"] == "reviewer"
    assert selected["reviewer-only"]["labels"]["01"]["answer"] == "Да"


def test_latest_decision_reason_wins_and_inputs_not_mutated(setup):
    db, cfg = setup
    ev_id = reviewed(db, cfg)
    appeal = db.create_appeal(ev_id, "01", "Ещё раз")
    db.decide_appeal(appeal, "changed", "Нет", "Позднее решение")
    before = copy.deepcopy(db.evaluation(ev_id))
    selected = examples(db, cfg)[0]
    assert selected["labels"]["01"]["answer"] == "Нет"
    assert selected["labels"]["01"]["explanation"] == "Позднее решение"
    assert db.evaluation(ev_id) == before


def test_block_matches_prototype_without_bot_numbering_or_references(setup):
    db, cfg = setup
    text = precedents.block(examples(db, cfg), cfg)
    assert text.startswith("Прецеденты — похожие диалоги")
    assert "оценки прецедентов механически не переноси" in text
    assert "ссылайся только на номера реплик оцениваемого диалога" in text
    assert "--- Прецедент 1: chat · RU\nклиент: Купить акции" in text
    assert "оператор: Здравствуйте!" in text and "событие: Чат закрыт" in text
    assert "Утверждённая оценка:\n- 01 Установление контакта: Нет — Эталон, затем." in text
    assert "БОТ НЕ ДОЛЖЕН" not in text and not re.search(r"\[\d", text)
    assert "(реплики" not in text
    assert text.endswith("=== Оцениваемый диалог ===\n")


class RecordingFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.users = []

    async def complete(self, system, user, schema, **kwargs):
        self.users.append(user)
        return await super().complete(system, user, schema, **kwargs)


@pytest.mark.parametrize("k", [0, 1, 2])
def test_environment_and_evaluate_block_placement(setup, monkeypatch, k):
    db, cfg = setup
    monkeypatch.setenv("CQA_PRECEDENTS", str(k))
    selected = examples(db, cfg)
    assert len(selected) == k
    provider = RecordingFake()
    result = asyncio.run(judge.evaluate(db.record("target"), cfg, provider, precedents=selected))
    user, = provider.users
    assert result["status"] == "ok" and result["prompt_version"] == "p2"
    normal = prompt.build_user(db.record("target"))
    assert user.endswith(normal)
    assert ("=== Оцениваемый диалог ===" in user) == bool(k)
    if not k:
        assert user == normal and precedents.block(selected, cfg) == ""


def test_no_available_precedents_is_empty(setup):
    db, cfg = setup
    db.ex("DELETE FROM conversations")
    assert precedents.select(db, record("x", "запрос"), cfg) == []


def test_precedent_transcripts_follow_pii_setting_without_mutating_input(setup):
    db, cfg = setup
    selected = examples(db, cfg)
    selected[0]["transcript"][1]["text"] = "Моя почта test@example.com"
    before = copy.deepcopy(selected)
    for mask_pii in (True, False):
        provider = RecordingFake()
        asyncio.run(judge.evaluate(db.record("target"), cfg, provider, precedents=selected, mask_pii=mask_pii))
        assert ("test@example.com" in provider.users[0]) is not mask_pii
    assert selected == before


@pytest.mark.parametrize("votes,retry", [(1, False), (3, False), (3, True)])
def test_runner_passes_same_precedents_to_every_vote_and_retry_and_persists(setup, monkeypatch, votes, retry):
    db, cfg = setup
    reviewed(db, cfg)
    original_evaluate, original_select = judge.evaluate, precedents.select
    calls, selections = [], []

    def select(*args):
        selected = original_select(*args)
        selections.append(selected)
        return selected

    async def evaluate(rec, config, provider, *, precedents):
        calls.append(precedents)
        if retry and len(calls) <= votes:
            return {"status": "error", "error": "429", "answers": {}}
        return await original_evaluate(rec, config, provider, precedents=precedents)

    monkeypatch.setattr(precedents, "select", select)
    monkeypatch.setattr(judge, "evaluate", evaluate)
    monkeypatch.setattr(runner, "RETRY_PAUSES", (0,))
    run_id = db.create_run(1, "fake", "fake", ["target"])
    asyncio.run(runner.Runner(db, votes=votes)._one(
        run_id, "target", cfg, 1, FakeProvider(), asyncio.Semaphore(2)))
    assert len(selections) == 1 and len(calls) == votes * (2 if retry else 1)
    assert all(call is selections[0] for call in calls)
    ev = db.evaluation(db.run_items(run_id)[0]["evaluation_id"])
    assert ev["status"] == "ok" and ev["model_calls"] == len(calls)
    assert ev["precedents"] == [{"conversation_id": "near", "source": "reviewer"},
                                {"conversation_id": "far", "source": "reference"}]


@pytest.mark.parametrize("retry", [False, True])
def test_sandbox_passes_precedents_once_per_dialog_including_retry_without_writes(setup, monkeypatch, retry):
    db, cfg = setup
    original_evaluate, original_select = judge.evaluate, precedents.select
    selections, calls = [], []

    def select(*args):
        selected = original_select(*args)
        selections.append(selected)
        return selected

    async def evaluate(rec, config, provider, *, precedents):
        calls.append((rec["conversation_id"], precedents))
        if retry and sum(cid == rec["conversation_id"] for cid, _ in calls) == 1:
            return {"status": "error", "error": "429", "answers": {}}
        return await original_evaluate(rec, config, provider, precedents=precedents)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(precedents, "select", select)
    monkeypatch.setattr(judge, "evaluate", evaluate)
    monkeypatch.setattr(sandbox.asyncio, "sleep", no_sleep)
    before = list(db.conn.iterdump())
    result = asyncio.run(sandbox.preview(db, cfg, FakeProvider(), ["target", "far"]))
    assert len(result["items"]) == 2 and all(item["error"] is None for item in result["items"])
    assert len(selections) == 2 and len(calls) == (4 if retry else 2)
    for i, cid in enumerate(("target", "far")):
        assert all(selected is selections[i] for called, selected in calls if called == cid)
        assert all(p["conversation_id"] != cid for p in selections[i])
    assert list(db.conn.iterdump()) == before


def test_api_returns_precedents_and_defaults_for_old_evaluations(setup, tmp_path, monkeypatch):
    db, cfg = setup
    monkeypatch.setattr(api, "read_env", lambda *args: None)
    app = api.create_app(db.path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    selected = [{"conversation_id": "near", "source": "reviewer"}]
    result = asyncio.run(judge.evaluate(db.record("target"), cfg, FakeProvider()))
    ev = {**result, "conversation_id": "target", "checklist_id": 1}
    with TestClient(app) as client:
        for expected in ([], selected):
            if expected:
                ev["precedents"] = expected
            ev_id = db.insert_evaluation(ev)
            response = client.get(f"/api/evaluations/{ev_id}")
            assert response.status_code == 200, response.text
            assert response.json()["precedents"] == expected
    app.state.db.close()


def test_existing_database_migration_preserves_evaluations(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO evaluations(conversation_id, answers) VALUES('old', '{}')")
    db = DB(path)
    try:
        assert db.evaluation(1)["precedents"] == []
        columns = {c["name"]: c for c in db.q("PRAGMA table_info(evaluations)")}
        assert columns["precedents"]["type"] == "TEXT"
        assert columns["precedents"]["dflt_value"] == "'[]'"
    finally:
        db.close()
