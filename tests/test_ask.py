"""Ask: компактный снимок, строгие ссылки, учёт всех попыток; только фейковый провайдер."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa.ask import FakeAskProvider, answer_question, build_context, build_system, response_schema
from cqa.db import DB
from cqa.judge.providers import ProviderError, Reply


def insert_evaluation(db, conversation_id="a", checklist_id=1, **changes):
    ev = {"conversation_id": conversation_id, "checklist_id": checklist_id, "status": "ok", "total": 42.5,
          "gross": False, "model": "fake", "answers": {
              "01": {"answer": "Нет", "explanation": "Пояснение " * 100, "votes": ["Да", "Нет", "Нет"]},
              "08": {"answer": "Нет", "explanation": "Нарушений не обнаружено", "votes": ["Нет"] * 3},
          }}
    ev.update(changes)
    return db.insert_evaluation(ev)


@pytest.fixture
def db(tmp_path):
    db = DB(tmp_path / "ask.db")
    db.seed_default_checklist()
    records = [{"conversation_id": cid, "language": lang, "channel": channel,
                "transcript": [{"speaker": "client", "text": "SECRET TRANSCRIPT", "timestamp": "00:00"}]}
               for cid, lang, channel in [("a", "RU", "agent chat"), ("b", "KZ", "voice call"), ("unevaluated", "EN", "agent chat")]]
    db.save_dataset("DEV", "test", records)
    db.save_dataset("OTHER", "test", [{**records[0], "conversation_id": "other"}])
    insert_evaluation(db, "a")
    insert_evaluation(db, "b")
    insert_evaluation(db, "other")
    yield db
    db.close()


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: default)
    app = api_module.create_app(db.path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    app.state.db.set_setting("default_provider", "fake")
    with TestClient(app) as client:
        yield client
    app.state.db.close()


def test_context_latest_success_filters_compaction_and_no_mutation(db):
    first = db.one("SELECT MIN(id) AS id FROM evaluations")["id"]
    cfg = db.checklist(1)["config"]
    cfg["criteria"][0]["title"] = "Новая формулировка"
    cl2, _ = db.create_checklist(cfg)
    latest = insert_evaluation(db, "a", cl2, total=75, gross=True)
    insert_evaluation(db, "a", cl2, status="error", total=None, answers={})
    db.create_appeal(latest, "01", "Возражение")
    db.decide_appeal(1, "changed", "Да", "Уточнение")
    before = db.q("SELECT * FROM evaluations ORDER BY id")
    context = build_context(db, "DEV")
    assert [c["id"] for c in context["conversations"]] == ["a", "b"]
    a = context["conversations"][0]
    assert a["evaluation_id"] == latest and a["total"] == 75 and a["gross"] is True
    assert a["language"] == "RU" and a["channel"] == "agent chat"
    assert a["criteria"]["01"]["answer"] == "Нет"  # без наложения апелляции
    assert len(a["criteria"]["01"]["explanation"]) == 200
    assert a["criteria"]["01"]["disagreed"] is True
    assert a["criteria"]["08"]["disagreed"] is False
    assert context["checklists"][str(cl2)][0]["title"] == "Новая формулировка"
    assert "SECRET TRANSCRIPT" not in build_system(context)
    old = build_context(db, "DEV", 1)
    assert old["conversations"][0]["evaluation_id"] == first
    assert list(old["checklists"]) == ["1"]
    assert len(build_context(db)["conversations"]) == 3
    assert build_context(db, "missing")["conversations"] == []
    assert db.q("SELECT * FROM evaluations ORDER BY id") == before


def test_no_votes_are_unknown_and_ids_preserved(db):
    cid = "900101300007"  # даже похожий на ИИН id остаётся рабочей ссылкой
    db.save_dataset("IDS", "test", [{"conversation_id": cid, "transcript": []}])
    insert_evaluation(db, cid, answers={"01": {"answer": "Да", "explanation": "ИИН 900101300007"}})
    c = build_context(db, "IDS")["conversations"][0]
    assert c["id"] == cid
    assert c["criteria"]["01"]["disagreed"] is None
    assert "900101300007" not in c["criteria"]["01"]["explanation"]


def test_optional_insights_match_evaluation_not_just_conversation(db):
    assert "insights" not in build_context(db, "DEV")["conversations"][0]
    # Таблица insights — из схемы базы (cqa/insights.py); пишем в её настоящие столбцы.
    eid = db.one("SELECT MAX(id) AS id FROM evaluations WHERE conversation_id='a'")["id"]
    def insight(evaluation_id, created_at, data):
        db.ex("INSERT INTO insights(conversation_id, evaluation_id, created_at, model, cost_usd, data) VALUES(?,?,?,?,?,?)",
              ("a", evaluation_id, created_at, "fake", 0, data))
    insight(eid, "2026-01-01", json.dumps({"summary": "Старое резюме"}))
    insight(eid, "2026-01-02", json.dumps({"summary": "ИИН 900101300007 " + "я" * 700,
                                         "topic": "Перевод", "flags": ["Нужна проверка", {"note": "a@example.com"}]}))
    insight(eid + 100, "2026-01-03", json.dumps({"summary": "ЧУЖАЯ ОЦЕНКА"}))
    context = build_context(db, "DEV")
    data = context["conversations"][0]["insights"]
    assert len(data["summary"]) == 500 and "900101300007" not in data["summary"]
    assert data["topic"] == "Перевод" and data["flags"][0] == "Нужна проверка"
    assert "a@example.com" not in json.dumps(data)
    assert "ЧУЖАЯ ОЦЕНКА" not in build_system(context)
    insight(eid, "2026-01-04", "bad json")
    assert "insights" not in build_context(db, "DEV")["conversations"][0]


def test_full_65_dialogues_fit_one_call_and_stable_cached_prefix(db):
    records = [{"conversation_id": f"dialog-{n:02}", "language": "RU", "channel": "agent chat", "transcript": []}
               for n in range(65)]
    db.save_dataset("FULL", "test", records)
    for record in records:
        answers = {f"{i:02}": {"answer": "Нет", "explanation": "я" * 500, "votes": ["Да", "Нет"]} for i in range(1, 9)}
        insert_evaluation(db, record["conversation_id"], answers=answers)
    context = build_context(db, "FULL")
    assert len(context["conversations"]) == 65
    system = build_system(context)
    assert len(system) < 200_000
    class Recording(FakeAskProvider):
        calls = []
        async def complete(self, system, user, schema, max_tokens=0):
            self.calls.append((system, user, schema))
            return await super().complete(system, user, schema, max_tokens)
    provider = Recording()
    asyncio.run(answer_question(db, "Первый вопрос", context, provider))
    asyncio.run(answer_question(db, "Второй вопрос", build_context(db, "FULL"), provider))
    assert len(provider.calls) == 2
    assert provider.calls[0][0] == provider.calls[1][0] == system
    assert provider.calls[0][1] != provider.calls[1][1]
    assert len(provider.calls[0][2]["properties"]["refs"]["items"]["properties"]["conversation_id"]["enum"]) == 65


def test_schema_and_metering_saved_even_with_cached_tokens(db):
    context = build_context(db, "DEV")
    schema = response_schema(context)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["answer", "refs"]
    ref = schema["properties"]["refs"]["items"]
    assert ref["additionalProperties"] is False
    assert set(ref["required"]) == {"conversation_id", "criterion_id", "why"}
    assert ref["properties"]["conversation_id"]["enum"] == ["a", "b"]
    assert ref["properties"]["criterion_id"]["enum"] == ["01", "08", None]
    class Metered(FakeAskProvider):
        async def complete(self, system, user, schema, max_tokens=0):
            assert "900101300007" not in user
            result = await super().complete(system, user, schema, max_tokens)
            result.model = "haiku-test"
            result.input_tokens, result.output_tokens = 1000, 500
            result.cache_read, result.cache_write = 100, 200
            return result
    result = asyncio.run(answer_question(db, "ИИН 900101300007", context, Metered("haiku")))
    assert result["cost_usd"] == 0.000376 and result["model"] == "haiku-test"
    saved = db.list_asks()[0]
    assert saved["question"] == "ИИН 900101300007" and saved["created_at"]
    assert saved["input_tokens"] == 1300 and saved["output_tokens"] == 500
    assert saved["answer"] == result["answer"] and saved["refs"] == result["refs"]
    assert saved["cost_usd"] == result["cost_usd"] and saved["status"] == "ok"


@pytest.mark.parametrize("output", [
    "not json", "[]", "{}", '{"answer":3,"refs":[]}', '{"answer":" ","refs":[]}',
    '{"answer":"Текст","refs":false}', '{"answer":"Текст","refs":[null]}',
    '{"answer":"Текст","refs":[],"extra":1}',
    json.dumps({"answer": "Текст", "refs": [{"conversation_id": "other", "criterion_id": None, "why": "x"}]}),
    json.dumps({"answer": "Текст", "refs": [{"conversation_id": "a", "criterion_id": "99", "why": "x"}]}),
    json.dumps({"answer": "Текст", "refs": [{"conversation_id": "a", "criterion_id": [], "why": "x"}]}),
    json.dumps({"answer": "Текст", "refs": [{"conversation_id": [], "criterion_id": None, "why": "x"}]}),
    json.dumps({"answer": "Текст", "refs": [{"conversation_id": "a", "criterion_id": None, "why": " "}]}),
])
def test_invalid_output_is_502_and_cost_is_logged(client, monkeypatch, output):
    class Invalid(FakeAskProvider):
        async def complete(self, *args, **kwargs):
            return Reply(text=output, model="haiku-test", input_tokens=1000, output_tokens=200)
    client.app.state.db.set_setting("default_provider", "haiku")
    monkeypatch.setattr(api_module.judge, "available", lambda pid: True)
    monkeypatch.setattr(api_module.judge, "get_provider", lambda pid: Invalid("haiku"))
    response = client.post("/api/ask", json={"question": "Вопрос", "dataset_id": "DEV"})
    assert response.status_code == 502 and response.json()["errors"]
    saved = client.get("/api/asks").json()[0]
    assert saved["status"] == "error" and saved["error"] and saved["refs"] == []
    assert saved["cost_usd"] == 0.0002 and saved["input_tokens"] == 1000


def test_endpoint_and_journal_and_protected_data_unchanged(client):
    db = client.app.state.db
    before = {t: db.q(f"SELECT * FROM {t}") for t in ["evaluations", "checklists", "conversations", "appeals", "runs"]}
    response = client.post("/api/ask", json={"question": "  Где сомнения?  ", "dataset_id": "DEV", "checklist_id": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"answer", "refs", "cost_usd", "model"}
    assert result["model"] == "fake" and result["refs"][0]["conversation_id"] == "a"
    saved = client.get("/api/asks").json()[0]
    assert saved["question"] == "Где сомнения?" and saved["answer"] == result["answer"]
    assert saved["status"] == "ok" and saved["input_tokens"] > 0
    for table, rows in before.items():
        assert db.q(f"SELECT * FROM {table}") == rows


@pytest.mark.parametrize("body", [{}, {"question": " "}, {"question": 123}, {"question": "x" * 4001},
    {"question": "x", "dataset_id": "missing"}, {"question": "x", "checklist_id": 999},
    {"question": "x", "checklist_id": True}, {"question": "x", "dataset_id": ""}])
def test_invalid_request_does_not_call_or_log(client, monkeypatch, body):
    def unexpected(*args):
        pytest.fail("Неверный запрос не должен вызывать модель")
    monkeypatch.setattr(api_module.judge, "get_provider", unexpected)
    assert client.post("/api/ask", json=body).status_code == 422
    assert client.get("/api/asks").json() == []


def test_empty_selection_needs_no_provider(client, monkeypatch):
    client.app.state.db.save_dataset("EMPTY", "test", [])
    monkeypatch.setattr(api_module.judge, "available", lambda pid: False)
    response = client.post("/api/ask", json={"question": "Вопрос", "dataset_id": "EMPTY"})
    assert response.status_code == 200
    assert response.json() == {"answer": "В выбранном наборе нет оценённых диалогов.", "refs": [], "model": "none", "cost_usd": 0}
    assert client.get("/api/asks").json() == []  # вызовов не было


def test_haiku_is_used_regardless_of_default_and_fake_is_gated(db, tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: default)
    app = api_module.create_app(db.path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=False)
    calls = []
    monkeypatch.setattr(api_module.judge, "available", lambda pid: True)
    def get_provider(pid):
        calls.append(pid)
        return FakeAskProvider(pid)
    monkeypatch.setattr(api_module.judge, "get_provider", get_provider)
    with TestClient(app) as client:
        for default in ["fake", "sonnet", "local"]:
            app.state.db.set_setting("default_provider", default)
            assert client.post("/api/ask", json={"question": "Вопрос"}).status_code == 200
    app.state.db.close()
    assert calls == ["haiku"] * 3


def test_unavailable_and_failed_provider(client, monkeypatch):
    client.app.state.db.set_setting("default_provider", "haiku")
    monkeypatch.setattr(api_module.judge, "available", lambda pid: False)
    assert client.post("/api/ask", json={"question": "Вопрос"}).status_code == 422
    assert client.get("/api/asks").json() == []
    class Failed(FakeAskProvider):
        async def complete(self, *args, **kwargs):
            raise ProviderError("Нет связи")
    monkeypatch.setattr(api_module.judge, "available", lambda pid: True)
    monkeypatch.setattr(api_module.judge, "get_provider", lambda pid: Failed("haiku"))
    response = client.post("/api/ask", json={"question": "Вопрос"})
    assert response.status_code == 502 and response.json()["errors"] == ["Нет связи"]
    saved = client.get("/api/asks").json()[0]
    assert saved["status"] == "error" and saved["error"] == "Нет связи"


def test_migration_on_existing_database_is_idempotent(db):
    db.ex("DROP TABLE asks")
    before = copy.deepcopy(db.q("SELECT * FROM evaluations"))
    for _ in range(2):
        reopened = DB(db.path)
        assert reopened.list_asks() == []
        assert reopened.q("SELECT * FROM evaluations") == before
        reopened.close()
