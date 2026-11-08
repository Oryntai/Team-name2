"""Точная секунда реплики в записи: подстановка из audio_align.json, откат на оценку по окнам записи."""
from __future__ import annotations

import json

from cqa import audio


def iso(sec: int) -> str:
    m, s = divmod(sec, 60)
    return f"2026-11-03T10:{m:02d}:{s:02d}.1234567+00:00"


CALL = [
    {"timestamp": iso(0), "speaker": "client", "text": "Алло"},
    {"timestamp": iso(5), "speaker": "system", "text": "Запись и транскрибирование запущены."},
    {"timestamp": iso(30), "speaker": "operator", "text": "Здравствуйте"},
    {"timestamp": iso(65), "speaker": "system", "text": "Запись и транскрибирование приостановлены."},
    {"timestamp": iso(70), "speaker": "operator", "text": "реплика на паузе"},
    {"timestamp": iso(80), "speaker": "system", "text": "Запись и транскрибирование возобновлены."},
    {"timestamp": iso(100), "speaker": "client", "text": "Спасибо"},
]
EST = [0.0, 0.0, 25.0, 60.0, 60.0, 60.0, 80.0]
CHAT = [{"timestamp": "00:00", "speaker": "client", "text": "Привет"},
        {"timestamp": "00:05", "speaker": "operator", "text": "Здравствуйте"}]


def test_estimate_unchanged():
    assert audio.estimate_offsets(CALL) == EST
    assert audio.audio_offsets(CALL) == EST  # без id звонка — прежняя оценка


def test_exact_second_replaces_estimate():
    exact = {"c1": [0.4, None, 19.37, None, 52.0, None, 71.06]}
    assert audio.audio_offsets(CALL, "c1", exact) == [0.4, 0.0, 19.4, 60.0, 52.0, 60.0, 71.1]


def test_null_falls_back_to_estimate():
    exact = {"c1": [None] * len(CALL)}
    assert audio.audio_offsets(CALL, "c1", exact) == EST


def test_unknown_conversation_falls_back():
    assert audio.audio_offsets(CALL, "other", {"c1": [1.0] * len(CALL)}) == EST


def test_length_mismatch_ignored():
    """Транскрипт сменился (другое число реплик) — точные секунды не подходят, берём оценку."""
    assert audio.audio_offsets(CALL, "c1", {"c1": [1.0] * (len(CALL) - 1)}) == EST
    assert audio.audio_offsets(CALL, "c1", {"c1": [1.0] * (len(CALL) + 1)}) == EST


def test_bad_values_ignored():
    exact = {"c1": ["5", -1, True, 3.0, None, float("nan"), 7]}
    assert audio.audio_offsets(CALL, "c1", exact) == [0.0, 0.0, 25.0, 3.0, 60.0, 60.0, 7.0]


def test_chat_stays_none():
    assert audio.audio_offsets(CHAT, "chat", {"chat": [1.0, 2.0]}) == [None, None]


def test_load_exact_from_file(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"c1": [None, None, 20.0, None, None, None, 70.0], "junk": 5}))
    data = audio.load_exact(p)
    assert set(data) == {"c1"}
    assert audio.audio_offsets(CALL, "c1", data)[2] == 20.0
    assert audio.load_exact(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("{")
    assert audio.load_exact(tmp_path / "bad.json") == {}


def test_shipped_file_matches_dev_shape():
    """Файл в репозитории: только числа/null, по одному значению на реплику, время не идёт назад."""
    data = audio.load_exact()
    assert data, "cqa/audio_align.json должен быть в репозитории"
    for cid, secs in data.items():
        assert cid.startswith("p4-")
        assert all(x is None or isinstance(x, (int, float)) and x >= 0 for x in secs)
        nums = [x for x in secs if x is not None]
        assert nums == sorted(nums), cid
