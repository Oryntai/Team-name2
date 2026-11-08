"""Ограничения общего демо-сервера: CQA_PROVIDERS (какие модели можно выбрать) и CQA_BUDGET_USD (потолок трат за сутки).

По умолчанию обе переменные не заданы — поведение прежнее. Без сети: клиент Claude подменён, .env проекта не читается.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa.api import create_app
from cqa.judge import providers


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "ROOT", tmp_path)  # без .env проекта
    monkeypatch.setattr(providers, "_SPENT", {"day": "", "usd": 0.0})
    monkeypatch.delenv("CQA_PROVIDERS", raising=False)
    monkeypatch.delenv("CQA_BUDGET_USD", raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: None)
    app = create_app(tmp_path / "t.db", web_dir=tmp_path / "web", data_dir=tmp_path / "data", allow_fake=True)
    with TestClient(app) as c:
        yield c


def model_ids(c) -> list[str]:
    return [p["id"] for p in c.get("/api/meta").json()["providers"]]


def test_all_models_by_default(client):
    assert {"haiku", "sonnet", "local", "fake"} <= set(model_ids(client))


def test_only_listed_models(client, monkeypatch):
    monkeypatch.setenv("CQA_PROVIDERS", "haiku")
    assert model_ids(client) == ["haiku"]
    assert not providers.available("sonnet")
    r = client.post("/api/runs", json={"provider": "sonnet", "conversation_ids": ["x"]})
    assert r.status_code == 422 and "sonnet" in r.json()["detail"]
    assert client.put("/api/settings", json={"default_provider": "sonnet"}).status_code == 422


class Messages:
    def __init__(self, out_tokens: int):
        self.out_tokens, self.calls = out_tokens, 0

    async def create(self, **kw):
        self.calls += 1
        usage = SimpleNamespace(input_tokens=0, output_tokens=self.out_tokens, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], usage=usage,
                               content=[SimpleNamespace(type="text", text="{}")])


def haiku(monkeypatch, out_tokens: int):
    p = providers.AnthropicProvider("haiku")
    msgs = Messages(out_tokens)
    monkeypatch.setattr(p, "_client", lambda: SimpleNamespace(messages=msgs))
    return p, msgs


def test_no_budget_by_default(monkeypatch):
    p, msgs = haiku(monkeypatch, 100_000)
    for _ in range(3):
        asyncio.run(p.complete("s", "u", {}))
    assert msgs.calls == 3


def test_budget_stops_calls(monkeypatch):
    monkeypatch.setenv("CQA_BUDGET_USD", "0.04")
    p, msgs = haiku(monkeypatch, 100_000)  # 100 тыс. токенов вывода Haiku = $0,05
    asyncio.run(p.complete("s", "u", {}))
    with pytest.raises(providers.ProviderError, match="лимит"):
        asyncio.run(p.complete("s", "u", {}))
    assert msgs.calls == 1  # второй запрос до Claude не дошёл


def test_budget_resets_next_day(monkeypatch):
    monkeypatch.setattr(providers, "_SPENT", {"day": "2000-01-01", "usd": 99.0})
    monkeypatch.setenv("CQA_BUDGET_USD", "1")
    p, msgs = haiku(monkeypatch, 10)
    asyncio.run(p.complete("s", "u", {}))  # вчерашние траты не считаются
    assert msgs.calls == 1
