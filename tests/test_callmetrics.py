"""Метрики без модели, сети и аудио: исходные номера и ISO с семью знаками дроби."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cqa import sources
from cqa.api import create_app
from cqa.callmetrics import metrics


ROOT = Path(__file__).resolve().parents[1]


def call(rows):
    start = dt.datetime(2026, 11, 3, 15, 49, tzinfo=dt.timezone.utc)
    return {"conversation_id": "metrics-call", "channel": "voice call", "language": "RU",
            "transcript": [{"timestamp": (start + dt.timedelta(seconds=s)).isoformat(timespec="microseconds")
                           .replace("+00:00", "0+00:00"), "speaker": role, "text": text}
                           for s, role, text in rows]}


@pytest.fixture
def record():
    return call([
        (0, "system", "Запись и транскрибирование запущены."),
        (1, "client", "Помогите проверить мой-перевод."),
        (13, "operator", "Сейчас я проверю перевод."),
        (15, "system", "Клиент находится на удержании."),
        (25, "system", "Запись и транскрибирование приостановлены."),
        (30, "system", "Запись и транскрибирование возобновлены."),
        (40, "system", "Клиент больше не находится на удержании."),
        (45, "client", "Да"),
        (45, "operator", "Спасибо за ожидание."),
        (50, "client", "Деньги уже пришли."),
        (60, "operator", "Всего доброго!"),
        (65, "system", "CRM: статус обращения изменён."),
        (70, "system", "Клиент завершил разговор."),
    ])


def test_synthetic_call(record):
    original = copy.deepcopy(record)
    m = metrics(record)
    assert record == original
    assert m["estimated"] is True and "не по звуку" in m["note"]
    assert m["duration_s"] == 70
    assert m["hold_s"] == 25
    assert m["holds"] == [{"start": record["transcript"][3]["timestamp"],
                            "end": record["transcript"][6]["timestamp"],
                            "start_n": 4, "end_n": 7, "duration_s": 25, "open": False}]
    assert m["recording_pause_s"] == 5
    assert [(p["start_n"], p["end_n"]) for p in m["recording_pauses"]] == [(5, 6)]
    assert m["speech"]["operator"] == {"seconds": 9, "share": 0.5422, "words_per_minute": 72.4}
    assert m["speech"]["client"] == {"seconds": 7.6, "share": 0.4578, "words_per_minute": 47.4}
    assert m["response_pauses"] == {"count": 3, "median_s": 10, "max_s": 12,
                                    "long": [{"client_n": 2, "operator_n": 3, "duration_s": 12}]}
    assert [e["n"] for e in m["unknown_events"]] == [12, 13]
    assert not m["warnings"]
    json.dumps(m, allow_nan=False)


def test_chat_and_non_iso(record):
    assert metrics({**record, "channel": "agent chat"}) is None
    record["transcript"][3]["timestamp"] = "00:15"
    assert metrics(record) is None
    record["transcript"][3]["timestamp"] = "2026-13-99T00:00:00Z"
    assert metrics(record) is None
    assert metrics({"channel": "voice call", "transcript": []}) is None


def test_unknown_event_is_returned(record):
    m = metrics(record)
    assert m["unknown_events"][0] == {"n": 12, "timestamp": record["transcript"][11]["timestamp"],
                                      "text": "CRM: статус обращения изменён."}
    assert m["hold_s"] == 25 and m["duration_s"] == 70


@pytest.mark.parametrize("start,end", [
    ("Клиент находится на удержании.", "Клиент больше не находится на удержании."),
    ("Клиент күту режимінде.", "Клиент енді күту режимінде емес."),
    ("Customer has been placed on hold.", "Customer is no longer on hold."),
    ("Client is on hold.", "Client was taken off hold."),
])
def test_hold_languages_and_unclosed(start, end):
    r = call([(0, "system", start), (2, "system", start), (9, "system", end),
              (10, "system", start), (14, "client", "Спасибо")])
    m = metrics(r)
    assert m["hold_s"] == 13
    assert [(h["start_n"], h["end_n"], h["open"]) for h in m["holds"]] == [(1, 3, False), (4, None, True)]
    assert m["holds"][1]["end"] == r["transcript"][-1]["timestamp"]
    assert not m["unknown_events"]


@pytest.mark.parametrize("pause,resume", [
    ("Жазу және транскрипциялау кідіртілді.", "Жазу және транскрипциялау жалғастырылды."),
    ("Recording and transcription paused.", "Recording and transcription resumed."),
])
def test_recording_pauses_are_separate(pause, resume):
    m = metrics(call([(0, "system", pause), (3, "system", resume), (5, "system", pause),
                      (10, "operator", "Жақсы")]))
    assert m["recording_pause_s"] == 8 and m["hold_s"] == 0
    assert m["recording_pauses"][-1]["open"] is True


def test_empty_speech_duplicate_times_and_system_between_turns():
    m = metrics(call([(0, "client", ""), (0, "operator", "Три коротких слова"),
                      (0, "client", "Да"), (0, "system", "CRM"), (0, "operator", "")]))
    assert m["duration_s"] == 0
    assert m["speech"]["operator"] == {"seconds": 0, "share": None, "words_per_minute": None}
    assert m["response_pauses"] == {"count": 1, "median_s": 0, "max_s": 0, "long": []}


def test_timezone_offsets_and_unordered_times():
    r = call([(0, "client", "Раз два три"), (10, "operator", "Раз два три")])
    r["transcript"][1]["timestamp"] = "2026-11-03T20:49:10.0000000+05:00"
    assert metrics(r)["duration_s"] == 10
    r["transcript"].reverse()
    m = metrics(r)
    assert m["duration_s"] == 0 and m["warnings"]


def test_api_call_metrics(record, tmp_path):
    app = create_app(tmp_path / "test.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    chat = {**record, "conversation_id": "metrics-chat", "channel": "agent chat"}
    app.state.db.save_dataset("TEST", "file", [record, chat])
    with TestClient(app) as c:
        assert c.get("/api/conversations/metrics-call").json()["call_metrics"] == metrics(record)
        assert c.get("/api/conversations/metrics-chat").json()["call_metrics"] is None


def test_dev_five_p4_calls():
    dev = ROOT / "data" / "dev.json"
    if not dev.resolve().is_relative_to(ROOT):
        pytest.skip("data/dev.json находится за пределами рабочей папки")
    if not dev.exists():
        pytest.skip("нет data/dev.json")
    records = sources.parse_bytes(dev.read_bytes())[2]
    calls = [r for r in records if r["channel"] == "voice call" and r["conversation_id"].startswith("p4")][:5]
    assert len(calls) == 5
    for r in calls:
        m = metrics(r)
        assert m is not None and m["duration_s"] >= 0
        json.dumps(m, allow_nan=False)
