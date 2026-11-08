"""Записи звонков: время реплик, смещение реплики в записи, свежая ссылка на mp3.

Секунда в mp3 = сумма окон записи до момента реплики. Окна берём из системных событий звонка:
«Запись и транскрибирование запущены» → «…приостановлены», «…возобновлены» → конец разговора.
Реплики до старта записи → 0; реплика во время паузы → место, где запись продолжилась.
Длина mp3 по кадрам на DEV больше суммы окон на 1–7 с, а метка реплики в CRM ближе к концу фразы.
Поэтому для звонков DEV есть точные секунды: `audio_align.json` рядом (whisper + сопоставление текста,
пересборка — `lab/audio_align.py`). Где точной секунды нет — прежняя оценка по окнам записи.
"""
from __future__ import annotations

import datetime as dt
import functools
import html
import json
import pathlib
import re
import time

import httpx

# слова системных событий о записи (RU / KZ / EN)
_REC = ("запис", "жазу", "record", "транскри")
_PAUSE = ("приостановлен", "тоқтатыл", "кідіртіл", "paused", "pause")
_RESUME = ("возобновлен", "жалғас", "resumed", "resume")
_START = ("запущен", "басталды", "started", "start")


def parse_iso(ts) -> dt.datetime | None:
    if not isinstance(ts, str) or "T" not in ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m and m.group(2) and len(m.group(2)) > 7:  # дробь длиннее 6 знаков старые Python не читают
        s = m.group(1) + m.group(2)[:7] + m.group(3)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def clock_seconds(ts) -> float | None:
    """Метка чата «MM:SS» или «HH:MM:SS» → секунды от начала чата."""
    if not isinstance(ts, str):
        return None
    parts = ts.strip().split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


def fmt_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def duration(transcript: list[dict]) -> str | None:
    """Длительность диалога по меткам первой и последней реплики (метки не меняем)."""
    if not transcript:
        return None
    a, b = transcript[0].get("timestamp"), transcript[-1].get("timestamp")
    ia, ib = parse_iso(a), parse_iso(b)
    if ia and ib:
        return fmt_duration((ib - ia).total_seconds())
    ca, cb = clock_seconds(a), clock_seconds(b)
    if ca is not None and cb is not None:
        return fmt_duration(cb - ca)
    return None


def _event_kind(text: str) -> str | None:
    t = (text or "").lower()
    if not any(w in t for w in _REC):
        return None
    if any(w in t for w in _PAUSE):
        return "pause"
    if any(w in t for w in _RESUME):
        return "resume"
    if any(w in t for w in _START):
        return "start"
    return None


def recording_windows(transcript: list[dict]) -> list[tuple[dt.datetime, dt.datetime]]:
    """Окна, когда шла запись. Пустой список — метки не ISO (чат) или реплик нет."""
    times = [parse_iso(t.get("timestamp")) for t in transcript]
    if not times or any(x is None for x in times):
        return []
    end = max(times)
    events = [(times[i], _event_kind(t.get("text", ""))) for i, t in enumerate(transcript)
              if t.get("speaker") == "system"]
    events = [(ts, k) for ts, k in events if k]
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    if not events:
        return [(times[0], end)]
    open_at = None if events[0][1] == "start" else times[0]  # пауза без старта → запись шла с начала
    for ts, kind in events:
        if kind in ("start", "resume"):
            if open_at is None:
                open_at = ts
        elif kind == "pause" and open_at is not None:
            if ts > open_at:
                windows.append((open_at, ts))
            open_at = None
    if open_at is not None and end > open_at:
        windows.append((open_at, end))
    return windows


def estimate_offsets(transcript: list[dict]) -> list[float | None]:
    """Оценка секунды в записи по окнам записи (для каждой реплики звонка; у чатов — None)."""
    windows = recording_windows(transcript)
    if not windows:
        return [None] * len(transcript)
    out: list[float | None] = []
    for t in transcript:
        ts = parse_iso(t.get("timestamp"))
        s = 0.0
        for ws, we in windows:
            if ts <= ws:
                break
            s += (min(ts, we) - ws).total_seconds()
        out.append(round(s, 1))
    return out


ALIGN_PATH = pathlib.Path(__file__).with_name("audio_align.json")


@functools.lru_cache(maxsize=4)
def load_exact(path: pathlib.Path = ALIGN_PATH) -> dict[str, list]:
    """Точные секунды реплик {conversation_id: [секунда или None по индексу реплики]}; нет файла — пусто."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, list)} if isinstance(d, dict) else {}


def audio_offsets(transcript: list[dict], cid: str | None = None,
                  exact: dict[str, list] | None = None) -> list[float | None]:
    """Секунда в записи для каждой реплики звонка; у чатов — None.

    Точная секунда (whisper) — если она есть для этого звонка и число реплик совпадает; иначе оценка по окнам.
    """
    est = estimate_offsets(transcript)
    if cid is None:
        return est
    ex = (load_exact() if exact is None else exact).get(cid)
    if not isinstance(ex, list) or len(ex) != len(transcript):
        return est
    return [round(float(x), 1) if e is not None and isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0
            else e for x, e in zip(ex, est)]


def recorded_seconds(transcript: list[dict]) -> float:
    return sum((we - ws).total_seconds() for ws, we in recording_windows(transcript))


# --- свежая подписанная ссылка на mp3 (каждый запрос к audio_url даёт новую, живёт ~1 ч) ---
_SRC_RE = re.compile(r"<(?:source|audio)\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_cache: dict[str, tuple[float, str]] = {}
TTL = 40 * 60  # ссылка живёт около часа — берём с запасом


class AudioError(Exception):
    pass


def extract_src(page: str) -> str | None:
    m = _SRC_RE.search(page or "")
    return html.unescape(m.group(1)) if m else None


async def resolve_mp3(audio_url: str, client: httpx.AsyncClient) -> str:
    """Адрес mp3 из HTML-плеера организаторов (кешируем саму ссылку на ~40 мин, не оценки)."""
    now = time.monotonic()
    hit = _cache.get(audio_url)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = await client.get(audio_url, timeout=60, follow_redirects=True)
    except httpx.HTTPError as e:
        raise AudioError(f"Плеер записи недоступен: {type(e).__name__}") from e
    if r.status_code != 200:
        raise AudioError(f"Плеер записи ответил {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("audio/"):
        src = str(r.url)
    else:
        src = extract_src(r.text)
    if not src:
        raise AudioError("В странице плеера не нашлась ссылка на запись")
    _cache[audio_url] = (now + TTL, src)
    return src


def forget(audio_url: str) -> None:
    _cache.pop(audio_url, None)
