"""HTTP API (FastAPI) по контракту docs/ARCHITECTURE.md и раздача интерфейса из web/.

Запуск: `python -m cqa` (см. cqa/__main__.py) или `uvicorn --factory cqa.api:create_app`.
Итог всегда считается заново из ответов (`scoring.score`): исходный — из ответов модели,
финальный — с решениями проверяющего поверх. Оценки модели в базе не переписываются.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import pathlib
import re
from typing import Literal

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (analytics, ask, assist, audio, calibrations, callmetrics, crittest, doubts, guard, insights, judge,
               pii, precedents, sandbox, sources, translate)
from .checklist import allowed_answers, normalize, validate
from .db import DB
from .judge.providers import env as read_env
from .incremental import plan
from .runner import Runner
from .scoring import ScoreError, score

log = logging.getLogger("cqa.api")
for _name in ("httpx", "httpcore"):  # полный адрес запроса содержит подпись ссылки API — не логируем
    logging.getLogger(_name).setLevel(logging.WARNING)
ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
_SAFE_ID = re.compile(r"^[\w.\-]{1,128}$")


# ---------- тела запросов ----------
class SettingsIn(BaseModel):
    dataset_url: str | None = None
    default_provider: str | None = None


class ImportIn(BaseModel):
    group_id: str
    url: str | None = None


class RunIn(BaseModel):
    conversation_ids: list[str] | None = None
    dataset_id: str | None = None
    checklist_id: int | None = None  # не задан → самая новая версия чек-листа
    provider: str | None = None
    mode: Literal["full", "incremental"] = "incremental"
    votes: int | None = Field(default=None, ge=1, le=5, strict=True)


class InsightsIn(BaseModel):
    conversation_ids: list[str] | None = None
    dataset_id: str | None = None


class ChecklistIn(BaseModel):
    config: dict
    note: str | None = ""
    parent_id: int | None = None


class ValidateIn(BaseModel):
    config: dict


class AssistIn(BaseModel):
    config: dict
    criterion_id: str
    provider: str | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000, strict=True)
    dataset_id: str | None = Field(default=None, min_length=1)
    checklist_id: int | None = Field(default=None, gt=0, strict=True)


class TryIn(BaseModel):
    config: dict
    conversation_ids: list[str] | None = None
    provider: str | None = None
    criteria: list[str] | None = None


class CheckApprovedIn(BaseModel):
    config: dict
    criteria: list[str] | None = None
    limit: int = Field(default=12, ge=1, le=100, strict=True)
    provider: str | None = None


class AppealIn(BaseModel):
    evaluation_id: int
    criterion_id: str
    comment: str | None = ""
    reason_code: Literal["wrong_interpretation", "wrong_evidence", "wrong_na", "other"] = "other"


class CalibrationIn(BaseModel):
    model_config = {"extra": "forbid"}
    conversation_id: str
    checklist_id: int | None = Field(default=None, ge=1, strict=True)
    reviewer: str | None = Field(default="", max_length=200)
    answers: dict[str, str]
    comment: str | None = Field(default="", max_length=5000)


class DecisionIn(BaseModel):
    decision: str
    new_answer: str | None = None
    reason: str | None = ""


class DoubtQuestionsIn(BaseModel):
    model_config = {"extra": "forbid"}
    criterion_id: str
    dataset_id: str | None = None
    evaluation_id: int | None = Field(default=None, ge=1, strict=True)  # один спор — вопрос для карточки
    refresh: bool = False
    provider: str | None = None


class DoubtResolveIn(BaseModel):
    model_config = {"extra": "forbid"}
    evaluation_id: int = Field(ge=1, strict=True)
    criterion_id: str
    answer: str
    question: str | None = Field(default="", max_length=1000)
    reason: str | None = Field(default="", max_length=2000)


class CacheHeaders:
    """Cache-Control без BaseHTTPMiddleware (не мешает потоковой отдаче записи)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        value = (b"private, max-age=600" if path.startswith("/api/audio/") else
                 b"no-store" if path.startswith("/api/") else b"no-cache")

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", value))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_wrapped)


def _fail(status: int, message: str, errors: list[str] | None = None):
    raise HTTPException(status_code=status, detail={"detail": message, "errors": errors or [message]})


class TranslateIn(BaseModel):
    provider: Literal["haiku", "fake"] = "haiku"


class GuardIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    upto: int = Field(ge=0, strict=True)  # сколько реплик диалога идут до черновика
    draft: str = Field(min_length=1, max_length=4000)
    provider: str | None = None


