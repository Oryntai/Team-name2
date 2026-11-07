"""Импорт диалогов: API организаторов (GET с group_id) и JSON-файл.

Метки времени и порядок реплик сохраняем как в исходном массиве: ничего не сортируем и не переписываем
(в DEV бывают подряд одинаковые метки). Подпись ссылки API (`sig`) никуда не выводим — только маской.
"""
from __future__ import annotations

import json
import urllib.parse

import httpx

ROLES = ("client", "operator", "bot", "system")
_SECRET_PARAMS = {"sig", "signature", "code", "key", "token", "sv", "sp"}


class SourceError(Exception):
    """Понятная пользователю ошибка импорта (по-русски)."""


def mask_url(url: str | None) -> str | None:
    """Ссылка без секретов: хост, начало пути и имена параметров; значения подписи — ***."""
    if not url:
        return None
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "***"
    q = [(k, "***" if k.lower() in _SECRET_PARAMS else v)
         for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
    path = p.path if len(p.path) <= 24 else p.path[:20] + "…"
    host = p.netloc if len(p.netloc) <= 28 else p.netloc[:14] + "…" + p.netloc[-12:]
    return urllib.parse.urlunsplit((p.scheme, host, path, urllib.parse.urlencode(q, safe="*/"), ""))


def with_group_id(url: str, group_id: str) -> str:
    """Подставляет group_id, не трогая остальные параметры побайтно (подпись считается по ним)."""
    p = urllib.parse.urlsplit(url)
    parts = [x for x in p.query.split("&") if x and not x.startswith("group_id=") and x != "group_id"]
    parts.append("group_id=" + urllib.parse.quote(str(group_id).strip(), safe=""))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "&".join(parts), p.fragment))


def _record(r, i: int) -> dict:
    where = f"Запись №{i}"
    if not isinstance(r, dict):
        raise SourceError(f"{where}: ожидался объект диалога.")
    cid = r.get("conversation_id")
    if not isinstance(cid, (str, int)) or not str(cid).strip():
        raise SourceError(f"{where}: нет conversation_id.")
    cid = str(cid).strip()
    where = f"Диалог {cid}"
    tr = r.get("transcript")
    if isinstance(tr, str):  # на случай, если транскрипт пришёл строкой JSON
        try:
            tr = json.loads(tr)
        except json.JSONDecodeError as e:
            raise SourceError(f"{where}: transcript не читается как JSON.") from e
    if not isinstance(tr, list) or not tr:
        raise SourceError(f"{where}: transcript должен быть непустым массивом реплик.")
    out_tr = []
    for j, t in enumerate(tr, 1):
        if not isinstance(t, dict):
            raise SourceError(f"{where}, реплика {j}: ожидался объект.")
        missing = [k for k in ("timestamp", "speaker", "text") if k not in t]
        if missing:
            raise SourceError(f"{where}, реплика {j}: нет полей {', '.join(missing)}.")
        out_tr.append({"timestamp": t["timestamp"], "speaker": t["speaker"],
                       "text": t["text"] if isinstance(t["text"], str) else str(t["text"])})
    audio_url = r.get("audio_url")
    return {"conversation_id": cid, "channel": r.get("channel") or "", "language": r.get("language") or "",
            "transcript": out_tr, "audio_url": audio_url if isinstance(audio_url, str) and audio_url.strip() else None}


def parse_payload(obj) -> tuple[str | None, int | None, list[dict]]:
    """Формат API организаторов, {"records": [...]}, список записей или одна запись → (group_id, total, records)."""
    group_id, total = None, None
    if isinstance(obj, dict) and ("isSuccess" in obj or "data" in obj):
        if obj.get("isSuccess") is False:
            raise SourceError(f"API организаторов вернуло ошибку: {str(obj.get('error') or 'без текста')[:300]}")
        obj = obj.get("data")
        if not isinstance(obj, dict):
            raise SourceError("В ответе нет поля data.")
    if isinstance(obj, dict) and "records" in obj:
        group_id = obj.get("dataset_group_id")
        total = obj.get("total_records")
        obj = obj["records"]
    if isinstance(obj, dict) and "conversation_id" in obj:
        obj = [obj]
    if not isinstance(obj, list):
        raise SourceError("Не похоже на набор диалогов: нужен формат API организаторов, список записей или одна запись.")
    if not obj:
        raise SourceError("В наборе нет ни одного диалога.")
    records = [_record(r, i) for i, r in enumerate(obj, 1)]
    seen: set[str] = set()
    for r in records:
        if r["conversation_id"] in seen:
            raise SourceError(f"Диалог {r['conversation_id']} встречается в наборе дважды.")
        seen.add(r["conversation_id"])
    return (str(group_id) if group_id not in (None, "") else None,
            int(total) if isinstance(total, int) else len(records), records)


def parse_bytes(data: bytes) -> tuple[str | None, int | None, list[dict]]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceError(f"Файл не читается как JSON (UTF-8): {e}") from e
    return parse_payload(obj)


async def fetch(url: str, group_id: str, timeout: float = 120) -> tuple[str | None, int | None, list[dict]]:
    """GET к API организаторов. Ошибки — без адреса с подписью."""
    if not url or not url.startswith(("http://", "https://")):
        raise SourceError("Не задан адрес API организаторов: вставьте ссылку GET из задания в настройках.")
    full = with_group_id(url, group_id)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(full)
    except httpx.TimeoutException as e:
        raise SourceError("API организаторов не ответило вовремя — попробуйте ещё раз.") from e
    except httpx.HTTPError as e:
        raise SourceError(f"API организаторов недоступно ({type(e).__name__}).") from e
    if r.status_code != 200:
        try:
            msg = (r.json() or {}).get("error")
        except ValueError:
            msg = None
        raise SourceError(f"API организаторов: {msg}" if msg else f"API организаторов ответило {r.status_code}.")
    try:
        obj = r.json()
    except ValueError as e:
        raise SourceError("API организаторов вернуло не JSON.") from e
    gid, total, records = parse_payload(obj)
    return gid or group_id, total, records
