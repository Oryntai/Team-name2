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
        if not isinstance(t.get("speaker"), str) or not t["speaker"].strip():
            raise SourceError(f"{where}, реплика {j}: не указано, кто говорит (speaker).")
        # Без текста или метки времени реплика остаётся на своём месте (номера реплик — по исходному массиву):
        # диалог можно оценить, а не терять целиком.
        text, ts = t.get("text"), t.get("timestamp")
        out_tr.append({"timestamp": "" if ts is None else ts, "speaker": t["speaker"],
                       "text": "" if text is None else text if isinstance(text, str) else str(text)})
    audio_url = r.get("audio_url")
    channel, language = r.get("channel"), r.get("language")
    return {"conversation_id": cid, "channel": channel if isinstance(channel, str) else "",
            "language": language if isinstance(language, str) else "",
            "transcript": out_tr, "audio_url": audio_url if isinstance(audio_url, str) and audio_url.strip() else None}


def parse_payload(obj, *, tolerate_records: bool = False) -> tuple[str | None, int | None, list[dict]]:
    """Формат API организаторов, {"records": [...]}, список записей или одна запись → (group_id, total, records).

    tolerate_records (выгрузка результатов): запись, которую нельзя прочитать, не отменяет весь набор — она приходит
    с полем source_error и попадает в results.json со status error и причиной (остальные оцениваются).
    """
    group_id, total = None, None
    if isinstance(obj, dict) and ("isSuccess" in obj or "data" in obj):
        flag = obj.get("isSuccess")
        if flag is False or (isinstance(flag, (int, str)) and not isinstance(flag, bool)
                             and str(flag).strip().lower() in ("0", "false", "no")):
            reason = obj.get("error") or obj.get("message") or "без текста"
            raise SourceError(f"API организаторов вернуло ошибку: {str(reason)[:300]}")
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
    records = []
    for i, r in enumerate(obj, 1):
        try:
            records.append(_record(r, i))
        except SourceError as e:
            cid = r.get("conversation_id") if isinstance(r, dict) else None
            if not tolerate_records:
                raise
            if isinstance(cid, int) and not isinstance(cid, bool):
                cid = str(cid)
            if not isinstance(cid, str) or not cid.strip():
                # Без id запись в results.json не сопоставить; остальные всё равно оцениваем — лучше, чем ничего.
                print(f"ВНИМАНИЕ: {e} Запись пропущена — в файле будет меньше записей, чем в наборе.")
                continue
            records.append({"conversation_id": cid.strip(), "channel": "", "language": "", "transcript": [],
                            "audio_url": None, "source_error": str(e)})
    if not records:
        raise SourceError("В наборе нет ни одного диалога с conversation_id.")
    seen: set[str] = set()
    for r in records:
        if r["conversation_id"] in seen:
            raise SourceError(f"Диалог {r['conversation_id']} встречается в наборе дважды.")
        seen.add(r["conversation_id"])
    return (str(group_id) if group_id not in (None, "") else None,
            int(total) if isinstance(total, int) else len(records), records)


def parse_bytes(data: bytes, *, tolerate_records: bool = False) -> tuple[str | None, int | None, list[dict]]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceError(f"Файл не читается как JSON (UTF-8): {e}") from e
    return parse_payload(obj, tolerate_records=tolerate_records)


async def fetch(url: str, group_id: str, timeout: float = 120, *,
                tolerate_records: bool = False) -> tuple[str | None, int | None, list[dict]]:
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
    gid, total, records = parse_payload(obj, tolerate_records=tolerate_records)
    return gid or group_id, total, records
