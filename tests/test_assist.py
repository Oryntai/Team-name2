"""Подсказка методисту: строгая схема, полный контекст, API без сети и сохранения."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa.assist import SCHEMA, draft_guidance
from cqa.checklist import load_default
from cqa.judge.providers import FakeProvider, ProviderError, Reply


@pytest.mark.parametrize("criterion_id", ["05", "04", "03", "08"])
def test_fake_guidance_matches_schema(criterion_id):
    config = load_default()
    before = copy.deepcopy(config)
    result = asyncio.run(draft_guidance(config, criterion_id, FakeProvider()))
    assert set(result) == {"guidance", "warnings", "na_when_suggestion", "model", "cost_usd", "latency_s"}
    assert isinstance(result["guidance"], str) and result["guidance"].strip()
    assert isinstance(result["warnings"], list) and all(isinstance(w, str) for w in result["warnings"])
    assert result["na_when_suggestion"] is None or isinstance(result["na_when_suggestion"], str)
    assert result["model"] == "fake" and result["cost_usd"] == 0 and result["latency_s"] >= 0
    assert config == before


def test_prompt_contains_target_and_other_criteria():
    config = load_default()
    config["criteria"][4].update(title="Новый критерий", yes="Новая формулировка", guidance="")

    class RecordingProvider(FakeProvider):
        calls = 0

        async def complete(self, system, user, schema, max_tokens=0):
            self.calls += 1
            assert schema == SCHEMA and schema["additionalProperties"] is False
            assert set(schema["required"]) == {"guidance", "warnings", "na_when_suggestion"}
            assert "до 700 символов" in system and "кроме отметки грубых ошибок" in system
            checklist, target = user.removeprefix("Весь чек-лист:\n").split("\nЦелевой критерий:\n")
            checklist, target = json.loads(checklist), json.loads(target)
            assert target["id"] == "05" and target["title"] == "Новый критерий"
            assert checklist["rules"] == config["rules"]
            for original, sent in zip(config["criteria"], checklist["criteria"], strict=True):
                for key in ("id", "title", "type", "weight", "yes", "no", "na", "na_when", "steps", "guidance"):
                    assert sent[key] == original[key]
            return await super().complete(system, user, schema, max_tokens)

    provider = RecordingProvider()
    asyncio.run(draft_guidance(config, "05", provider))
    assert provider.calls == 1


def test_na_suggestion_and_usage_metadata():
    class MeteredProvider(FakeProvider):
        async def complete(self, *args, **kwargs):
            return Reply(text=json.dumps({"guidance": "Проверьте условие. Оцените оператора. Укажите доказательства.",
                                          "warnings": ["Уточните условие N/A."],
                                          "na_when_suggestion": "Клиент завершил диалог до уточнения запроса."}),
                         model="test-model", input_tokens=1000, output_tokens=500, cache_read=100, cache_write=200)

    result = asyncio.run(draft_guidance(load_default(), "02", MeteredProvider("haiku")))
    assert result["na_when_suggestion"] == "Клиент завершил диалог до уточнения запроса."
    assert result["model"] == "test-model" and result["cost_usd"] == 0.000376


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: default)
    app = api_module.create_app(tmp_path / "assist.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    with TestClient(app) as c:
        yield c
    app.state.db.close()


def test_api_returns_guidance_without_saving(client):
    before = client.get("/api/checklists").json()
    config = load_default()
    config["criteria"][4].update(title="Несохранённый критерий", na=True, na_when="", guidance="")
    response = client.post("/api/checklists/assist", json={"config": config, "criterion_id": "05", "provider": "fake"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"guidance", "warnings", "na_when_suggestion", "model", "cost_usd", "latency_s"}
    assert result["guidance"] and result["warnings"] and result["model"] == "fake"
    assert result["cost_usd"] == 0 and result["latency_s"] >= 0
    assert client.get("/api/checklists").json() == before
    assert client.get("/api/checklists/1").json()["config"] == load_default()


def test_api_uses_default_provider(client):
    client.app.state.db.set_setting("default_provider", "fake")
    response = client.post("/api/checklists/assist", json={"config": load_default(), "criterion_id": "05"})
    assert response.status_code == 200 and response.json()["model"] == "fake"


@pytest.mark.parametrize("config, criterion_id", [
    (load_default(), "missing"), ({}, "05"), ({"criteria": "bad"}, "05"),
    ({"criteria": [None]}, "05"), ({"criteria": [{"id": []}]}, "05"),
    ({"criteria": [{"id": "05"}, {"id": "05"}]}, "05"),
])
def test_invalid_target_rejected_before_provider(client, monkeypatch, config, criterion_id):
    def unexpected_provider(*args):
        pytest.fail("Для неверного запроса провайдер вызываться не должен")

    monkeypatch.setattr(api_module.judge, "get_provider", unexpected_provider)
    response = client.post("/api/checklists/assist", json={"config": config, "criterion_id": criterion_id})
    assert response.status_code == 422
    assert response.json()["detail"] and response.json()["errors"]


def test_unknown_and_unavailable_provider(client, monkeypatch):
    body = {"config": load_default(), "criterion_id": "05", "provider": "missing"}
    assert client.post("/api/checklists/assist", json=body).status_code == 422
    monkeypatch.setattr(api_module.judge, "available", lambda pid: False)
    body["provider"] = "haiku"
    response = client.post("/api/checklists/assist", json=body)
    assert response.status_code == 422 and response.json()["errors"]


@pytest.mark.parametrize("text", ["not json", "[]", "{}",
    '{"guidance":7,"warnings":[],"na_when_suggestion":null}',
    '{"guidance":"Текст","warnings":"ошибка","na_when_suggestion":null}',
    '{"guidance":"Текст","warnings":[1],"na_when_suggestion":null}',
    '{"guidance":"Текст","warnings":[],"na_when_suggestion":false}',
    json.dumps({"guidance": "Текст", "warnings": [], "na_when_suggestion": None, "extra": 1}),
])
def test_invalid_model_output_is_project_error(client, monkeypatch, text):
    async def complete(*args, **kwargs):
        return Reply(text=text, model="fake")

    provider = FakeProvider()
    monkeypatch.setattr(provider, "complete", complete)
    monkeypatch.setattr(api_module.judge, "get_provider", lambda pid: provider)
    response = client.post("/api/checklists/assist", json={"config": load_default(), "criterion_id": "05", "provider": "fake"})
    assert response.status_code == 502
    assert response.json()["detail"] and response.json()["errors"]


def test_provider_error_is_project_error(client, monkeypatch):
    async def complete(*args, **kwargs):
        raise ProviderError("Провайдер недоступен")

    provider = FakeProvider()
    monkeypatch.setattr(provider, "complete", complete)
    monkeypatch.setattr(api_module.judge, "get_provider", lambda pid: provider)
    response = client.post("/api/checklists/assist", json={"config": load_default(), "criterion_id": "05", "provider": "fake"})
    assert response.status_code == 502 and response.json()["errors"] == ["Провайдер недоступен"]


def test_long_guidance_is_accepted(client, monkeypatch):
    """700 символов — пожелание в промпте, а не повод показать ошибку вместо подсказки."""
    async def complete(*args, **kwargs):
        return Reply(text=json.dumps({"guidance": "я" * 900, "warnings": [], "na_when_suggestion": None}), model="fake")

    provider = FakeProvider()
    monkeypatch.setattr(provider, "complete", complete)
    monkeypatch.setattr(api_module.judge, "get_provider", lambda pid: provider)
    r = client.post("/api/checklists/assist", json={"config": load_default(), "criterion_id": "05", "provider": "fake"})
    assert r.status_code == 200 and len(r.json()["guidance"]) == 900
