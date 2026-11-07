"""Сверка черновика: временная БД, строгая схема и фейковый провайдер без сети."""
import asyncio
from collections import Counter
import copy
import json

import pytest
from fastapi.testclient import TestClient

from cqa import api, crittest, judge, precedents
from cqa.checklist import load_default
from cqa.db import DB
from cqa.judge.providers import FakeProvider, Reply


class RecordingFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.active = self.max_active = 0
        self.fail = set()
        self.invalid = set()

    async def complete(self, system, user, schema, **kwargs):
        self.calls.append((system, user, copy.deepcopy(schema)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if any(cid in user for cid in self.fail):
                raise RuntimeError("Провайдер недоступен")
            if any(cid in user for cid in self.invalid):
                return Reply(text="не JSON", model="fake", input_tokens=1000, output_tokens=200)
            reply = await super().complete(system, user, schema, **kwargs)
            reply.input_tokens, reply.output_tokens = 1000, 200
            return reply
        finally:
            self.active -= 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    provider = RecordingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: provider)
    monkeypatch.setattr(api, "read_env", lambda key, default=None: default)
    # Ненулевая тестовая цена проверяет сохранение и суммирование стоимости.
    monkeypatch.setitem(judge.PROVIDERS["fake"], "price_in", 1)
    monkeypatch.setitem(judge.PROVIDERS["fake"], "price_out", 5)
    records = [{"conversation_id": f"test-{i:02}", "language": "RU", "channel": "chat",
                "transcript": [{"timestamp": "00:00", "speaker": "operator",
                                "text": f"Диалог test-{i:02}. ИИН 900101300007"}]} for i in range(30)]
    labels = {r["conversation_id"]: {"02": {"answer": ["Да", "Нет", "N/A"][i // 10]},
                                     "05": {"answer": ["Да", "Нет"][i % 2]}}
              for i, r in enumerate(records)}
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"dialogs": labels}), encoding="utf-8")
    monkeypatch.setattr(precedents, "REFERENCE", reference)
    app = api.create_app(tmp_path / "test.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    db = app.state.db
    db.save_dataset("DEV", "test", records)
    with TestClient(app) as client:
        yield client, db, provider
    db.close()


def draft(*keys):
    cfg = load_default()
    for c in cfg["criteria"]:
        if c["id"] in keys:
            c["guidance"] += " Дополнение для проверки."
    return cfg


def request(client, config=None, **kwargs):
    return client.post("/api/checklists/check-approved",
                       json={"config": config or draft("02"), "provider": "fake", **kwargs})


def protected_snapshot(db):
    return {table: db.q(f"SELECT * FROM {table}") for table in
            ("evaluations", "runs", "run_items", "appeals", "checklists", "conversations", "settings", "datasets")}


def test_balanced_default_limit_partial_schema_parallel_cost_and_no_evaluation_writes(setup):
    client, db, provider = setup
    before = protected_snapshot(db)
    r = request(client)
    assert r.status_code == 200, r.text
    result = r.json()
    row, = result["criteria"]
    assert row["id"] == "02" and row["matched"] == 4 and row["total"] == row["sampled"] == 12
    assert row["available"] == 30 and len(row["disagreements"]) == 8
    labels = crittest.approved_answers(db, load_default())
    assert Counter(labels[cid]["02"] for cid in result["conversation_ids"]) == {"Да": 4, "Нет": 4, "N/A": 4}
    assert len(provider.calls) == result["model_calls"] == 12 and provider.max_active == 12
    for system, user, schema in provider.calls:
        assert schema["additionalProperties"] is False
        assert schema["properties"]["criteria"]["required"] == ["02"]
        assert "900101300007" not in user and "[ИИН скрыт]" in user
        assert "Утверждённая оценка" not in user  # без утечки эталона
        assert draft("02")["criteria"][1]["guidance"] in system
    assert result["cost_usd"] == 0.024 and result["latency_s"] >= 0
    calls = client.get("/api/checklist-checks/calls").json()
    assert len(calls) == 12 and sum(c["input_tokens"] for c in calls) == 12000
    assert all(c["status"] == "ok" and c["cost_usd"] == 0.002 and c["criteria"] == ["02"] for c in calls)
    assert protected_snapshot(db) == before
    for d in row["disagreements"]:
        assert d["approved_answer"] in ("Нет", "N/A") and d["answer"] == "Да"
        assert d["explanation"] and d["evidence"] == [1] and db.conversation(d["conversation_id"])


def test_multiple_criteria_share_one_call_and_total_limit(setup):
    client, db, provider = setup
    result = request(client, draft("02", "05"), limit=6).json()
    assert len(result["conversation_ids"]) == len(set(result["conversation_ids"])) == 6
    assert len(provider.calls) == 6
    assert all(call[2]["properties"]["criteria"]["required"] == ["02", "05"] for call in provider.calls)
    labels = crittest.approved_answers(db, load_default())
    assert Counter(labels[cid]["02"] for cid in result["conversation_ids"]) == {"Да": 2, "Нет": 2, "N/A": 2}
    assert Counter(labels[cid]["05"] for cid in result["conversation_ids"]) == {"Да": 3, "Нет": 3}


def test_criteria_is_filter_of_changed_definitions(setup):
    client, db, provider = setup
    result = request(client, draft("02", "05"), criteria=["02", "02", "01"], limit=2).json()
    assert [c["id"] for c in result["criteria"]] == ["02"]
    assert all(call[2]["properties"]["criteria"]["required"] == ["02"] for call in provider.calls)


def test_weights_sections_and_order_do_not_call_model(setup):
    client, db, provider = setup
    cfg = load_default()
    cfg["criteria"][1]["weight"] += 1
    cfg["criteria"][1]["section"] = "Другая секция"
    cfg["criteria"].reverse()
    cfg["name"] = "Другое название"
    before = protected_snapshot(db)
    result = request(client, cfg).json()
    assert result["criteria"] == [] and result["model_calls"] == 0 and result["cost_usd"] == 0
    assert result["message"] and not provider.calls and protected_snapshot(db) == before


def test_latest_saved_version_is_baseline(setup):
    client, db, provider = setup
    cfg = draft("02")
    new_id, _ = db.create_checklist(cfg)
    result = request(client, cfg).json()
    assert result["checklist_id"] == new_id and result["criteria"] == [] and not provider.calls


def test_global_rules_change_all_definitions(setup):
    client, db, provider = setup
    cfg = load_default()
    cfg["rules"].append("Новая общая инструкция")
    result = request(client, cfg, limit=1).json()
    assert [c["id"] for c in result["criteria"]] == [c["id"] for c in cfg["criteria"]]
    assert len(provider.calls) == 1
    assert provider.calls[0][2]["properties"]["criteria"]["required"] == ["02", "05"]


def test_new_criterion_and_missing_reference_without_provider_call(setup, monkeypatch):
    client, db, provider = setup
    cfg = load_default()
    new = copy.deepcopy(cfg["criteria"][0])
    new["id"] = "09"
    cfg["criteria"].append(new)
    monkeypatch.setattr(judge, "available", lambda pid: False)
    result = request(client, cfg).json()
    row, = result["criteria"]
    assert row["id"] == "09" and row["available"] == row["total"] == 0
    assert row["message"] == crittest.NO_APPROVED and not provider.calls and not db.criterion_test_calls()


def evaluation(db, cid="test-00", answers=None):
    return db.insert_evaluation({"conversation_id": cid, "checklist_id": 1, "model": "fake", "status": "ok",
                                 "answers": answers or {"02": {"answer": "Да"}, "05": {"answer": "Нет"}}})


def decision(db, eid, key, answer=None, status="changed"):
    aid = db.create_appeal(eid, key, "Не согласен")
    db.decide_appeal(aid, status, answer, "Решение проверяющего")
    return aid


def test_reviewer_overrides_only_reviewed_criterion_and_confirmation_preserves_change(setup):
    client, db, provider = setup
    eid = evaluation(db)
    decision(db, eid, "02", "Нет")
    decision(db, eid, "02", status="confirmed")
    evaluation(db)  # новый автоматический прогон не отменяет утверждение
    labels = crittest.approved_answers(db, load_default())
    assert labels["test-00"] == {"02": "Нет", "05": "Да"}
    newer = evaluation(db)
    decision(db, newer, "02", "N/A")
    assert crittest.approved_answers(db, load_default())["test-00"]["02"] == "N/A"


def test_confirm_original_open_ignored_and_no_labels_from_unreviewed_model_answers(setup):
    client, db, provider = setup
    eid = evaluation(db, "test-01")
    db.create_appeal(eid, "02", "Открытая")
    decision(db, eid, "05", status="confirmed")
    labels = crittest.approved_answers(db, load_default())
    assert labels["test-01"] == {"02": "Да", "05": "Нет"}
    assert "01" not in labels["test-01"]


def test_old_incompatible_appeal_does_not_override_reference(setup):
    client, db, provider = setup
    eid = evaluation(db)
    decision(db, eid, "02", "Нет")
    active = draft("02")
    db.create_checklist(active)
    assert crittest.approved_answers(db, active)["test-00"]["02"] == "Да"


def test_reviewer_labels_without_dev_reference(setup):
    client, db, provider = setup
    eid = evaluation(db, answers={"01": {"answer": "Нет"}})
    decision(db, eid, "01", status="confirmed")
    result = request(client, draft("01")).json()
    row, = result["criteria"]
    assert row["available"] == row["total"] == 1 and row["matched"] == 0
    assert row["disagreements"][0]["approved_answer"] == "Нет"


def test_removed_answer_remains_in_reference_and_counts_as_disagreement(setup):
    client, db, provider = setup
    cfg = draft("02")
    cfg["criteria"][1]["na"] = False
    result = request(client, cfg, limit=3).json()
    row, = result["criteria"]
    assert row["total"] == 3 and any(d["approved_answer"] == "N/A" for d in row["disagreements"])
    assert "N/A" not in provider.calls[0][2]["properties"]["criteria"]["properties"]["02"]["properties"]["answer"]["enum"]


def test_partial_errors_excluded_from_agreement_and_all_costs_logged(setup):
    client, db, provider = setup
    provider.invalid = {"test-00"}
    result = request(client, limit=3).json()
    row, = result["criteria"]
    assert row["total"] == 2 and row["sampled"] == 3 and len(row["errors"]) == 1
    assert row["errors"][0]["conversation_id"] == "test-00"
    assert result["cost_usd"] == 0.006 and len(provider.calls) == 3
    assert Counter(c["status"] for c in db.criterion_test_calls()) == {"ok": 2, "error": 1}


def test_all_provider_exceptions_logged_without_retries_or_false_agreement(setup):
    client, db, provider = setup
    provider.fail = {"test-"}
    r = request(client, limit=3)
    assert r.status_code == 200
    row, = r.json()["criteria"]
    assert row["total"] == row["matched"] == 0 and len(row["errors"]) == 3
    assert len(provider.calls) == 3 and len(db.criterion_test_calls()) == 3
    assert all(c["status"] == "error" and "RuntimeError" in c["error"] for c in db.criterion_test_calls())


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
                                   {"limit": "12"}, {"criteria": []}, {"criteria": ["missing"]},
                                   {"provider": "missing"}, {"config": {"criteria": []}},
                                   {"config": {"criteria": "wrong"}}])
