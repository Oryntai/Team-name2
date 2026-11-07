"""Справочные метрики звонка по расшифровке; в оценку и промпт не входят."""
from __future__ import annotations

import datetime as dt
import re
import statistics

from .audio import parse_iso


# Шаблоны событий RU / KZ / EN: сначала завершение, затем начало удержания.
_EVENTS = {
    "hold_end": (
        r"клиент больше не находится на удержании",
        r"(?:client|customer) is no longer on hold|(?:client|customer) (?:was |has been )?taken off hold",
        r"клиент (?:енді )?(?:ұстап тұру|күту) режимінде емес|клиент (?:ұстап тұру|күту) режимінен (?:шығарылды|шықты)",
    ),
    "hold_start": (
        r"клиент находится на удержании",
        r"(?:client|customer) is on hold|(?:client|customer) (?:was |has been )?(?:placed|put) on hold",
        r"клиент (?:ұстап тұру|күту) режимінде|клиент (?:ұстап тұру|күту) режиміне (?:қойылды|ауыстырылды)",
    ),
    "recording_pause": (
        r"(?:запис|транскри).*приостановлен",
        r"(?:жазу|транскри).*(?:тоқтатыл|кідіртіл)",
        r"(?:record|transcri).*paus",
    ),
    "recording_resume": (
        r"(?:запис|транскри).*возобновлен",
        r"(?:жазу|транскри).*жалғас",
        r"(?:record|transcri).*resum",
    ),
    "recording_start": (
        r"(?:запис|транскри).*запущен",
        r"(?:жазу|транскри).*басталды",
        r"(?:record|transcri).*start",
    ),
}
_WORD = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
NOTE = "оценка по меткам времени расшифровки, не по звуку"


def _event(text: str) -> str | None:
    text = " ".join(text.casefold().replace("ё", "е").split())
    return next((kind for kind, patterns in _EVENTS.items()
                 if any(re.search(p, text) for p in patterns)), None)


def metrics(record: dict) -> dict | None:
    """Секунды, доли 0…1 и темп слов/мин; номера — позиции в исходном массиве с 1.

    Доли считаем среди речи клиента и оператора. Темп — по репликам от трёх слов
    с положительным оценочным временем. Последняя реплика имеет нулевое время.
    Пауза ответа — разница начал соседних реплик client → operator, включая речь клиента.
    """
    tr = record.get("transcript") or []
    if record.get("channel") != "voice call" or not tr:
        return None
    times = [parse_iso(t.get("timestamp")) for t in tr]
    if any(t is None for t in times):
        return None
    # ISO без часового пояса считаем UTC; исходные строки и порядок не меняем.
    warnings = []
    if any(t.tzinfo is None for t in times):
        warnings.append("Метки без часового пояса приняты за UTC.")
    times = [t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t for t in times]
    if any(b < a for a, b in zip(times, times[1:])):
        warnings.append("Метки идут не по времени: отрицательные интервалы приняты за 0 с.")

    def seconds(a, b):
        return max(0.0, (times[b] - times[a]).total_seconds())

    def interval(a, b, open_end=False):
        return {"start": tr[a]["timestamp"], "end": tr[b]["timestamp"],
                "start_n": a + 1, "end_n": None if open_end else b + 1,
                "duration_s": round(seconds(a, b), 3), "open": open_end}

    holds, recording_pauses, unknown = [], [], []
    opened = {"hold": None, "recording": None}
    groups = {"hold": holds, "recording": recording_pauses}
    speech = {role: {"seconds": 0.0, "words": 0, "pace_seconds": 0.0}
              for role in ("operator", "client")}
    pauses = []
    for i, t in enumerate(tr):
        role = t.get("speaker")
        if role == "system":
            kind = _event(t.get("text") or "")
            if kind is None:
                unknown.append({"n": i + 1, "timestamp": t["timestamp"], "text": t.get("text", "")})
            elif kind != "recording_start":
                group, action = kind.split("_", 1)
                if action in ("start", "pause") and opened[group] is None:
                    opened[group] = i
                elif action in ("end", "resume") and opened[group] is not None:
                    groups[group].append(interval(opened[group], i))
                    opened[group] = None
        if i + 1 == len(tr):
            continue
        gap = seconds(i, i + 1)
        if role in speech:
            words = len(_WORD.findall(t.get("text") or ""))
            estimated = min(gap, words * 0.6 + 2) if words else 0.0
            speech[role]["seconds"] += estimated
            if words >= 3 and estimated > 0:
                speech[role]["words"] += words
                speech[role]["pace_seconds"] += estimated
        if role == "client" and tr[i + 1].get("speaker") == "operator":
            pauses.append({"client_n": i + 1, "operator_n": i + 2, "duration_s": gap})

    for group, start in opened.items():
        if start is not None:
            groups[group].append(interval(start, len(tr) - 1, open_end=True))
    total_speech = sum(s["seconds"] for s in speech.values())
    speech = {role: {"seconds": round(s["seconds"], 3),
                     "share": round(s["seconds"] / total_speech, 4) if total_speech else None,
                     "words_per_minute": round(s["words"] * 60 / s["pace_seconds"], 1)
                     if s["pace_seconds"] else None} for role, s in speech.items()}
    gaps = [p["duration_s"] for p in pauses]
    return {"estimated": True, "note": NOTE, "duration_s": round(seconds(0, len(tr) - 1), 3),
            "hold_s": round(sum(h["duration_s"] for h in holds), 3), "holds": holds,
            "recording_pause_s": round(sum(p["duration_s"] for p in recording_pauses), 3),
            "recording_pauses": recording_pauses, "speech": speech,
            "response_pauses": {"count": len(gaps),
                                "median_s": round(statistics.median(gaps), 3) if gaps else None,
                                "max_s": round(max(gaps), 3) if gaps else None,
                                "long": [{**p, "duration_s": round(p["duration_s"], 3)}
                                         for p in pauses if p["duration_s"] > 10]},
            "unknown_events": unknown, "warnings": warnings}
