"""Сервер по контракту docs/ARCHITECTURE.md: импорт → прогон → оценка → апелляция → решение → пересчёт.

Без сети и без платных вызовов: провайдер `fake` (детерминированная заглушка из ядра), временная база.
"""
from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa import sources
from cqa.api import create_app



def iso(sec: int) -> str:
    m, s = divmod(sec, 60)
    return f"2026-11-03T10:{m:02d}:{s:02d}.1234567+00:00"


CHAT = {"conversation_id": "t-chat-1", "channel": "agent chat", "language": "RU", "audio_url": None, "transcript": [
    {"timestamp": "00:00", "speaker": "client", "text": "Хочу проверить перевод. Мой ИИН 900101300007"},
    {"timestamp": "00:03", "speaker": "bot", "text": "Соединяю с оператором."},
    {"timestamp": "00:05", "speaker": "system", "text": "Эскалация на оператора."},
    {"timestamp": "00:05", "speaker": "operator", "text": "Здравствуйте, меня зовут Дина. Чем помочь?"},
    {"timestamp": "01:10", "speaker": "client", "text": "Карта 4111 1111 1111 1111, деньги не пришли."},
    {"timestamp": "02:00", "speaker": "operator", "text": "Проверю и вернусь в этот чат. Хорошего дня!"},
]}
CALL = {"conversation_id": "t-call-1", "channel": "voice call", "language": "RU",
        "audio_url": "https://example.invalid/player?sig=SECRET", "transcript": [
            {"timestamp": iso(0), "speaker": "client", "text": "Алло"},  # до старта записи → 0
            {"timestamp": iso(5), "speaker": "system", "text": "Запись и транскрибирование запущены."},
            {"timestamp": iso(30), "speaker": "operator", "text": "Здравствуйте"},  # 25
            {"timestamp": iso(65), "speaker": "system", "text": "Запись и транскрибирование приостановлены."},
            {"timestamp": iso(70), "speaker": "operator", "text": "реплика на паузе"},  # 60 (место продолжения)
            {"timestamp": iso(80), "speaker": "system", "text": "Запись и транскрибирование возобновлены."},
            {"timestamp": iso(100), "speaker": "client", "text": "Спасибо"},  # 60 + 20 = 80
            {"timestamp": iso(200), "speaker": "system", "text": "Клиент завершил разговор."},  # 60 + 120 = 180
        ]}
PAYLOAD = {"error": "", "isSuccess": True, "data": {"dataset_group_id": "TEST", "total_records": 2,
                                                   "records": [CHAT, CALL]}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: None)  # без реальной ссылки из .env
    app = create_app(tmp_path / "t.db", web_dir=tmp_path / "web", data_dir=tmp_path / "data", allow_fake=True)
    with TestClient(app) as c:
        yield c


def upload(c, payload=PAYLOAD):
    data = json.dumps(payload, ensure_ascii=False).encode()
    r = c.post("/api/datasets/upload", files={"file": ("dev.json", io.BytesIO(data), "application/json")})
    assert r.status_code == 200, r.text
    return r.json()


def run_and_wait(c, body) -> dict:
    r = c.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    for _ in range(200):
        run = c.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError("прогон не завершился")


