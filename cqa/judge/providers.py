"""Провайдеры моделей: Claude (официальный SDK anthropic), локальная модель (OpenAI-совместимый сервер), заглушка для тестов.

Ключи и адреса — из окружения или файла .env в корне проекта (в git не идёт):
  ANTHROPIC_API_KEY — для haiku / sonnet;
  CQA_LOCAL_BASE_URL, CQA_LOCAL_MODEL, CQA_LOCAL_API_KEY — для local (vLLM, llama.cpp server, Ollama, LM Studio, MLX).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[2]

# $ за 1М токенов (вход, выход); кеш: запись ×1,25 входа, чтение ×0,1 входа (справка claude-api, 2026-10-06)
PROVIDERS = {
    "haiku": {"label": "Claude Haiku 5.5 · облако", "kind": "anthropic", "model": "claude-haiku-5-5",
              "price_in": 0.10, "price_out": 0.50, "effort": "medium"},
    "sonnet": {"label": "Claude Sonnet 5.5 · облако", "kind": "anthropic", "model": "claude-sonnet-5-5",
               "price_in": 2.00, "price_out": 10.00, "effort": "medium"},
    "local": {"label": "Своя модель · локально", "kind": "openai", "model": None, "price_in": 0.0, "price_out": 0.0},
    "fake": {"label": "Заглушка для тестов", "kind": "fake", "model": "fake", "price_in": 0.0, "price_out": 0.0,
             "hidden": True},
}


class ProviderError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    stop: str | None = None


def env(key: str, default: str | None = None) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == key and v.strip():
                    return v.strip().strip('"').strip("'")
    return default


def available(pid: str) -> bool:
    kind = PROVIDERS.get(pid, {}).get("kind")
    if kind == "anthropic":
        return bool(env("ANTHROPIC_API_KEY"))
    if kind == "openai":
        return bool(env("CQA_LOCAL_BASE_URL"))
    return kind == "fake"


def model_name(pid: str) -> str:
    p = PROVIDERS[pid]
    return (env("CQA_LOCAL_MODEL") or "local") if p["kind"] == "openai" else p["model"]


def cost_usd(pid: str, r: Reply) -> float:
    p = PROVIDERS[pid]
    pin, pout = p["price_in"], p["price_out"]
    return round((r.input_tokens * pin + r.cache_write * pin * 1.25 + r.cache_read * pin * 0.1
                  + r.output_tokens * pout) / 1e6, 6)


class AnthropicProvider:
    def __init__(self, pid: str):
        self.pid, self.cfg = pid, PROVIDERS[pid]
        self.model = self.cfg["model"]
        self._clients: dict[int, object] = {}

    def _client(self):
        import anthropic
        key = id(asyncio.get_running_loop())  # httpx-клиент привязан к своему циклу событий
        if key not in self._clients:
            api_key = env("ANTHROPIC_API_KEY")
            if not api_key:
                raise ProviderError("Нет ключа ANTHROPIC_API_KEY (переменная окружения или .env).")
            # base_url явно: в окружении Claude Code может стоять ANTHROPIC_BASE_URL
            self._clients[key] = anthropic.AsyncAnthropic(api_key=api_key, base_url="https://api.anthropic.com",
                                                          timeout=300, max_retries=4)
        return self._clients[key]

    async def complete(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> Reply:
        import anthropic
        client = self._client()
        kw = dict(model=self.model, max_tokens=max_tokens,
                  system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": user}],
                  output_config={"effort": self.cfg.get("effort", "medium"),
                                 "format": {"type": "json_schema", "schema": schema}})
        try:
            if self.pid == "sonnet":  # серверная страховка на случай отказа (у Haiku 5.5 её нет)
                r = await client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            else:
                r = await client.messages.create(**kw)
        except anthropic.AuthenticationError as e:
            raise ProviderError("Ключ Claude не принят (401).") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Лимит запросов Claude (429), повторите позже.") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude API {e.status_code}: {str(e.message)[:300]}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Нет связи с Claude API: {e}") from e
        if r.stop_reason == "refusal":
            raise ProviderError("Модель отказалась оценивать диалог (refusal).")
        if r.stop_reason == "max_tokens":
            raise ProviderError("Ответ модели обрезан (max_tokens).")
        text = next((b.text for b in r.content if b.type == "text"), "")
        u = r.usage
        return Reply(text=text, model=r.model, input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                     cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                     cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0, stop=r.stop_reason)


class OpenAICompatProvider:
    """Своя модель на OpenAI-совместимом сервере. Сначала пробуем строгую схему, затем json_object, затем без формата."""

    def __init__(self, pid: str):
        self.pid = pid
        self.base = (env("CQA_LOCAL_BASE_URL") or "").rstrip("/")
        self.model = env("CQA_LOCAL_MODEL") or "local"
        self.key = env("CQA_LOCAL_API_KEY") or "local"
        self._clients: dict[int, object] = {}

    def _client(self):
        from openai import AsyncOpenAI
        if not self.base:
            raise ProviderError("Не задан адрес локальной модели CQA_LOCAL_BASE_URL.")
        key = id(asyncio.get_running_loop())
        if key not in self._clients:
            base = self.base if self.base.endswith("/v1") else self.base + "/v1"
            self._clients[key] = AsyncOpenAI(base_url=base, api_key=self.key, timeout=900, max_retries=1)
        return self._clients[key]

    async def complete(self, system: str, user: str, schema: dict, max_tokens: int = 8000) -> Reply:
        import openai
        client = self._client()
        formats = [{"type": "json_schema", "json_schema": {"name": "cqa", "schema": schema, "strict": True}},
                   {"type": "json_object"}, None]
        last = None
        for rf in formats:
            extra = {"response_format": rf} if rf else {}
            try:
                r = await client.chat.completions.create(
                    model=self.model, max_tokens=max_tokens, temperature=0,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **extra)
            except openai.BadRequestError as e:  # сервер не умеет такой response_format — пробуем проще
                last = e
                continue
            except openai.APIError as e:
                raise ProviderError(f"Локальная модель: {str(e)[:300]}") from e
            msg = r.choices[0].message
            text = msg.content or (getattr(msg, "model_extra", None) or {}).get("reasoning_content") or ""
            if r.choices[0].finish_reason == "length":
                raise ProviderError("Ответ локальной модели обрезан (max_tokens).")
            u = r.usage
            return Reply(text=text, model=r.model or self.model, input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                         output_tokens=getattr(u, "completion_tokens", 0) or 0, stop=r.choices[0].finish_reason)
        raise ProviderError(f"Локальная модель не приняла запрос: {str(last)[:300]}")


class FakeProvider:
    """Только для тестов: детерминированный ответ без сети (первая допустимая ступень, доказательство — реплика 1)."""

    def __init__(self, pid: str = "fake"):
        self.pid, self.model = pid, "fake"

    async def complete(self, system: str, user: str, schema: dict, max_tokens: int = 0) -> Reply:
        crit = schema["properties"]["criteria"]["properties"]
        out = {cid: {"explanation": f"Тестовый ответ заглушки по критерию {cid} [1].",
                     "answer": spec["properties"]["answer"]["enum"][0], "evidence": [1]} for cid, spec in crit.items()}
        return Reply(text=json.dumps({"criteria": out}, ensure_ascii=False), model="fake",
                     input_tokens=len(system) + len(user), output_tokens=200, stop="end_turn")


_CACHE: dict[str, object] = {}


def get_provider(pid: str):
    if pid not in PROVIDERS:
        raise ProviderError(f"Неизвестная модель «{pid}». Доступны: {', '.join(PROVIDERS)}.")
    if pid not in _CACHE or PROVIDERS[pid]["kind"] == "openai":  # адрес локальной модели можно поменять на ходу
        kind = PROVIDERS[pid]["kind"]
        _CACHE[pid] = {"anthropic": AnthropicProvider, "openai": OpenAICompatProvider, "fake": FakeProvider}[kind](pid)
    return _CACHE[pid]
