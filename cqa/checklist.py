"""Чек-лист: формат, проверка, допустимые ответы.

Формат — в docs/ARCHITECTURE.md. Типы критериев: bool (Да/Нет), scale (ступени, верхняя = полный вес),
flag (грубая ошибка: «Да» = нарушение, без баллов). Ответы везде строки: "Да", "Нет", "N/A", "15".
"""
from __future__ import annotations

import copy
import json
import pathlib
import re

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "checklists" / "default.json"
TYPES = ("bool", "scale", "flag")
TYPE_LABELS = {"bool": "Да / Нет", "scale": "шкала", "flag": "грубая ошибка (без баллов)"}
YES, NO, NA = "Да", "Нет", "N/A"
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")


def load_default() -> dict:
    return normalize(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))


def fmt_value(v) -> str:
    """Ступень шкалы строкой: 20 → "20", 7.5 → "7.5"."""
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def steps_desc(c: dict) -> list[dict]:
    return sorted(c.get("steps") or [], key=lambda s: -float(s["value"]))


def allowed_answers(c: dict) -> list[str]:
    vals = [fmt_value(s["value"]) for s in steps_desc(c)] if c["type"] == "scale" else [YES, NO]
    if c.get("na"):
        vals.append(NA)
    return vals


def sections(config: dict) -> list[str]:
    out: list[str] = []
    for c in config["criteria"]:
        s = (c.get("section") or "").strip() or "Без секции"
        if s not in out:
            out.append(s)
    return out


def kind_label(c: dict) -> str:
    if c["type"] == "scale":
        s = "шкала " + " / ".join(fmt_value(s["value"]) for s in sorted(c["steps"], key=lambda s: float(s["value"])))
    elif c["type"] == "flag":
        s = "Да / Нет"
    else:
        s = "Да / Нет"
    return s + (" / N/A" if c.get("na") else "")


def normalize(config: dict) -> dict:
    """Копия с заполненными необязательными полями (не меняет смысл)."""
    cfg = copy.deepcopy(config)
    cfg.setdefault("name", "Чек-лист")
    cfg.setdefault("description", "")
    cfg.setdefault("rounding", 2)
    cfg.setdefault("rules", [])
    for c in cfg.get("criteria") or []:
        if not isinstance(c, dict):
            continue
        c["id"] = str(c.get("id", "")).strip()
        for k in ("section", "title", "yes", "no", "na_when", "guidance"):
            c[k] = (c.get(k) or "").strip() if isinstance(c.get(k) or "", str) else c.get(k)
        c.setdefault("type", "bool")
        c.setdefault("na", False)
        c.setdefault("steps", [])
        if c["type"] == "flag":
            c.setdefault("weight", 0)
    return cfg


def validate(config: dict) -> list[str]:
    """Список ошибок по-русски; пустой — чек-лист корректен."""
    errs: list[str] = []
    if not isinstance(config, dict):
        return ["Чек-лист должен быть объектом JSON."]
    if not str(config.get("name") or "").strip():
        errs.append("Укажите название чек-листа.")
    r = config.get("rounding", 2)
    if not isinstance(r, int) or isinstance(r, bool) or not 0 <= r <= 4:
        errs.append("Округление итога — целое число знаков от 0 до 4.")
    rules = config.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(x, str) for x in rules):
        errs.append("Общие правила — список строк.")
    crit = config.get("criteria")
    if not isinstance(crit, list) or not crit:
        return errs + ["Добавьте хотя бы один критерий."]
    seen: set[str] = set()
    weighted = 0
    for i, c in enumerate(crit, 1):
        if not isinstance(c, dict):
            errs.append(f"Критерий №{i}: должен быть объектом.")
            continue
        cid = str(c.get("id", "")).strip()
        name = f"Критерий {cid or '№' + str(i)}"
        if not _ID_RE.match(cid):
            errs.append(f"{name}: номер — 1–16 символов: буквы, цифры, «-», «_», «.».")
        elif cid in seen:
            errs.append(f"{name}: номер повторяется.")
        seen.add(cid)
        if not str(c.get("title") or "").strip():
            errs.append(f"{name}: укажите название.")
        t = c.get("type")
        if t not in TYPES:
            errs.append(f"{name}: тип — один из: Да/Нет (bool), шкала (scale), грубая ошибка (flag).")
            continue
        w = c.get("weight", 0)
        if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
            errs.append(f"{name}: вес — неотрицательное число.")
            w = 0
        if t == "flag":
            if w:
                errs.append(f"{name}: грубая ошибка не даёт баллов — вес должен быть 0.")
            if c.get("na"):
                errs.append(f"{name}: у грубой ошибки нет N/A (ответ «Нет» = нарушений не найдено).")
        elif w <= 0:
            errs.append(f"{name}: вес критерия с баллами должен быть больше 0.")
        else:
            weighted += 1
        if not str(c.get("yes") or "").strip():
            errs.append(f"{name}: опишите, когда ставится «Да»" + (" (или как выставлять шкалу)." if t == "scale" else "."))
        if c.get("na") and not str(c.get("na_when") or "").strip():
            errs.append(f"{name}: N/A разрешён — укажите условие, когда он ставится.")
        if t == "scale":
            steps = c.get("steps")
            if not isinstance(steps, list) or len(steps) < 2:
                errs.append(f"{name}: у шкалы должно быть не меньше двух ступеней.")
                continue
            vals = []
            for s in steps:
                v = s.get("value") if isinstance(s, dict) else None
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    errs.append(f"{name}: значение ступени — неотрицательное число.")
                    continue
                if not str(s.get("text") or "").strip():
                    errs.append(f"{name}: опишите ступень {fmt_value(v)}.")
                vals.append(float(v))
            if len(set(vals)) != len(vals):
                errs.append(f"{name}: значения ступеней повторяются.")
            if vals and max(vals) <= 0:
                errs.append(f"{name}: верхняя ступень шкалы должна быть больше 0 (она даёт полный вес).")
    if not weighted:
        errs.append("Нет ни одного критерия с баллами — итог не посчитать.")
    return errs