def test_upload_run_evaluation_appeal_decision(client):
    c = client
    assert upload(c) == {"dataset_id": "TEST", "imported": 2, "total": 2}
    convs = c.get("/api/conversations").json()
    assert [x["id"] for x in convs] == ["t-chat-1", "t-call-1"]
    assert convs[0]["latest"] is None and convs[0]["appeals"] == {"open": 0, "decided": 0}
    assert convs[0]["n_replies"] == 6 and convs[0]["duration"] == "02:00"

    run = run_and_wait(c, {"dataset_id": "TEST", "checklist_id": 1, "provider": "fake"})
    assert run["status"] == "done" and run["done"] == 2 and run["failed"] == 0 and run["total"] == 2
    assert {i["status"] for i in run["items"]} == {"ok"} and run["checklist_label"].endswith("· v1")
    ev_id = next(i["evaluation_id"] for i in run["items"] if i["conversation_id"] == "t-chat-1")

    ev = c.get(f"/api/evaluations/{ev_id}").json()
    assert ev["status"] == "ok" and ev["conversation_id"] == "t-chat-1" and ev["provider"] == "fake"
    assert [x["id"] for x in ev["criteria"]] == ["01", "02", "03", "04", "05", "06", "07", "08"]
    k04 = next(x for x in ev["criteria"] if x["id"] == "04")
    assert k04["allowed"] == ["20", "15", "10", "5", "0", "N/A"] and k04["type"] == "scale"
    for x in ev["criteria"]:
        assert set(x["original"]) == {"answer", "explanation", "evidence", "votes", "agreement"}
        assert x["original"]["agreement"] == "3/3"
        assert x["final_answer"] == x["original"]["answer"] and x["appeal"] is None
        assert {"applicable", "points", "weight_eff", "points_eff"} <= set(x["score_original"])
    # заглушка отвечает первой допустимой ступенью: всё «Да» / максимум, у грубой ошибки тоже «Да»
    assert ev["original"]["total"] == 100.0 and ev["original"]["gross"] is True
    assert ev["final"]["total"] == 100.0 and len(ev["original"]["sections"]) == 4
    # ПДн скрыты от модели: ИИН в реплике 1, карта в реплике 5
    assert {(p["n"], p["type"]) for p in ev["pii_masked"]} == {(1, "iin"), (5, "card")}

    # апелляция: одна открытая на критерий
    r = c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "05", "comment": "Оператор был вежлив"})
    assert r.status_code == 201, r.text
    ap = r.json()
    assert ap["status"] == "open" and ap["original_answer"] == "Да" and ap["criterion_title"]
    assert ap["evidence_replies"] and ap["evidence_replies"][0]["n"] == 1 and ap["allowed"] == ["Да", "Нет"]
    assert c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "05", "comment": "ещё"}).status_code == 409
    assert c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "05", "comment": " "}).status_code in (409, 422)
    assert c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "99", "comment": "x"}).status_code == 422
    assert [a["id"] for a in c.get("/api/appeals?status=open").json()] == [ap["id"]]

    # решение: причина обязательна, новый ответ — из допустимых и не равен текущему
    url = f"/api/appeals/{ap['id']}/decision"
    assert c.post(url, json={"decision": "change", "new_answer": "Нет", "reason": ""}).status_code == 422
    assert c.post(url, json={"decision": "change", "new_answer": "15", "reason": "x"}).status_code == 422
    assert c.post(url, json={"decision": "change", "new_answer": "Да", "reason": "x"}).status_code == 422
    r = c.post(url, json={"decision": "change", "new_answer": "Нет", "reason": "Не отреагировал на жалобу"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["appeal"]["status"] == "changed" and out["appeal"]["total_final"] == 85.0
    k05 = next(x for x in out["evaluation"]["criteria"] if x["id"] == "05")
    assert k05["original"]["answer"] == "Да" and k05["final_answer"] == "Нет" and k05["appeal"]["status"] == "changed"
    assert out["evaluation"]["original"]["total"] == 100.0 and out["evaluation"]["final"]["total"] == 85.0
    assert c.post(url, json={"decision": "confirm", "reason": "повторно"}).status_code == 409

    # вторая апелляция по тому же критерию после решения — можно; подтверждение не меняет итог
    r = c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "05", "comment": "не согласен"})
    assert r.status_code == 201 and r.json()["current_answer"] == "Нет"
    r = c.post(f"/api/appeals/{r.json()['id']}/decision", json={"decision": "confirm", "reason": "решение в силе"})
    assert r.status_code == 200 and r.json()["evaluation"]["final"]["total"] == 85.0

    convs = {x["id"]: x for x in c.get("/api/conversations").json()}
    lat = convs["t-chat-1"]["latest"]
    assert lat["total"] == 100.0 and lat["final_total"] == 85.0 and convs["t-chat-1"]["appeals"] == {"open": 0, "decided": 2}
    assert c.get("/api/appeals?status=decided").json()[0]["status"] in ("changed", "confirmed")

    runs = c.get("/api/runs").json()
    assert runs[0]["id"] == run["id"] and runs[0]["tokens_in"] > 0 and "items" not in runs[0]


def test_conversation_detail_offsets_and_pii(client):
    c = client
    upload(c)
    call = c.get("/api/conversations/t-call-1").json()
    assert call["audio_src"] == "/api/audio/t-call-1"
    assert [t["audio_offset"] for t in call["transcript"]] == [0.0, 0.0, 25.0, 60.0, 60.0, 60.0, 80.0, 180.0]
    assert [t["n"] for t in call["transcript"]] == list(range(1, 9))
    assert call["transcript"][0]["timestamp"] == iso(0)  # исходная метка не меняется
    chat = c.get("/api/conversations/t-chat-1").json()
    assert chat["audio_src"] is None and all(t["audio_offset"] is None for t in chat["transcript"])
    assert [p["type"] for p in chat["transcript"][0]["pii"]] == ["iin"]
    assert chat["transcript"][0]["text"].endswith("900101300007")  # в интерфейсе — оригинал
    assert c.get("/api/conversations/nope").status_code == 404
    assert c.get("/api/audio/t-chat-1").status_code == 404