def create_app(db_path: str | os.PathLike | None = None, *, web_dir: str | os.PathLike | None = None,
               data_dir: str | os.PathLike | None = None, autoload_dev: bool = False,
               allow_fake: bool | None = None) -> FastAPI:
    db = DB(db_path)
    db.seed_default_checklist()
    interrupted = db.fail_interrupted_runs()
    if interrupted:
        log.warning("прогонов прервано перезапуском: %s", interrupted)
    data_dir = pathlib.Path(data_dir or os.environ.get("CQA_DATA_DIR") or DATA_DIR)
    web_dir = pathlib.Path(web_dir or WEB_DIR)
    if allow_fake is None:
        allow_fake = os.environ.get("CQA_ALLOW_FAKE") == "1"
    if autoload_dev and db.count_conversations() == 0 and (data_dir / "dev.json").exists():
        try:
            gid, total, records = sources.parse_bytes((data_dir / "dev.json").read_bytes())
            db.save_dataset(gid or "DEV", "file:data/dev.json", records, total)
            log.info("загружен набор %s из data/dev.json: %s диалогов", gid or "DEV", len(records))
        except sources.SourceError as e:
            log.warning("data/dev.json не загружен: %s", e)

    runner = Runner(db)
    insights_runner = insights.InsightsRunner(db)
    db.fail_interrupted_insights()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        for t in list(insights_runner.tasks):
            t.cancel()
        await insights_runner.wait()
        for t in list(runner.tasks):  # остановка сервера: незаконченные прогоны при старте пометятся ошибкой
            t.cancel()

    app = FastAPI(title="CQA — Conversation Quality Analytics", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.runner = db, runner
    app.state.insights_runner = insights_runner
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(CacheHeaders)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        d = exc.detail
        body = d if isinstance(d, dict) else {"detail": str(d), "errors": [str(d)]}
        if exc.status_code == 404 and not isinstance(d, dict):
            body = {"detail": "Не найдено.", "errors": ["Не найдено."]}
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        errs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query", "path"))
            kind = e.get("type", "")
            msg = ("обязательное поле" if kind == "missing" else "неверный тип значения"
                   if kind.endswith("_type") or kind.endswith("_parsing") else e.get("msg", "неверное значение"))
            errs.append(f"{loc}: {msg}" if loc else msg)
        return JSONResponse({"detail": "Неверный запрос: " + "; ".join(errs), "errors": errs}, status_code=422)

    # ---------- помощники ----------
    def dataset_url() -> str | None:
        return db.get_setting("dataset_url") or read_env("CQA_DATASET_URL")

    def visible_providers() -> list[str]:
        return [pid for pid, p in judge.PROVIDERS.items() if not p.get("hidden") or (allow_fake and pid == "fake")]

    def default_provider() -> str:
        p = db.get_setting("default_provider")
        return p if p in judge.PROVIDERS else "haiku"

    def label(cl: dict | None) -> str | None:
        return f"{cl['name']} · v{cl['version']}" if cl else None

    cl_cache: dict[int, dict] = {}

    def checklist(cl_id: int) -> dict | None:
        if cl_id not in cl_cache:  # версии неизменяемы — кешировать можно
            cl = db.checklist(cl_id)
            if not cl:
                return None
            cl_cache[cl_id] = cl
        return cl_cache[cl_id]

    def final_answers(original: dict[str, str], appeals: list[dict]) -> dict[str, str]:
        ans = dict(original)
        for a in sorted((a for a in appeals if a["status"] in ("confirmed", "changed")),
                        key=lambda a: (a["decided_at"] or "", a["id"])):
            if a["status"] == "changed" and a["new_answer"] is not None:
                ans[a["criterion_id"]] = a["new_answer"]
        return ans

    def scores(ev: dict, appeals: list[dict] | None = None):
        """(исходный итог, финальный итог, исходные ответы, финальные ответы) — None, если оценка не удалась."""
        cl = checklist(ev["checklist_id"])
        if not cl or ev["status"] != "ok":
            return None, None, {}, {}
        cfg = cl["config"]
        orig = {k: v.get("answer") for k, v in (ev["answers"] or {}).items() if isinstance(v, dict)}
        fin = final_answers(orig, appeals if appeals is not None else db.appeals_for(ev["id"]))
        try:
            return score(cfg, orig), score(cfg, fin), orig, fin
        except ScoreError:
            return None, None, orig, fin

    def summary(ev: dict, appeals: list[dict] | None = None) -> dict:
        so, sf, _, _ = scores(ev, appeals)
        cl = checklist(ev["checklist_id"])
        return {"evaluation_id": ev["id"], "checklist_id": ev["checklist_id"], "checklist_label": label(cl),
                "provider": ev["provider"], "model": ev["model"], "created_at": ev["created_at"], "status": ev["status"],
                "error": ev["error"], "total": so["total"] if so else ev["total"],
                "final_total": sf["total"] if sf else ev["total"],
                "gross": so["gross"] if so else (None if ev["gross"] is None else bool(ev["gross"])),
                "final_gross": sf["gross"] if sf else (None if ev["gross"] is None else bool(ev["gross"]))}

    def appeal_short(a: dict) -> dict:
        return {"id": a["id"], "comment": a["comment"], "status": a["status"], "new_answer": a["new_answer"],
                "reason_code": a["reason_code"],
                "reason": a["reason"], "created_at": a["created_at"], "decided_at": a["decided_at"]}

    def section_view(s: dict | None) -> dict | None:
        if not s:
            return None
        return {"total": s["total"], "gross": s["gross"], "gross_ids": s["gross_ids"],
                "applicable_weight": s["applicable_weight"], "sections": s["sections"]}

    def eval_detail(ev: dict) -> dict:
        cl = checklist(ev["checklist_id"])
        cfg = cl["config"] if cl else {"criteria": []}
        appeals = db.appeals_for(ev["id"])
        so, sf, orig, fin = scores(ev, appeals)
        by_crit: dict[str, list[dict]] = {}
        for a in appeals:
            by_crit.setdefault(a["criterion_id"], []).append(a)
        crit = []
        for c in cfg["criteria"]:
            o = (ev["answers"] or {}).get(c["id"])
            hist = by_crit.get(c["id"], [])
            crit.append({
                "id": c["id"], "title": c.get("title"), "section": c.get("section"), "type": c["type"],
                "weight": c.get("weight"), "na": bool(c.get("na")), "allowed": allowed_answers(c),
                "reused_from": (o or {}).get("reused_from"), "fingerprint": (o or {}).get("fingerprint"),
                "original": ({"answer": o.get("answer"), "explanation": o.get("explanation", ""),
                              "evidence": o.get("evidence") or [],
                              **{k: o[k] for k in ("votes", "agreement") if k in o}}
                             if isinstance(o, dict) else None),
                "positions": doubts.positions(db, ev, c["id"]) if doubts.split(o) else None,
                "final_answer": fin.get(c["id"]) if fin else (o or {}).get("answer"),
                "appeal": appeal_short(hist[-1]) if hist else None,
                "appeals": [appeal_short(a) for a in hist],
                "score_original": so["criteria"].get(c["id"]) if so else None,
                "score_final": sf["criteria"].get(c["id"]) if sf else None})
        return {"id": ev["id"], "run_id": ev["run_id"], "conversation_id": ev["conversation_id"],
                "checklist": {"id": ev["checklist_id"], "name": cl["name"] if cl else None,
                              "version": cl["version"] if cl else None, "label": label(cl)},
                "provider": ev["provider"], "model": ev["model"], "prompt_version": ev["prompt_version"],
                "mode": ev["mode"], "evaluated": ev["evaluated"], "reused": ev["reused"],
                "model_calls": ev["model_calls"],
                "precedents": ev["precedents"],
                "thinking": ev.get("thinking") or [],
                "created_at": ev["created_at"], "latency_s": ev["latency_s"], "input_tokens": ev["input_tokens"],
                "output_tokens": ev["output_tokens"], "cost_usd": ev["cost_usd"], "status": ev["status"],
                "error": ev["error"], "pii_masked": ev["pii_masked"] or [], "criteria": crit,
                "original": section_view(so), "final": section_view(sf)}

    def reply(tr: list[dict], n: int) -> dict | None:
        if 1 <= n <= len(tr):
            t = tr[n - 1]
            return {"n": n, "timestamp": t.get("timestamp"), "speaker": t.get("speaker"), "text": t.get("text")}
        return None

    def appeal_full(a: dict, ev: dict | None = None, conv: dict | None = None) -> dict:
        ev = ev or db.evaluation(a["evaluation_id"])
        conv = conv or db.conversation(ev["conversation_id"]) or {"transcript": []}
        cl = checklist(ev["checklist_id"])
        c = next((x for x in (cl["config"]["criteria"] if cl else []) if x["id"] == a["criterion_id"]), None)
        all_appeals = db.appeals_for(ev["id"])
        o = (ev["answers"] or {}).get(a["criterion_id"]) or {}
        so, sf, orig, _ = scores(ev, all_appeals)
        before = final_answers(orig, [x for x in all_appeals if x["id"] < a["id"]]).get(a["criterion_id"])
        # История решения: итог непосредственно до и после него, без более поздних решений.
        decided = [x for x in all_appeals if x["status"] != "open"]
        prior = (decided if a["status"] == "open" else
                 [x for x in decided if (x["decided_at"] or "", x["id"]) < (a["decided_at"] or "", a["id"])])
        _, score_before, _, _ = scores(ev, prior)
        _, score_after, _, _ = scores(ev, prior + ([a] if a["status"] != "open" else []))
        ev_n = o.get("evidence") or []
        return {"id": a["id"], "evaluation_id": ev["id"], "conversation_id": ev["conversation_id"],
                "channel": conv.get("channel"), "language": conv.get("language"),
                "checklist_label": label(cl), "criterion_id": a["criterion_id"],
                "criterion_title": c.get("title") if c else a["criterion_id"], "criterion_type": c["type"] if c else None,
                "allowed": allowed_answers(c) if c else [], "original_answer": o.get("answer"),
                "current_answer": before, "explanation": o.get("explanation", ""), "evidence": ev_n,
                "evidence_replies": [r for r in (reply(conv["transcript"], n) for n in ev_n) if r],
                "comment": a["comment"], "reason_code": a["reason_code"],
                "status": a["status"], "new_answer": a["new_answer"], "reason": a["reason"],
                "created_at": a["created_at"], "decided_at": a["decided_at"],
                "total_before_decision": score_before["total"] if score_before else ev["total"],
                "total_after_decision": score_after["total"] if score_after else ev["total"],
                "total_original": so["total"] if so else ev["total"], "total_final": sf["total"] if sf else ev["total"]}

    def has_local_audio(cid: str) -> pathlib.Path | None:
        if not _SAFE_ID.match(cid):
            return None
        p = data_dir / "audio" / f"{cid}.mp3"
        return p if p.is_file() else None

    # ---------- маршруты ----------
    @app.post("/api/ask")
    async def ask_question(body: AskIn):
        question = body.question.strip()
        if not question:
            _fail(422, "Напишите вопрос.")
        if body.dataset_id is not None and not db.one("SELECT id FROM datasets WHERE id=?", (body.dataset_id,)):
            _fail(422, "Набор не найден.")
        if body.checklist_id is not None and not checklist(body.checklist_id):
            _fail(422, "Чек-лист не найден.")
        context = ask.build_context(db, body.dataset_id, body.checklist_id)
        if not context["conversations"]:
            return await ask.answer_question(db, question, context, None)
        # Ask всегда использует Haiku; fake включается только явно для тестов.
        pid = "fake" if allow_fake and default_provider() == "fake" else "haiku"
        if not judge.available(pid):
            _fail(422, "Модель недоступна: проверьте настройки провайдера.")
        try:
            provider = ask.FakeAskProvider() if pid == "fake" else judge.get_provider(pid)
            return await ask.answer_question(db, question, context, provider)
        except judge.ProviderError as e:
            _fail(502, str(e))

    @app.get("/api/asks")
    async def asks():
        return db.list_asks()

    @app.get("/api/meta")
    async def meta():
        return {"providers": [{"id": pid, "label": judge.PROVIDERS[pid]["label"], "model": judge.model_name(pid),
                               "available": judge.available(pid), "price_in": judge.PROVIDERS[pid]["price_in"],
                               "price_out": judge.PROVIDERS[pid]["price_out"]} for pid in visible_providers()],
                "default_provider": default_provider(), "dataset_url_configured": bool(dataset_url()),
                "prompt_version": judge.PROMPT_VERSION}

    def settings_view():
        url = dataset_url()
        return {"dataset_url_configured": bool(url), "dataset_url_hint": sources.mask_url(url),
                "default_provider": default_provider()}

    @app.get("/api/settings")
    async def get_settings():
        return settings_view()

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        fields = body.model_fields_set
        if "dataset_url" in fields:
            url = (body.dataset_url or "").strip()
            if url and not url.startswith(("http://", "https://")):
                _fail(422, "Ссылка API должна начинаться с http:// или https://.")
            db.set_setting("dataset_url", url or None)
        if "default_provider" in fields and body.default_provider is not None:
            if body.default_provider not in visible_providers():
                _fail(422, f"Неизвестная модель «{body.default_provider}».")
            db.set_setting("default_provider", body.default_provider)
        return settings_view()

    @app.get("/api/datasets")
    async def datasets():
        return db.list_datasets()

    def analytics_report(dataset_id, checklist_id):
        try:
            return analytics.report(db, dataset_id, checklist_id)
        except ValueError as exc:
            _fail(422, str(exc))

    @app.get("/api/analytics")
    def get_analytics(dataset_id: str | None = None, checklist_id: int | None = None):
        data = analytics_report(dataset_id, checklist_id)
        return {key: value for key, value in data.items() if key != "dialogues"}

    @app.get("/api/analytics/export.csv")
    def export_analytics(dataset_id: str | None = None, checklist_id: int | None = None):
        data = analytics_report(dataset_id, checklist_id)
        return StreamingResponse(iter([analytics.export_csv(data)]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="cqa-analytics.csv"'})

    @app.post("/api/datasets/import")
    async def import_dataset(body: ImportIn):
        gid = (body.group_id or "").strip()
        if not gid:
            _fail(422, "Укажите group_id набора.")
        url = (body.url or "").strip() or dataset_url()
        if not url:
            _fail(422, "Не задан адрес API организаторов: вставьте ссылку GET из задания.")
        try:
            ds_id, total, records = await sources.fetch(url, gid)
        except sources.SourceError as e:
            _fail(422, str(e))
        if body.url and body.url.strip():
            db.set_setting("dataset_url", body.url.strip())
        n = db.save_dataset(ds_id or gid, f"api:{gid}", records, total)
        return {"dataset_id": ds_id or gid, "imported": n, "total": total}

    @app.post("/api/datasets/upload")
    async def upload_dataset(file: UploadFile = File(...)):
        data = await file.read()
        if not data:
            _fail(422, "Файл пустой.")
        try:
            gid, total, records = sources.parse_bytes(data)
        except sources.SourceError as e:
            _fail(422, str(e))
        ds_id = gid or "upload-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        n = db.save_dataset(ds_id, f"file:{file.filename or 'upload.json'}", records, total)
        return {"dataset_id": ds_id, "imported": n, "total": total}

    @app.get("/api/conversations")
    async def conversations(dataset_id: str | None = None, language: str | None = None, channel: str | None = None):
        latest = db.latest_evaluations()
        counts = db.appeal_counts()
        out = []
        for c in db.list_conversations(dataset_id, language, channel):
            ev = latest.get(c["id"])
            if ev:
                s = summary(ev)
                lat = {k: s[k] for k in ("evaluation_id", "checklist_id", "checklist_label", "status", "total",
                                         "final_total", "gross", "final_gross", "created_at", "provider", "model")}
            else:
                lat = None
            out.append({"id": c["id"], "dataset_id": c["dataset_id"], "channel": c["channel"], "language": c["language"],
                        "n_replies": c["n_replies"], "duration": c["duration"],
                        "has_audio": bool(c["audio_url"]) or bool(has_local_audio(c["id"])),
                        "latest": lat, "appeals": counts.get(c["id"], {"open": 0, "decided": 0})})
        return out

    @app.get("/api/conversations/{cid}")
    async def conversation(cid: str):
        c = db.conversation(cid)
        if not c:
            _fail(404, f"Диалог {cid} не найден.")
        tr = c["transcript"]
        offs = audio.audio_offsets(tr)
        items = [{"n": i, "timestamp": t.get("timestamp"), "speaker": t.get("speaker"), "text": t.get("text"),
                  "audio_offset": offs[i - 1], "pii": pii.find(t.get("text") or "")} for i, t in enumerate(tr, 1)]
        has_audio = bool(c["audio_url"]) or bool(has_local_audio(cid))
        return {"id": c["id"], "dataset_id": c["dataset_id"], "channel": c["channel"], "language": c["language"],
                "duration": c["duration"], "audio_src": f"/api/audio/{cid}" if has_audio else None,
                "audio_recorded_s": round(audio.recorded_seconds(tr), 1) if has_audio else None,
                "call_metrics": callmetrics.metrics(c),
                "transcript": items,
                "evaluations": [{**summary(e), "answers": e["answers"]} for e in db.evaluations_for(cid)]}

    @app.get("/api/conversations/{cid}/translation")
    async def conversation_translation(cid: str):
        if not db.conversation(cid):
            _fail(404, f"Диалог {cid} не найден.")
        return db.translation(cid)

    @app.post("/api/conversations/{cid}/translate")
    async def conversation_translate(cid: str, body: TranslateIn | None = None):
        # Соседние запросы к одному диалогу ждут тот же перевод, без повторной оплаты.
        async with precedents.translation_lock(cid):  # тот же замок, что у перевода для поиска похожих
            record = db.record(cid)
            if not record:
                _fail(404, f"Диалог {cid} не найден.")
            saved = db.translation(cid)
            if saved["translation"]:
                return saved
            if (record.get("language") or "").strip().lower() == "ru":
                _fail(422, "Диалог уже на русском.")
            if not record["transcript"]:
                _fail(422, "Транскрипт пуст.")
            pid = (body or TranslateIn()).provider
            if pid == "fake" and not allow_fake:
                _fail(422, "Тестовый провайдер отключён.")
            if not judge.available(pid):
                _fail(422, "Модель недоступна: проверьте настройки провайдера.")
            provider = translate.FakeTranslationProvider() if pid == "fake" else judge.get_provider(pid)
            try:
                return await translate.translate(db, record, provider)
            except judge.ProviderError as e:
                _fail(502, str(e))

    @app.get("/api/evaluations/{ev_id}")
    async def evaluation(ev_id: int):
        ev = db.evaluation(ev_id)
        if not ev:
            _fail(404, f"Оценка {ev_id} не найдена.")
        return eval_detail(ev)

    @app.post("/api/insights/run", status_code=202)
    async def start_insights(body: InsightsIn):
        if body.conversation_ids is not None:
            ids = list(dict.fromkeys(x.strip() for x in body.conversation_ids if x.strip()))
        elif body.dataset_id:
            ids = db.conversation_ids(body.dataset_id)
        else:
            ids = []
        if not ids:
            _fail(422, "Выберите диалоги или набор для разбора.")
        snapshots = []
        for cid in ids:
            rec = db.record(cid)
            if not rec or (body.dataset_id and db.conversation(cid)["dataset_id"] != body.dataset_id):
                _fail(422, f"Диалог {cid} не найден в выбранном наборе.")
            row = db.one("SELECT id FROM evaluations WHERE conversation_id=? AND status='ok' ORDER BY id DESC LIMIT 1", (cid,))
            if not row:
                _fail(422, f"Сначала оцените диалог {cid}.")
            ev = db.evaluation(row["id"])
            snapshots.append((rec, ev, db.checklist(ev["checklist_id"])["config"]))
        pid = default_provider()
        if pid not in visible_providers() or not judge.available(pid):
            _fail(422, "Модель разбора недоступна. Проверьте модель по умолчанию в настройках.")
        try:
            provider = judge.get_provider(pid)
            insight_ids = insights_runner.start(snapshots, provider)
        except judge.ProviderError as e:
            _fail(422, str(e))
        return {"insight_ids": insight_ids, "conversation_ids": ids}

    @app.get("/api/insights")
    async def list_insights(dataset_id: str | None = None):
        return db.list_insights(dataset_id)

    @app.get("/api/insights/{conversation_id}")
    async def get_insight(conversation_id: str):
        if not db.conversation(conversation_id):
            _fail(404, "Диалог не найден.")
        return db.latest_insight(conversation_id)
    @app.post("/api/calibrations", status_code=201)
    async def create_calibration(body: CalibrationIn):
        if not db.conversation(body.conversation_id):
            _fail(404, "Диалог не найден.")
        if body.checklist_id is not None and not checklist(body.checklist_id):
            _fail(422, "Чек-лист не найден.")
        ev = db.calibration_evaluation(body.conversation_id, body.checklist_id)
        if not ev:
            _fail(409, "Сначала нужна успешная оценка ИИ по этому чек-листу.")
        cfg = checklist(ev["checklist_id"])["config"]
        expected = {c["id"] for c in cfg["criteria"]}
        if set(body.answers) != expected:
            _fail(422, "Ответьте на все критерии чек-листа без дополнительных критериев.")
        for c in cfg["criteria"]:
            if body.answers[c["id"]] not in allowed_answers(c):
                _fail(422, f"Критерий {c['id']}: допустимо {', '.join(allowed_answers(c))}.")
        # Проверяем сравнение до записи: неполная старая оценка не создаёт калибровку.
        try:
            calibrations.compare({"answers": body.answers}, ev, cfg)
        except (ScoreError, KeyError, TypeError):
            _fail(409, "Оценка ИИ неполная — выберите другую оценку.")
        row = db.create_calibration(ev, body.answers, (body.reviewer or "").strip(), (body.comment or "").strip())
        return calibrations.compare(row, ev, cfg)

    @app.get("/api/calibrations")
    async def list_calibrations(conversation_id: str | None = None, dataset_id: str | None = None):
        items = [calibrations.compare(row, db.evaluation(row["evaluation_id"]),
                                     checklist(row["checklist_id"])["config"])
                 for row in db.list_calibrations(conversation_id, dataset_id)]
        return {"items": items, "summary": calibrations.summarize(items)}

    def run_input(body: RunIn):
        if body.checklist_id is None:
            body.checklist_id = max((c["id"] for c in db.list_checklists()), default=None)
        if body.checklist_id is None or not checklist(body.checklist_id):
            _fail(422, "Чек-лист не найден.")
        pid = body.provider or default_provider()
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        if not judge.available(pid):
            _fail(422, "Модель недоступна: " + ("нет ключа ANTHROPIC_API_KEY." if judge.PROVIDERS[pid]["kind"] ==
                                                "anthropic" else "не задан адрес CQA_LOCAL_BASE_URL."))
        if body.conversation_ids:
            ids = list(dict.fromkeys(x.strip() for x in body.conversation_ids if x and x.strip()))
            missing = [x for x in ids if not db.conversation(x)]
            if missing:
                _fail(422, "Диалоги не найдены: " + ", ".join(missing[:10]) + ("…" if len(missing) > 10 else ""))
        elif body.dataset_id:
            ids = db.conversation_ids(body.dataset_id)
            if not ids:
                _fail(422, f"В наборе {body.dataset_id} нет диалогов.")
        else:
            _fail(422, "Выберите диалоги или набор для оценки.")
        if not ids:
            _fail(422, "Выберите диалоги или набор для оценки.")
        return ids, pid

    @app.post("/api/runs/plan")
    async def run_plan(body: RunIn):
        ids, pid = run_input(body)
        out = plan(db, ids, body.checklist_id, judge.model_name(pid), body.mode)
        out["model_calls"] *= runner.votes if body.votes is None else body.votes
        return out

    @app.post("/api/runs")
    async def start_run(body: RunIn):
        ids, pid = run_input(body)
        try:
            run_id = runner.start(ids, body.checklist_id, pid, body.mode, votes=body.votes)
        except judge.ProviderError as e:
            _fail(422, str(e))
        return {"run_id": run_id}

    def run_view(r: dict, items: bool) -> dict:
        cl = checklist(r["checklist_id"])
        out = {"id": r["id"], "status": r["status"], "checklist_id": r["checklist_id"], "checklist_label": label(cl),
               "provider": r["provider"], "model": r["model"], "total": r["total"], "done": r["done"],
               "failed": r["failed"], "cost_usd": round(r["cost_usd"] or 0, 6), "created_at": r["created_at"],
               "finished_at": r["finished_at"], "mode": r["mode"], **db.run_counts(r["id"])}
        st = r if "tokens_in" in r else db.run_stats(r["id"])
        out.update({"avg_cost_per_eval": round(st["avg_cost_per_eval"], 6) if st["avg_cost_per_eval"] is not None else None,
                    "avg_latency_s": round(st["avg_latency_s"], 2) if st["avg_latency_s"] is not None else None,
                    "tokens_in": st["tokens_in"], "tokens_out": st["tokens_out"]})
        if items:
            out["items"] = [{"conversation_id": i["conversation_id"], "status": i["status"],
                             "evaluation_id": i["evaluation_id"], "total": i["total"],
                             "gross": None if i["gross"] is None else bool(i["gross"]), "error": i["error"]}
                            for i in db.run_items(r["id"])]
        return out

    @app.get("/api/runs")
    async def runs():
        return [run_view(r, False) for r in db.list_runs()]

    @app.get("/api/runs/{run_id}")
    async def run(run_id: int):
        r = db.run(run_id)
        if not r:
            _fail(404, f"Прогон {run_id} не найден.")
        return run_view(r, True)

    @app.get("/api/runs/{run_id}/diff")
    async def run_diff(run_id: int):
        r = db.run(run_id)
        if not r:
            _fail(404, f"Прогон {run_id} не найден.")
        counts, items = {}, []
        for item in db.run_items(run_id):
            previous = db.evaluation(item["previous_evaluation_id"]) if item["previous_evaluation_id"] else None
            current = db.evaluation(item["evaluation_id"]) if item["evaluation_id"] else None
            before = previous["answers"] if previous else {}
            after = current["answers"] if current else {}
            changes = []
            # Сравниваем исходные ответы; решения проверяющего принадлежат своей оценке.
            if current and current["status"] == "ok":
                for cid in dict.fromkeys([*before, *after]):
                    a, b = before.get(cid, {}).get("answer"), after.get(cid, {}).get("answer")
                    if a != b:
                        changes.append({"id": cid, "before": a, "after": b})
                        counts[cid] = counts.get(cid, 0) + 1

            def brief(ev):
                return {"id": ev["id"], "total": ev["total"],
                        "gross": None if ev["gross"] is None else bool(ev["gross"]),
                        "status": ev["status"]} if ev else None

            items.append({"conversation_id": item["conversation_id"], "status": item["status"],
                          "error": item["error"], "previous": brief(previous), "current": brief(current),
                          "changes": changes})
        cfg = checklist(r["checklist_id"])["config"]
        titles = {c["id"]: c["title"] for c in cfg["criteria"]}
        for item in items:
            if item["previous"]:
                old = db.evaluation(item["previous"]["id"])
                for c in checklist(old["checklist_id"])["config"]["criteria"]:
                    titles.setdefault(c["id"], c["title"])
        return {"run_id": run_id, "items": items,
                "criteria": [{"id": cid, "title": title, "changed": counts.get(cid, 0)}
                             for cid, title in titles.items()]}

    @app.get("/api/checklists")
    async def checklists():
        return [{**c, "label": label(c)} for c in db.list_checklists()]

    @app.get("/api/checklists/{cl_id}")
    async def checklist_one(cl_id: int):
        cl = checklist(cl_id)
        if not cl:
            _fail(404, f"Чек-лист {cl_id} не найден.")
        return {"id": cl["id"], "name": cl["name"], "version": cl["version"], "parent_id": cl["parent_id"],
                "note": cl["note"], "created_at": cl["created_at"], "label": label(cl), "config": cl["config"]}

    @app.post("/api/checklists/validate")
    async def checklist_validate(body: ValidateIn):
        errs = validate(normalize(body.config))
        return {"ok": not errs, "errors": errs}

    @app.post("/api/checklists/assist")
    async def checklist_assist(body: AssistIn):
        try:
            assist.build_user(body.config, body.criterion_id)
        except ValueError as e:
            _fail(422, str(e))
        pid = body.provider or default_provider()
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        if not judge.available(pid):
            _fail(422, "Модель недоступна: проверьте настройки провайдера.")
        try:
            return await assist.draft_guidance(body.config, body.criterion_id, judge.get_provider(pid))
        except judge.ProviderError as e:
            _fail(502, str(e))

    @app.post("/api/checklists/try")
    async def checklist_try(body: TryIn):
        cfg = normalize(body.config) if isinstance(body.config.get("criteria"), list) else body.config
        errs = validate(cfg)
        if errs:
            _fail(422, "Чек-лист не проверен: исправьте ошибки.", errs)
        pid = body.provider or default_provider()
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        if not judge.available(pid):
            _fail(422, "Модель недоступна: проверьте настройки провайдера.")
        try:
            return await sandbox.preview(db, cfg, judge.get_provider(pid), body.conversation_ids, body.criteria)
        except ValueError as e:
            _fail(422, str(e))
        except judge.ProviderError as e:
            _fail(502, str(e))

    @app.post("/api/checklists/check-approved")
    async def checklist_check_approved(body: CheckApprovedIn):
        cfg = normalize(body.config) if isinstance(body.config.get("criteria"), list) else body.config
        errs = validate(cfg)
        if errs:
            _fail(422, "Чек-лист не проверен: исправьте ошибки.", errs)
        pid = body.provider or default_provider()
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        try:
            return await crittest.check_approved(db, cfg, judge.get_provider(pid), body.criteria, body.limit)
        except ValueError as e:
            _fail(422, str(e))

    @app.get("/api/checklist-checks/calls")
    async def checklist_test_calls():
        return db.criterion_test_calls()

    @app.post("/api/checklists", status_code=201)
    async def checklist_create(body: ChecklistIn):
        cfg = normalize(body.config)
        errs = validate(cfg)
        if errs:
            return JSONResponse({"detail": "Чек-лист не сохранён: исправьте ошибки.", "errors": errs}, status_code=422)
        if body.parent_id is not None and not checklist(body.parent_id):
            _fail(422, f"Исходная версия {body.parent_id} не найдена.")
        cl_id, version = db.create_checklist(cfg, note=(body.note or "").strip(), parent_id=body.parent_id)
        return {"id": cl_id, "version": version, "label": f"{cfg['name']} · v{version}"}

    @app.get("/api/appeals")
    async def appeals(status: str = Query("all", pattern="^(open|decided|all)$")):
        cache_ev: dict[int, dict] = {}
        cache_conv: dict[str, dict] = {}
        out = []
        for a in db.list_appeals(status):
            ev = cache_ev.get(a["evaluation_id"]) or db.evaluation(a["evaluation_id"])
            if not ev:
                continue
            cache_ev[ev["id"]] = ev
            conv = cache_conv.get(ev["conversation_id"]) or db.conversation(ev["conversation_id"]) or {"transcript": []}
            cache_conv[ev["conversation_id"]] = conv
            out.append(appeal_full(a, ev, conv))
        return out

    @app.post("/api/appeals", status_code=201)
    async def appeal_create(body: AppealIn):
        ev = db.evaluation(body.evaluation_id)
        if not ev:
            _fail(404, f"Оценка {body.evaluation_id} не найдена.")
        if ev["status"] != "ok":
            _fail(422, "Оценка завершилась ошибкой — оспаривать нечего, запустите оценку заново.")
        cl = checklist(ev["checklist_id"])
        if not cl or not any(c["id"] == body.criterion_id for c in cl["config"]["criteria"]):
            _fail(422, f"В чек-листе этой оценки нет критерия {body.criterion_id}.")
        comment = (body.comment or "").strip()
        if not comment:
            _fail(422, "Добавьте комментарий: что именно вы оспариваете.")
        if db.open_appeal(ev["id"], body.criterion_id):
            _fail(409, "По этому критерию уже есть открытая апелляция.")
        a_id = db.create_appeal(ev["id"], body.criterion_id, comment, body.reason_code)
        return appeal_full(db.appeal(a_id), ev)

    @app.post("/api/appeals/{appeal_id}/decision")
    async def appeal_decide(appeal_id: int, body: DecisionIn):
        a = db.appeal(appeal_id)
        if not a:
            _fail(404, f"Апелляция {appeal_id} не найдена.")
        if a["status"] != "open":
            _fail(409, "Апелляция уже рассмотрена.")
        reason = (body.reason or "").strip()
        if not reason:
            _fail(422, "Укажите причину решения.")
        if body.decision not in ("confirm", "change"):
            _fail(422, "Решение — «confirm» (подтвердить) или «change» (изменить).")
        ev = db.evaluation(a["evaluation_id"])
        cl = checklist(ev["checklist_id"])
        c = next((x for x in cl["config"]["criteria"] if x["id"] == a["criterion_id"]), None) if cl else None
        new_answer = None
        if body.decision == "change":
            new_answer = (body.new_answer or "").strip()
            allowed = allowed_answers(c) if c else []
            if new_answer not in allowed:
                _fail(422, f"Новый ответ должен быть одним из: {', '.join(allowed)}.")
            _, _, orig, _ = scores(ev)
            current = final_answers(orig, [x for x in db.appeals_for(ev["id"]) if x["id"] < a["id"]]).get(a["criterion_id"])
            if new_answer == current:
                _fail(422, "Новый ответ совпадает с текущим — выберите «Подтвердить».")
        db.decide_appeal(appeal_id, "changed" if body.decision == "change" else "confirmed", new_answer, reason)
        ev = db.evaluation(a["evaluation_id"])
        return {"appeal": appeal_full(db.appeal(appeal_id), ev), "evaluation": eval_detail(ev)}

    # ---------- «модель спрашивает»: спорные решения → вопросы к чек-листу ----------
    def doubt_ids(dataset_id: str | None) -> list[str] | None:
        if dataset_id is None:
            return None
        ids = db.conversation_ids(dataset_id)
        if not ids:
            _fail(404, f"Набор {dataset_id} не найден.")
        return ids

    @app.get("/api/doubts")
    async def doubts_summary(dataset_id: str | None = None):
        return doubts.summary(db, doubt_ids(dataset_id))

    @app.post("/api/doubts/questions")
    async def doubts_questions(body: DoubtQuestionsIn):
        if body.evaluation_id is not None:
            ev = db.evaluation(body.evaluation_id)
            if not ev:
                _fail(404, f"Оценка {body.evaluation_id} не найдена.")
            one = doubts.dispute(db, ev, body.criterion_id)
            disputes = [one] if one else []
        else:
            disputes = doubts.collect(db, conversation_ids=doubt_ids(body.dataset_id), criteria={body.criterion_id})
        if not disputes:
            _fail(422, "Оценки модели по этому критерию не расходятся — спрашивать не о чем.")
        disputes, cl_id = doubts.scope(disputes)
        cl = checklist(cl_id)
        if not cl:
            _fail(422, f"Чек-лист {cl_id} не найден.")
        brief = [{k: d[k] for k in ("conversation_id", "evaluation_id", "answer", "agreement", "votes")} for d in disputes]
        key = doubts.cache_key(cl_id, body.criterion_id, disputes)
        cached = None if body.refresh else db.doubt_questions(key)
        if cached:
            return {"key": key, "criterion_id": body.criterion_id, "checklist_id": cl_id, "disputes": brief,
                    "questions": cached["questions"], "model": cached["model"], "cost_usd": 0.0,
                    "latency_s": 0.0, "cached": True}
        pid = body.provider or default_provider()
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        if not judge.available(pid):
            _fail(422, "Модель недоступна: проверьте настройки провайдера.")
        try:
            result = await doubts.ask(db, cl["config"], body.criterion_id, disputes, judge.get_provider(pid))
        except ValueError as e:
            _fail(422, str(e))
        except judge.ProviderError as e:
            _fail(502, str(e))
        db.save_doubt_questions(key, cl_id, body.criterion_id, [d["evaluation_id"] for d in disputes], result)
        return {"key": key, "criterion_id": body.criterion_id, "checklist_id": cl_id, "disputes": brief,
                **result, "cached": False}

    @app.post("/api/doubts/resolve")
    async def doubts_resolve(body: DoubtResolveIn):
        """Ответ проверяющего на сомнение модели — решённая апелляция с кодом model_doubt (итог пересчитан)."""
        ev = db.evaluation(body.evaluation_id)
        if not ev:
            _fail(404, f"Оценка {body.evaluation_id} не найдена.")
        cl = checklist(ev["checklist_id"])
        c = next((x for x in cl["config"]["criteria"] if x["id"] == body.criterion_id), None) if cl else None
        if c is None:
            _fail(422, f"В чек-листе этой оценки нет критерия {body.criterion_id}.")
        if body.answer not in allowed_answers(c):
            _fail(422, f"Ответ должен быть одним из: {', '.join(allowed_answers(c))}.")
        appeals = db.appeals_for(ev["id"])
        if any(a["criterion_id"] == body.criterion_id and a["reason_code"] == "model_doubt" for a in appeals):
            _fail(409, "Сомнение модели по этому критерию уже разрешено.")
        if db.open_appeal(ev["id"], body.criterion_id):
            _fail(409, "По этому критерию открыта апелляция оператора — решите её в разделе «Апелляции».")
        d = doubts.dispute(db, ev, body.criterion_id)
        if d is None:
            _fail(422, "Оценки модели по этому критерию не расходятся — для пересмотра есть апелляция.")
        _, _, orig, _ = scores(ev, appeals)
        current = final_answers(orig, appeals).get(body.criterion_id)
        votes = " · ".join(f"{p['answer']} ×{p['count']}" for p in d["positions"])
        question = (body.question or "").strip() or f"Оценки модели разошлись: {votes}"
        reason = (body.reason or "").strip() or f"Проверяющий выбрал «{body.answer}» (голоса модели: {votes})."
        a_id = db.create_appeal(ev["id"], body.criterion_id, question, "model_doubt")
        if body.answer == current:
            db.decide_appeal(a_id, "confirmed", None, reason)
        else:
            db.decide_appeal(a_id, "changed", body.answer, reason)
        ev = db.evaluation(ev["id"])
        return {"appeal": appeal_full(db.appeal(a_id), ev), "evaluation": eval_detail(ev)}

    @app.post("/api/guard")
    async def guard_check(body: GuardIn):
        """Щит до отправки: черновик ответа оператора по действующему чек-листу (cqa/guard.py)."""
        rec = db.record(body.conversation_id)
        if not rec:
            _fail(404, f"Диалог {body.conversation_id} не найден.")
        cl = checklist(max(c["id"] for c in db.list_checklists()))
        # как «Спросить ИИ»: Haiku; fake — только явно для тестов
        pid = body.provider or ("fake" if allow_fake and default_provider() == "fake" else "haiku")
        if pid not in visible_providers():
            _fail(422, f"Неизвестная модель «{pid}».")
        if not judge.available(pid):
            _fail(422, "Модель недоступна: проверьте настройки провайдера.")
        try:
            provider = guard.FakeGuardProvider() if pid == "fake" else judge.get_provider(pid)
            return {**await guard.check(rec, body.upto, body.draft, cl["config"], provider),
                    "checklist_label": label(cl)}
        except ValueError as e:
            _fail(422, str(e))
        except judge.ProviderError as e:
            _fail(502, str(e))

    @app.get("/api/audio/{cid}")
    async def audio_file(cid: str, request: Request):
        local = has_local_audio(cid)
        if local:
            return FileResponse(local, media_type="audio/mpeg")
        c = db.conversation(cid)
        if not c or not c["audio_url"]:
            _fail(404, "У диалога нет записи.")
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True)
        headers = {"Range": request.headers["range"]} if "range" in request.headers else {}
        try:
            for attempt in range(2):
                mp3 = await audio.resolve_mp3(c["audio_url"], client)
                r = await client.send(client.build_request("GET", mp3, headers=headers), stream=True)
                if r.status_code in (401, 403, 404, 410) and attempt == 0:  # ссылка протухла — берём свежую
                    await r.aclose()
                    audio.forget(c["audio_url"])
                    continue
                break
        except (audio.AudioError, httpx.HTTPError) as e:
            await client.aclose()
            _fail(502, f"Запись недоступна: {e}")
        if r.status_code not in (200, 206):
            await r.aclose()
            await client.aclose()
            _fail(502, f"Хранилище записей ответило {r.status_code}.")
        out_headers = {"Accept-Ranges": "bytes"}
        for h in ("content-length", "content-range"):
            if h in r.headers:
                out_headers[h.title()] = r.headers[h]

        async def close():
            await r.aclose()
            await client.aclose()

        ctype = r.headers.get("content-type", "audio/mpeg")
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out_headers,
                                 media_type=ctype if ctype.startswith("audio/") else "audio/mpeg",
                                 background=BackgroundTask(close))

    # интерфейс: / → web/index.html и статика (каталог может появиться позже — check_dir=False)
    app.mount("/", StaticFiles(directory=str(web_dir), html=True, check_dir=False), name="web")
    return app