def test_invalid_requests_do_not_call_or_write(setup, kwargs):
    client, db, provider = setup
    before = protected_snapshot(db)
    r = request(client, **kwargs)
    assert r.status_code == 422, r.text
    assert r.json()["errors"] and not provider.calls
    assert protected_snapshot(db) == before and not db.criterion_test_calls()


def test_limit_above_corpus_and_missing_conversations(setup):
    client, db, provider = setup
    db.ex("DELETE FROM conversations WHERE id='test-00'")
    result = request(client, limit=100).json()
    assert len(result["conversation_ids"]) == 29 and "test-00" not in result["conversation_ids"]
    assert result["criteria"][0]["available"] == 29


def test_unavailable_provider_fails_before_writing(setup, monkeypatch):
    client, db, provider = setup
    monkeypatch.setattr(judge, "available", lambda pid: False)
    r = request(client)
    assert r.status_code == 422 and not provider.calls and not db.criterion_test_calls()


def test_fake_is_hidden_without_explicit_allowance(tmp_path):
    app = api.create_app(tmp_path / "hidden.db", allow_fake=False)
    with TestClient(app) as client:
        assert request(client).status_code == 422
    app.state.db.close()


def test_table_migration_is_idempotent_and_preserves_existing_data(tmp_path):
    path = tmp_path / "old.db"
    db = DB(path)
    db.seed_default_checklist()
    eid = evaluation(db)
    snapshot = protected_snapshot(db)
    db.ex("DROP TABLE criterion_test_calls")
    db.close()
    for _ in range(2):
        db = DB(path)
        assert protected_snapshot(db) == snapshot and db.evaluation(eid)
        assert db.criterion_test_calls() == []
        db.close()