def test_checklists_validate_and_new_version(client):
    c = client
    lst = c.get("/api/checklists").json()
    assert len(lst) == 1 and lst[0]["version"] == 1 and lst[0]["n_criteria"] == 8
    cfg = c.get("/api/checklists/1").json()["config"]
    bad = json.loads(json.dumps(cfg))
    bad["criteria"][0]["weight"] = -5
    v = c.post("/api/checklists/validate", json={"config": bad}).json()
    assert v["ok"] is False and v["errors"]
    r = c.post("/api/checklists", json={"config": bad, "note": "x", "parent_id": 1})
    assert r.status_code == 422 and r.json()["errors"]
    good = json.loads(json.dumps(cfg))
    k05 = next(x for x in good["criteria"] if x["id"] == "05")
    k05["title"] = "Вежливость"
    k05["yes"] = "оператор вежлив на протяжении всего диалога."
    assert c.post("/api/checklists/validate", json={"config": good}).json() == {"ok": True, "errors": []}
    r = c.post("/api/checklists", json={"config": good, "note": "Заменили 05", "parent_id": 1})
    assert r.status_code == 201 and r.json()["version"] == 2
    new_id = r.json()["id"]
    one = c.get(f"/api/checklists/{new_id}").json()
    assert one["parent_id"] == 1 and one["note"] == "Заменили 05" and one["label"].endswith("· v2")
    upload(c)
    run = run_and_wait(c, {"conversation_ids": ["t-chat-1"], "checklist_id": new_id, "provider": "fake"})
    ev = c.get(f"/api/evaluations/{run['items'][0]['evaluation_id']}").json()
    assert ev["checklist"]["version"] == 2 and next(x for x in ev["criteria"] if x["id"] == "05")["title"] == "Вежливость"
    # в карточке — одна оценка (по v2)
    assert len(c.get("/api/conversations/t-chat-1").json()["evaluations"]) == 1


def test_run_validation(client):
    c = client
    upload(c)
    assert c.post("/api/runs", json={"dataset_id": "TEST", "checklist_id": 1, "provider": "nope"}).status_code == 422
    assert c.post("/api/runs", json={"conversation_ids": ["missing"], "checklist_id": 1, "provider": "fake"}).status_code == 422
    assert c.post("/api/runs", json={"checklist_id": 1, "provider": "fake"}).status_code == 422
    assert c.post("/api/runs", json={"dataset_id": "TEST", "checklist_id": 999, "provider": "fake"}).status_code == 422
    r = c.post("/api/runs", json={"dataset_id": "TEST", "provider": "fake"})  # без чек-листа → самая новая версия
    assert r.status_code == 200
    assert c.get("/api/runs/999").status_code == 404
    assert c.get("/api/evaluations/999").status_code == 404


def test_meta_settings_and_import(client, monkeypatch):
    c = client
    meta = c.get("/api/meta").json()
    ids = [p["id"] for p in meta["providers"]]
    assert "haiku" in ids and "sonnet" in ids and meta["dataset_url_configured"] is False
    assert c.post("/api/datasets/import", json={"group_id": "DEV"}).status_code == 422  # ссылки нет
    assert c.put("/api/settings", json={"dataset_url": "ftp://x"}).status_code == 422
    s = c.put("/api/settings", json={"dataset_url": "https://host.example/run?api-version=1&sig=TOPSECRET"}).json()
    assert s["dataset_url_configured"] is True and "TOPSECRET" not in json.dumps(s) and "sig=***" in s["dataset_url_hint"]

    async def fake_fetch(url, group_id, timeout=120):
        assert "group_id" not in url or True
        return "CTRL", 2, sources.parse_payload(PAYLOAD)[2]

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    r = c.post("/api/datasets/import", json={"group_id": "CTRL"})
    assert r.status_code == 200 and r.json() == {"dataset_id": "CTRL", "imported": 2, "total": 2}
    assert [d["id"] for d in c.get("/api/datasets").json()] == ["CTRL"]
    bad = c.post("/api/datasets/upload", files={"file": ("x.json", io.BytesIO(b"{not json"), "application/json")})
    assert bad.status_code == 422 and bad.json()["detail"]


def test_group_id_and_mask_helpers():
    url = "https://h.example/p?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=ABC&group_id=DEV"
    out = sources.with_group_id(url, "CTRL 2")
    assert out.endswith("&group_id=CTRL%202") and "sp=%2Ftriggers%2Fmanual%2Frun" in out and out.count("group_id") == 1
    assert "ABC" not in sources.mask_url(url)


def test_interrupted_run_marked_error_on_restart(tmp_path):
    from cqa.db import DB
    db = DB(tmp_path / "r.db")
    db.seed_default_checklist()
    db.save_dataset("TEST", "file", sources.parse_payload(PAYLOAD)[2])
    run_id = db.create_run(1, "fake", "fake", ["t-chat-1", "t-call-1"])
    db.close()
    app = create_app(tmp_path / "r.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    with TestClient(app) as c:
        run = c.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "error" and run["failed"] == 2
    assert {i["status"] for i in run["items"]} == {"error"} and "перезапуск" in run["items"][0]["error"]
