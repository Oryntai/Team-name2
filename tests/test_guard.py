"""Щит до отправки (cqa/guard.py и POST /api/guard): без сети, подставной провайдер."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from cqa import guard
from cqa.api import create_app
from cqa.checklist import load_default
from cqa.judge.providers import ProviderError, Reply

CHAT = {"conversation_id": "g-1", "channel": "agent chat", "language": "RU", "audio_url": None, "transcript": [
    {"timestamp": "00:00", "speaker": "client", "text": "Почему у меня заблокирован вывод?"},
    {"timestamp": "00:02", "speaker": "bot", "text": "Соединяю с оператором."},
    {"timestamp": "00:03", "speaker": "system", "text": "Эскалация на оператора."},
    {"timestamp": "00:05", "speaker": "operator", "text": "Здравствуйте, меня зовут Дина."},
    {"timestamp": "00:40", "speaker": "client", "text": "Так что с выводом?"},
    {"timestamp": "01:00", "speaker": "operator", "text": "СЕКРЕТНАЯ-ПОЗДНЯЯ-РЕПЛИКА"},
]}


class Stub:
    """Запоминает запрос; нарушает заданные критерии."""

    pid = "stub"

    def __init__(self, violated=(), text=None):
        self.violated, self.text, self.calls = set(violated), text, []

    async def complete(self, system, user, schema, max_tokens=0):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.text is not None:
            return Reply(text=self.text, model="stub")
        ids = schema["properties"]["criteria"]["required"]
        crit = {i: {"explanation": f"пояснение {i}", "violated": i in self.violated,
                    "quote": "на карту" if i in self.violated else "", "fix": f"исправить {i}" if i in self.violated else ""}
                for i in ids}
        return Reply(text=json.dumps({"criteria": crit, "rewrite": "Исправленный ответ" if self.violated else ""},
                                     ensure_ascii=False), model="stub")


def run(coro):
    return asyncio.run(coro)


def test_violation_reported_with_quote_and_fix():
    p = Stub(violated={"08"})
    r = run(guard.check(CHAT, 5, "Скиньте деньги мне на карту", load_default(), p))
    assert r["ok"] is False and r["first_reply"] is False
    assert [i["criterion_id"] for i in r["issues"]] == ["08"]
    i = r["issues"][0]
    assert i["title"] == "Грубые ошибки" and i["quote"] == "на карту" and i["why"] == "пояснение 08"
    assert i["fix"] == "исправить 08" and r["rewrite"] == "Исправленный ответ"


def test_clean_draft_is_ok_and_rewrite_dropped():
    r = run(guard.check(CHAT, 5, "Проверю и вернусь в этот чат.", load_default(), Stub()))
    assert r["ok"] is True and r["issues"] == [] and r["rewrite"] == ""
    assert [c["id"] for c in r["checked"]] == ["08", "03", "05"]


def test_first_reply_adds_01_later_reply_does_not():
    p = Stub()
    run(guard.check(CHAT, 3, "Здравствуйте!", load_default(), p))
    run(guard.check(CHAT, 5, "Проверяю.", load_default(), p))
    assert p.calls[0]["schema"]["properties"]["criteria"]["required"] == ["08", "03", "01", "05"]
    assert p.calls[1]["schema"]["properties"]["criteria"]["required"] == ["08", "03", "05"]
    assert "первый ответ оператора" in p.calls[0]["user"]
    assert "## 01. Установление контакта" in p.calls[0]["system"]
    assert "## 01." not in p.calls[1]["system"]


def test_prompt_has_only_dialog_before_draft_and_checklist_text():
    p = Stub()
    cfg = load_default()
    run(guard.check(CHAT, 5, "Черновик оператора", cfg, p))
    sent = p.calls[0]
    assert "СЕКРЕТНАЯ-ПОЗДНЯЯ-РЕПЛИКА" not in sent["user"]  # будущее диалога модели не показываем
    assert "Так что с выводом?" in sent["user"] and "Черновик оператора" in sent["user"]
    assert "станет репликой [6]" in sent["user"]
    g08 = next(c for c in cfg["criteria"] if c["id"] == "08")["guidance"]
    assert g08 in sent["system"]  # пояснение берётся из действующего чек-листа
    for c in cfg["criteria"]:
        if c["id"] in ("02", "04", "06", "07"):
            assert f"## {c['id']}." not in sent["system"]


def test_pii_masked_before_model():
    p = Stub()
    run(guard.check(CHAT, 5, "Ваша карта 4111 1111 1111 1111", load_default(), p))
    assert "4111" not in p.calls[0]["user"] and "[номер карты скрыт]" in p.calls[0]["user"]


def test_checklist_change_follows_config():
    cfg = load_default()
    for c in cfg["criteria"]:
        if c["id"] == "08":
            c["title"] = "Грубые ошибки (новая формулировка)"
            c["yes"] = "НОВОЕ-ОПРЕДЕЛЕНИЕ"
    cfg["criteria"] = [c for c in cfg["criteria"] if c["id"] != "05"]
    p = Stub(violated={"08"})
    r = run(guard.check(CHAT, 5, "текст", cfg, p))
    assert "НОВОЕ-ОПРЕДЕЛЕНИЕ" in p.calls[0]["system"]
    assert [c["id"] for c in r["checked"]] == ["08", "03"]
    assert r["issues"][0]["title"] == "Грубые ошибки (новая формулировка)"


def test_bad_inputs_and_bad_model_reply():
    cfg = load_default()
    with pytest.raises(ValueError):
        run(guard.check(CHAT, 99, "текст", cfg, Stub()))
    with pytest.raises(ValueError):
        run(guard.check(CHAT, 2, "   ", cfg, Stub()))
    with pytest.raises(ValueError):
        run(guard.check(CHAT, 2, "текст", {**cfg, "criteria": [c for c in cfg["criteria"]
                                                              if c["id"] not in guard.GUARD_IDS]}, Stub()))
    with pytest.raises(ProviderError):
        run(guard.check(CHAT, 2, "текст", cfg, Stub(text="не json")))
    with pytest.raises(ProviderError):  # нет поля violated
        run(guard.check(CHAT, 5, "текст", cfg, Stub(text=json.dumps(
            {"criteria": {i: {"explanation": "", "quote": "", "fix": ""} for i in ("08", "03", "05")},
             "rewrite": ""}))))


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_schema_has_no_keywords_claude_rejects():
    schema = guard.build_schema(guard.selected(load_default(), True))
    banned = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "format"}
    for k, v in _keys(schema):
        assert k not in banned
        if k == "enum":
            assert None not in v
        if k == "type":
            assert not isinstance(v, list)


# ---------- POST /api/guard ----------
@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "g.db", web_dir=tmp_path / "web", data_dir=tmp_path / "data", allow_fake=True)
    app.state.db.save_dataset("G", "test", [CHAT])
    with TestClient(app) as c:
        yield c


def test_api_guard_with_fake_provider(client):
    r = client.post("/api/guard", json={"conversation_id": "g-1", "upto": 3, "draft": "Здравствуйте, я Дина.",
                                        "provider": "fake"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True and b["first_reply"] is True
    assert [c["id"] for c in b["checked"]] == ["08", "03", "01", "05"]
    assert b["checklist_label"].endswith("v1") and b["model"] == "fake"


def test_api_guard_errors(client):
    assert client.post("/api/guard", json={"conversation_id": "нет-такого", "upto": 0, "draft": "x",
                                           "provider": "fake"}).status_code == 404
    r = client.post("/api/guard", json={"conversation_id": "g-1", "upto": 50, "draft": "x", "provider": "fake"})
    assert r.status_code == 422 and "Место черновика" in r.json()["detail"]
    assert client.post("/api/guard", json={"conversation_id": "g-1", "upto": 1, "draft": "  ",
                                           "provider": "fake"}).status_code == 422
    assert client.post("/api/guard", json={"conversation_id": "g-1", "upto": 1, "draft": "x",
                                           "provider": "gpt"}).status_code == 422


def test_api_guard_uses_latest_checklist(client, monkeypatch):
    cfg = load_default()
    for c in cfg["criteria"]:
        if c["id"] == "08":
            c["title"] = "Грубые нарушения v2"
    assert client.post("/api/checklists", json={"config": cfg, "note": "тест"}).status_code == 201
    seen = {}

    async def fake_check(rec, upto, draft, config, provider):
        seen["title"] = next(c["title"] for c in config["criteria"] if c["id"] == "08")
        return {"ok": True}

    monkeypatch.setattr(guard, "check", fake_check)
    r = client.post("/api/guard", json={"conversation_id": "g-1", "upto": 1, "draft": "x", "provider": "fake"})
    assert r.status_code == 200 and seen["title"] == "Грубые нарушения v2"
    assert r.json()["checklist_label"].endswith("v2")
