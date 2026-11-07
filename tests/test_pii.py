"""Маскировка личных данных: прячем идентификаторы, не трогаем имена, суммы, даты."""
import json
import pathlib

import pytest

from cqa.pii import find, iin_ok, luhn_ok, mask_text, mask_transcript

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_checksums():
    assert iin_ok("900101300126") and not iin_ok("900101300125")
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


@pytest.mark.parametrize("text,label", [
    ("мой ИИН 900101300126", "[ИИН скрыт]"),
    ("ЖСН: 851202450783", "[ИИН скрыт]"),
    ("карта 4111 1111 1111 1111", "[номер карты скрыт]"),
    ("card 4111111111111111", "[номер карты скрыт]"),
    ("тел. +7 701 123 45 67", "[телефон скрыт]"),
    ("звоните 8 (777) 123-45-67", "[телефон скрыт]"),
    ("mail me: a.b-c@mail.kz", "[e-mail скрыт]"),
    ("IBAN KZ86 125K ZT50 0410 0100", "[IBAN скрыт]"),
    ("паспорт N12345678", "[номер паспорта скрыт]"),
])
def test_masks_identifiers(text, label):
    masked, spans = mask_text(text)
    assert label in masked and len(spans) == 1


@pytest.mark.parametrize("text", [
    "сумма 1 000 000 тенге", "8 000 000", "дата 12.03.2025 в 14:30", "меня зовут Айгерим, я оператор",
    "[Имя скрыто] [Данные идентификации скрыты]", "ИИН 900101300125 (неверный контрольный разряд)",
    "счёт 123456789012", "тикер AAPL, 150 акций по 189.50", "1042-S за 2025 год",
])
def test_keeps_non_identifiers(text):
    assert mask_text(text) == (text, [])


def test_spans_point_to_original_text():
    text = "Карта 4111 1111 1111 1111 и ИИН 900101300126."
    spans = find(text)
    assert [text[s["start"]:s["end"]] for s in spans] == ["4111 1111 1111 1111", "900101300126"]


def test_transcript_masking_numbers_replies_from_one():
    tr = [{"timestamp": "00:01", "speaker": "client", "text": "Здравствуйте"},
          {"timestamp": "00:02", "speaker": "client", "text": "ИИН 900101300126"}]
    masked, found = mask_transcript(tr)
    assert masked[1]["text"] == "ИИН [ИИН скрыт]" and masked[1]["timestamp"] == "00:02"
    assert found == [{"n": 2, "type": "iin", "label": "[ИИН скрыт]"}]
    assert tr[1]["text"] == "ИИН 900101300126"  # оригинал не тронут


def test_dev_has_nothing_to_mask():
    """Стоп-условие карточки: на DEV маскировка не меняет ни одной реплики, значит не меняет и решений модели."""
    dev = ROOT / "data" / "dev.json"
    if not dev.exists():
        pytest.skip("нет data/dev.json")
    recs = json.loads(dev.read_text())["data"]["records"]
    assert sum(len(find(t["text"])) for r in recs for t in r["transcript"]) == 0
