"""Маскировка личных данных перед отправкой в модель.

Находим только структурированные идентификаторы и проверяем их там, где есть контрольная сумма:
ИИН/БИН (12 цифр, контрольный разряд), номер карты (13–19 цифр, алгоритм Луна), IBAN (mod 97),
телефон (+7 / 8 и международный формат), e-mail, номер паспорта РК (N + 8 цифр).
Имена, суммы, даты и тикеры НЕ трогаем: имя оператора нужно для критерия «представился», суммы — для консультации.
Метка сохраняет тип («[ИИН скрыт]»), чтобы модель видела, ЧТО именно назвали или раскрыли.
Позиции (start, end) — в исходном тексте: интерфейс подсвечивает по ним оригинал.
"""
from __future__ import annotations

import re

LABELS = {
    "iin": "[ИИН скрыт]",
    "card": "[номер карты скрыт]",
    "iban": "[IBAN скрыт]",
    "phone": "[телефон скрыт]",
    "email": "[e-mail скрыт]",
    "passport": "[номер паспорта скрыт]",
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
# карта: подряд 13–19 цифр или группами по 4 (суммы пишут группами по 3 — их не трогаем)
_CARD = re.compile(r"(?<![\d+])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?)(?![\d])")
_IIN = re.compile(r"(?<![\d+])\d{12}(?!\d)")
_PHONE = re.compile(r"(?<![\w+])(?:\+7|8)[ \-(]*\d{3}[ \-)]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)"
                    r"|(?<![\w+])\+(?:[1-689]\d{0,2})[ \-(]*\d{2,4}[ \-)]*\d{2,4}[ \-]*\d{2,4}(?:[ \-]*\d{2,4})?(?!\d)")
_PASSPORT = re.compile(r"(?<![\w])[NН]\s?\d{8}(?!\d)")  # латинская N или кириллическая Н


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def iin_ok(s: str) -> bool:
    """Контрольный разряд ИИН/БИН Казахстана."""
    if len(s) != 12 or not s.isdigit():
        return False
    d = [int(c) for c in s]
    k = sum(d[i] * (i + 1) for i in range(11)) % 11
    if k == 10:
        w = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]
        k = sum(d[i] * w[i] for i in range(11)) % 11
        if k == 10:
            return False
    return k == d[11]


def iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    num = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(num) % 97 == 1


def find(text: str) -> list[dict]:
    """Найденные идентификаторы: [{"start", "end", "type", "label"}], без пересечений, по порядку."""
    cands: list[tuple[int, int, str]] = []
    for m in _EMAIL.finditer(text):
        cands.append((m.start(), m.end(), "email"))
    for m in _IBAN.finditer(text):
        if iban_ok(m.group()):
            cands.append((m.start(), m.end(), "iban"))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            cands.append((m.start(), m.end(), "card"))
    for m in _IIN.finditer(text):
        if iin_ok(m.group()):
            cands.append((m.start(), m.end(), "iin"))
    for m in _PHONE.finditer(text):
        if 10 <= len(re.sub(r"\D", "", m.group())) <= 15:
            cands.append((m.start(), m.end(), "phone"))
    for m in _PASSPORT.finditer(text):
        cands.append((m.start(), m.end(), "passport"))
    # пересечения: раньше начало, затем длиннее
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    out, last_end = [], -1
    for s, e, t in cands:
        if s >= last_end:
            out.append({"start": s, "end": e, "type": t, "label": LABELS[t]})
            last_end = e
    return out


def mask_text(text: str) -> tuple[str, list[dict]]:
    spans = find(text or "")
    if not spans:
        return text, []
    parts, pos = [], 0
    for sp in spans:
        parts += [text[pos:sp["start"]], sp["label"]]
        pos = sp["end"]
    parts.append(text[pos:])
    return "".join(parts), spans


def mask_transcript(transcript: list[dict]) -> tuple[list[dict], list[dict]]:
    """Копия транскрипта со скрытыми данными + список скрытого: [{"n", "type", "label"}] (n — номер реплики с 1)."""
    masked, found = [], []
    for n, t in enumerate(transcript, 1):
        txt, spans = mask_text(t.get("text", ""))
        masked.append({**t, "text": txt})
        found += [{"n": n, "type": sp["type"], "label": sp["label"]} for sp in spans]
    return masked, found
