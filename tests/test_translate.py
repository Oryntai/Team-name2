"""Перевод для проверяющего: без сети, оценки и results.json не затрагиваются."""
import asyncio
import copy
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa.db import DB
from cqa.judge.providers import ProviderError, Reply
from cqa.translate import FakeTranslationProvider, SYSTEM, response_schema, translate


RECORD = {"conversation_id": "kz-chat", "channel": "agent chat", "language": "KZ", "transcript": [
    {"timestamp": "00:00", "speaker": "client", "text": "Сәлем! ИИН 900101300007, a@example.com"},
    {"timestamp": "00:01", "speaker": "bot", "text": "Connecting"},
    {"timestamp": "00:01", "speaker": "system", "text": "Assigned"},
    {"timestamp": "00:02", "speaker": "operator", "text": "Hello, card 4111 1111 1111 1111. 100 USD AAPL"},
    {"timestamp": "00:03", "speaker": "client", "text": ""},
]}
DATA = [{"turn": i, "text": "Перевод" if i < 5 else ""} for i in range(1, 6)]


class MeteredProvider:
    pid, model = "haiku", "test-haiku"

    def __init__(self, text=None, error=None):
        self.text = json.dumps(DATA, ensure_ascii=False) if text is None else text
        self.error = error
        self.calls = []

    async def complete(self, system, user, schema, max_tokens=16000):
        self.calls.append((system, user, schema))
        if self.error:
            raise self.error
        return Reply(text=self.text, model=self.model, input_tokens=1000, output_tokens=500,
                     cache_read=100, cache_write=200)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: default)
    app = api_module.create_app(tmp_path / "translation.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    app.state.db.save_dataset("TEST", "test", [copy.deepcopy(RECORD)])
    with TestClient(app) as c:
        yield c
    app.state.db.close()


def install_provider(monkeypatch, provider):
    requested = []

    def get_provider(pid):
        requested.append(pid)
        return provider

    monkeypatch.setattr(api_module.judge, "get_provider", get_provider)
    monkeypatch.setattr(api_module.judge, "available", lambda pid: True)
    return requested


def test_translation_masks_pii_and_keeps_original_array(client, monkeypatch):
    provider = MeteredProvider()
    requested = install_provider(monkeypatch, provider)
    db = client.app.state.db
    before = db.record("kz-chat")
    response = client.post("/api/conversations/kz-chat/translate")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert requested == ["haiku"] and len(provider.calls) == 1
    assert saved["translation"]["data"] == DATA
    assert saved["translation"]["lang"] == "ru"
    assert saved["translation"]["conversation_id"] == "kz-chat"
    assert saved["translation"]["model"] == provider.model
    assert saved["translation"]["created_at"]
    assert saved["cost_usd"] == saved["translation"]["cost_usd"] == 0.000376
    assert saved["model_calls"] == 1
    system, user, schema = provider.calls[0]
    assert system == SYSTEM and schema == response_schema(5)
    assert schema["type"] == "array" and schema["items"]["additionalProperties"] is False
    assert schema["minItems"] == 1 and "maxItems" not in schema
    sent = json.loads(user)
    assert [t["turn"] for t in sent] == list(range(1, 6))
    assert [t["speaker"] for t in sent] == [t["speaker"] for t in RECORD["transcript"]]
    assert sent[-1]["text"] == ""
    for secret in ("900101300007", "a@example.com", "4111 1111 1111 1111"):
        assert secret not in user
    for label in ("[ИИН скрыт]", "[e-mail скрыт]", "[номер карты скрыт]", "100 USD AAPL"):
        assert label in user
    assert db.record("kz-chat") == before


def test_cache_survives_reopening_database(client, monkeypatch, tmp_path):
    provider = MeteredProvider()
    install_provider(monkeypatch, provider)
    url = "/api/conversations/kz-chat/translate"
    first = client.post(url).json()
    assert client.post(url).json() == first
    assert client.get("/api/conversations/kz-chat/translation").json() == first
    app = api_module.create_app(tmp_path / "translation.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=False)
    monkeypatch.setattr(api_module.judge, "available", lambda pid: False)
    with TestClient(app) as reopened:
        assert reopened.get("/api/conversations/kz-chat/translation").json() == first
        assert reopened.post(url).json() == first
    app.state.db.close()
    assert len(provider.calls) == 1


def test_missing_translation_and_missing_conversation(client):
    assert client.get("/api/conversations/kz-chat/translation").json() == {
        "translation": None, "cost_usd": 0, "model_calls": 0}
    for method, suffix in ((client.get, "translation"), (client.post, "translate")):
        r = method(f"/api/conversations/missing/{suffix}")
        assert r.status_code == 404 and r.json()["errors"]


@pytest.mark.parametrize("language", ["RU", "ru", " Ru "])
def test_russian_rejected_without_model(client, monkeypatch, language):
    record = copy.deepcopy(RECORD)
    record["language"] = language
    client.app.state.db.save_dataset("TEST", "test", [record])
    provider = MeteredProvider()
    install_provider(monkeypatch, provider)
    assert client.post("/api/conversations/kz-chat/translate").status_code == 422
    assert not provider.calls


def test_empty_transcript_rejected(client, monkeypatch):
    record = {**RECORD, "transcript": []}
    client.app.state.db.save_dataset("TEST", "test", [record])
    provider = MeteredProvider()
    install_provider(monkeypatch, provider)
    assert client.post("/api/conversations/kz-chat/translate").status_code == 422
    assert not provider.calls


@pytest.mark.parametrize("text", ["not JSON", "{}", "null", "[]", json.dumps(DATA[:-1]),
    json.dumps([*DATA, {"turn": 6, "text": "лишнее"}]),
    json.dumps([{**DATA[0], "extra": 1}, *DATA[1:]]),
    json.dumps([{**DATA[0], "turn": True}, *DATA[1:]]),
    json.dumps([{**DATA[0], "turn": 1.0}, *DATA[1:]]),
    json.dumps([{**DATA[0], "turn": "1"}, *DATA[1:]]),
    json.dumps([{**DATA[0], "turn": 0}, *DATA[1:]]),
    json.dumps([{**DATA[0], "turn": 2}, *DATA[1:]]),
    json.dumps([{**DATA[0], "text": None}, *DATA[1:]]),
    json.dumps([{**DATA[0], "text": 2}, *DATA[1:]]),
    json.dumps([None, *DATA[1:]]), json.dumps([{"turn": 1}, *DATA[1:]]),
    json.dumps(list(reversed(DATA)))])
def test_invalid_output_logged_with_cost_and_retryable(client, monkeypatch, text):
    provider = MeteredProvider(text=text)
    install_provider(monkeypatch, provider)
    response = client.post("/api/conversations/kz-chat/translate")
    assert response.status_code == 502 and response.json()["errors"]
    assert len(provider.calls) == 1  # внутри перевода нет повторных попыток
    failed = client.get("/api/conversations/kz-chat/translation").json()
    assert failed == {"translation": None, "cost_usd": 0.000376, "model_calls": 1}
    assert client.app.state.db.q("SELECT * FROM translation_calls")[0]["status"] == "error"
    provider.text = json.dumps(DATA)
    retried = client.post("/api/conversations/kz-chat/translate").json()
    assert retried["translation"]["data"] == DATA and retried["model_calls"] == 2
    assert retried["cost_usd"] == 0.000752


def test_provider_failure_is_logged_without_invented_cost(client, monkeypatch):
    provider = MeteredProvider(error=ProviderError("offline"))
    install_provider(monkeypatch, provider)
    assert client.post("/api/conversations/kz-chat/translate").status_code == 502
    assert client.get("/api/conversations/kz-chat/translation").json() == {
        "translation": None, "cost_usd": 0, "model_calls": 1}


def test_cancelled_call_is_logged(client):
    provider = MeteredProvider(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(translate(client.app.state.db, RECORD, provider))
    assert client.app.state.db.translation("kz-chat") == {
        "translation": None, "cost_usd": 0, "model_calls": 1}


def test_unavailable_and_wrong_provider(client, monkeypatch):
    monkeypatch.setattr(api_module.judge, "available", lambda pid: False)
    assert client.post("/api/conversations/kz-chat/translate").status_code == 422
    for pid in ("local", "sonnet", "unknown"):
        assert client.post("/api/conversations/kz-chat/translate", json={"provider": pid}).status_code == 422
    assert client.app.state.db.q("SELECT * FROM translation_calls") == []


def test_fake_requires_explicit_permission(tmp_path):
    app = api_module.create_app(tmp_path / "disabled.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=False)
    app.state.db.save_dataset("TEST", "test", [RECORD])
    with TestClient(app) as c:
        assert c.post("/api/conversations/kz-chat/translate", json={"provider": "fake"}).status_code == 422
    app.state.db.close()


def test_fake_for_english_and_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_ALLOW_FAKE", "1")
    app = api_module.create_app(tmp_path / "fake.db", web_dir=tmp_path, data_dir=tmp_path)
    app.state.db.save_dataset("TEST", "test", [{**RECORD, "language": "EN"}])
    with TestClient(app) as c:
        response = c.post("/api/conversations/kz-chat/translate", json={"provider": "fake"})
        assert response.status_code == 200
        saved = response.json()
        assert saved["translation"]["model"] == "fake" and saved["cost_usd"] == 0
        assert [t["turn"] for t in saved["translation"]["data"]] == list(range(1, 6))
        assert saved["translation"]["data"][-1]["text"] == ""
    app.state.db.close()


def test_simultaneous_requests_only_call_model_once(client, monkeypatch):
    class SlowProvider(MeteredProvider):
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(0.03)
            return await super().complete(*args, **kwargs)

    provider = SlowProvider()
    install_provider(monkeypatch, provider)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as c:
            return await asyncio.gather(*(c.post("/api/conversations/kz-chat/translate") for _ in range(3)))

    responses = asyncio.run(request())
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)
    assert len(provider.calls) == 1


def test_translation_does_not_touch_evaluation_data(client):
    db = client.app.state.db
    db.insert_evaluation({"conversation_id": "kz-chat", "checklist_id": 1, "status": "ok",
                          "answers": {"01": {"answer": "Да", "evidence": [4], "explanation": "[4]"}},
                          "raw": '{"original": "bytes"}', "cost_usd": 0.1, "total": 100})
    tables = ("evaluations", "runs", "run_items", "appeals", "conversations", "checklists")
    before = {t: db.q(f"SELECT * FROM {t}") for t in tables}
    assert client.post("/api/conversations/kz-chat/translate", json={"provider": "fake"}).status_code == 200
    assert {t: db.q(f"SELECT * FROM {t}") for t in tables} == before


def test_migration_on_existing_database_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO settings VALUES('kept', 'value')")
    for _ in range(2):
        db = DB(path)
        assert db.get_setting("kept") == "value"
        assert db.translation("none") == {"translation": None, "cost_usd": 0, "model_calls": 0}
        db.close()


def test_reimport_same_source_keeps_cache_changed_source_invalidates(client):
    db = client.app.state.db
    asyncio.run(translate(db, RECORD, FakeTranslationProvider()))
    db.save_dataset("TEST", "test", [copy.deepcopy(RECORD)])
    assert db.translation("kz-chat")["translation"]
    changed = copy.deepcopy(RECORD)
    changed["transcript"][0]["text"] = "New source"
    db.save_dataset("TEST", "test", [changed])
    assert db.translation("kz-chat")["translation"] is None
    assert db.translation("kz-chat")["model_calls"] == 1
