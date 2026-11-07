"""Песочница: несохранённый чек-лист, только fake, временная база и никакой сети."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from cqa import api, judge
from cqa.checklist import load_default
from cqa.judge.providers import FakeProvider, ProviderError


RECORDS = [{"conversation_id": f"c{i}", "language": language, "channel": "chat",
            "transcript": [{"timestamp": "00:00", "speaker": "operator", "text": "Здравствуйте!"}]}
           for i, language in enumerate(["RU", "RU", "RU", "KZ", "EN"])]


class RecordingFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.answers = {}
        self.active = self.max_active = 0

    async def complete(self, system, user, schema, **kwargs):
        self.calls.append((system, user, schema))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            reply = await super().complete(system, user, schema, **kwargs)
            data = json.loads(reply.text)
            for cid, item in data["criteria"].items():
                item["answer"] = self.answers.get(cid, item["answer"])
            reply.text = json.dumps(data)
            reply.input_tokens, reply.output_tokens = 1000, 200
            return reply
        finally:
            self.active -= 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "read_env", lambda key, default=None: default)
    provider = RecordingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: provider)
    app = api.create_app(tmp_path / "try.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    app.state.db.save_dataset("TEST", "fake", RECORDS)
    with TestClient(app) as client:
        yield client, app.state.db, provider
    app.state.db.close()


def previous(db, provider, ids=("c0",)):
    config = load_default()
    for cid in ids:
        result = asyncio.run(judge.evaluate(db.record(cid), config, provider))
        db.insert_evaluation({**result, "conversation_id": cid, "checklist_id": 1})
    provider.calls.clear()
    provider.max_active = 0


def request(client, **kwargs):
    return client.post("/api/checklists/try", json={"config": load_default(), "provider": "fake", **kwargs})


def test_changed_criterion_with_previous_answer_and_no_writes(setup):
    client, db, provider = setup
    previous(db, provider)
    config = load_default()
    config["criteria"][4]["yes"] = "Новое требование вежливости."
    provider.answers["05"] = "Нет"
    before = list(db.conn.iterdump())
    changes = db.conn.total_changes
    response = request(client, config=config, conversation_ids=["c0"])
    assert response.status_code == 200, response.text
    result = response.json()
    item, = result["items"]
    assert (item["id"], item["language"], item["channel"]) == ("c0", "RU", "chat")
    criterion, = item["criteria"]
    assert criterion == {"id": "05", "title": config["criteria"][4]["title"], "answer": "Нет",
                         "previous_answer": "Да", "explanation": "Тестовый ответ заглушки по критерию 05 [1].",
                         "evidence": [1]}
    assert len(provider.calls) == 1
    assert len(provider.calls[0][2]["properties"]["criteria"]["required"]) == 8  # полный вызов, показан только 05
    assert result["model"] == result["provider"] == "fake"
    assert result["cost_usd"] == 0 and result["latency_s"] >= 0
    assert db.conn.total_changes == changes and list(db.conn.iterdump()) == before


def test_without_previous_evaluates_all_and_selects_different_languages(setup):
    client, db, provider = setup
    before = list(db.conn.iterdump())
    response = request(client)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["c0", "c3", "c4"]
    assert {item["language"] for item in items} == {"RU", "KZ", "EN"}
    assert len(provider.calls) == 3
    for item in items:
        assert [c["id"] for c in item["criteria"]] == [c["id"] for c in load_default()["criteria"]]
        assert all(c["previous_answer"] is None for c in item["criteria"])
    assert list(db.conn.iterdump()) == before


def test_default_prefers_successes_of_same_model(setup):
    client, db, provider = setup
    previous(db, provider, ("c1", "c2", "c3", "c4"))
    # Более новая оценка другой модели и ошибка не заменяют прошлую успешную fake.
    old = db.latest_success("c1", "fake")
    db.insert_evaluation({**old, "model": "other", "answers": {}})
    db.insert_evaluation({**old, "status": "error", "answers": {}})
    config = load_default()
    config["criteria"][0]["guidance"] = "Изменённое пояснение."
    response = request(client, config=config)
    assert response.status_code == 200, response.text
    assert [i["id"] for i in response.json()["items"]] == ["c1", "c3", "c4"]
    assert all([c["id"] for c in i["criteria"]] == ["01"] for i in response.json()["items"])
    assert all(i["criteria"][0]["previous_answer"] == "Да" for i in response.json()["items"])


@pytest.mark.parametrize("change", [False, True])
def test_no_definition_changes_evaluates_all(setup, change):
    client, db, provider = setup
    previous(db, provider)
    config = load_default()
    if change:
        config["criteria"][0]["weight"] += 5
    response = request(client, config=config, conversation_ids=["c0"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"][0]["criteria"]) == len(config["criteria"])
    assert len(provider.calls) == 1


def test_explicit_ids_are_unique_limited_to_three_and_parallel(setup, monkeypatch):
    client, db, provider = setup
    monkeypatch.setitem(judge.PROVIDERS, "fake", {**judge.PROVIDERS["fake"], "price_in": 1, "price_out": 2})
    response = request(client, conversation_ids=[" c4 ", "c4", "c3", "c2", "c1", "c0"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["id"] for item in result["items"]] == ["c4", "c3", "c2"]
    assert len(provider.calls) == provider.max_active == 3
    assert result["cost_usd"] == 0.0042


def test_explicit_criteria_override_changes_and_default_provider(setup):
    client, db, provider = setup
    previous(db, provider)
    db.set_setting("default_provider", "fake")
    config = load_default()
    config["criteria"][4]["yes"] = "Другое определение."
    response = request(client, config=config, provider=None, criteria=["01"], conversation_ids=["c0", "c4"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert all([c["id"] for c in item["criteria"]] == ["01"] for item in items)
    assert items[0]["criteria"][0]["previous_answer"] == "Да"
    assert items[1]["criteria"][0]["previous_answer"] is None


@pytest.mark.parametrize("config", [{}, {"criteria": "bad"}, {"criteria": {"id": "01"}},
                                    {"criteria": [None]}, {"criteria": []}])
def test_invalid_checklist_is_422_without_provider_call(setup, config):
    client, db, provider = setup
    response = request(client, config=config)
    assert response.status_code == 422
    assert response.json()["detail"] and response.json()["errors"]
    assert not provider.calls


def test_invalid_criterion_definition_is_422(setup):
    client, db, provider = setup
    config = copy.deepcopy(load_default())
    config["criteria"][4].update(na=True, na_when="")
    response = request(client, config=config)
    assert response.status_code == 422 and response.json()["errors"]
    assert not provider.calls


@pytest.mark.parametrize("fields", [{"criteria": ["unknown"]}, {"criteria": []},
                                   {"conversation_ids": ["missing"]}, {"conversation_ids": []},
                                   {"provider": "missing"}])
def test_invalid_selection_is_422(setup, fields):
    client, db, provider = setup
    response = request(client, **fields)
    assert response.status_code == 422 and response.json()["errors"]
    assert not provider.calls


def test_empty_dataset_and_unavailable_provider_are_422(setup, monkeypatch):
    client, db, provider = setup
    db.ex("DELETE FROM conversations")
    assert request(client).status_code == 422
    monkeypatch.setattr(judge, "available", lambda pid: False)
    response = request(client)
    assert response.status_code == 422 and response.json()["errors"]
    assert not provider.calls


@pytest.mark.parametrize("invalid_json", [False, True])
def test_model_failure_is_502_without_writes(setup, monkeypatch, invalid_json):
    client, db, provider = setup

    async def complete(*args, **kwargs):
        if invalid_json:
            reply = await FakeProvider().complete(*args, **kwargs)
            reply.text = "invalid JSON"
            return reply
        raise ProviderError("Тестовый сбой fake")

    monkeypatch.setattr(provider, "complete", complete)
    before = list(db.conn.iterdump())
    response = request(client, conversation_ids=["c0"])
    assert response.status_code == 502
    assert response.json()["errors"] and "c0" in response.json()["detail"]
    assert list(db.conn.iterdump()) == before
