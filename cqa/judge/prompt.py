"""Промпт и JSON-схема судьи — строятся из чек-листа, ничего не зашито под конкретные критерии.

Смена критерия в интерфейсе = другой текст промпта и другая схема при следующей оценке.
Реплики нумеруются с 1 в порядке массива; метки времени передаются как есть.
"""
from __future__ import annotations

from ..checklist import NA, allowed_answers, fmt_value, steps_desc

PROMPT_VERSION = "p1"

ROLE = ("Ты — старший специалист отдела контроля качества контакт-центра брокерской компании: каждый день оцениваешь "
        "работу операторов по внутреннему чек-листу. Ты беспристрастен: судишь только по тому, что написано в транскрипте "
        "и в чек-листе, ничего не додумываешь, не смягчаешь и не ужесточаешь сверх формулировок.")

SPEAKERS = "client — клиент, operator — оператор, bot — бот, system — системное событие"


def _criterion_block(c: dict) -> str:
    kind = " / ".join(allowed_answers(c)) if c["type"] != "flag" else "Да / Нет"
    weight = "без баллов, отмечается отдельно" if c["type"] == "flag" else f"вес {fmt_value(c['weight'])}"
    sec = f" (секция: {c['section']})" if c.get("section") else ""
    lines = [f"## {c['id']}. {c['title']} — {kind}; {weight}{sec}"]
    if c["type"] == "scale":
        lines.append(c["yes"][:1].upper() + c["yes"][1:])
        lines += [f"- {fmt_value(s['value'])}: {s['text']}" for s in steps_desc(c)]
    elif c["type"] == "flag":
        lines.append(f"Да (нарушение выявлено) — {c['yes']}")
        lines.append(f"Нет — {c['no'] or 'ни одного такого нарушения не выявлено.'}")
    else:
        lines.append(f"Да — {c['yes']}")
        if c.get("no"):
            lines.append(f"Нет — {c['no']}")
    if c.get("na"):
        lines.append(f"N/A — {c['na_when']}")
    if c.get("guidance"):
        lines.append(f"Пояснение: {c['guidance']}")
    return "\n".join(lines)


def build_system(config: dict) -> str:
    parts = [ROLE, "",
             "ЗАДАЧА: оценить работу ОПЕРАТОРА в одном диалоге по каждому критерию чек-листа.", "",
             f"# Чек-лист «{config.get('name', 'Чек-лист')}»",
             "N/A разрешён только у критериев, где он указан, и только при выполненном условии.", ""]
    parts += [_criterion_block(c) + "\n" for c in config["criteria"]]
    rules = [r for r in config.get("rules") or [] if r.strip()]
    if rules:
        parts += ["# Общие правила"] + [f"- {r}" for r in rules] + [""]
    parts += [
        "# Как отвечать",
        f"- Реплики пронумерованы [n]; роли: {SPEAKERS}. Бот и системные события — контекст.",
        "- По каждому критерию заполни поля в таком порядке:",
        "  explanation — 1–2 предложения по-русски (даже если диалог на казахском или английском): что сделал или не "
        "сделал оператор и почему такой ответ; ссылайся на реплики в виде [n]; при N/A — какое условие неприменимости выполнено;",
        "  answer — ровно один из допустимых ответов этого критерия;",
        "  evidence — номера реплик, на которых основан вывод; если действие пропущено — номера участка, где оно ожидалось; "
        "при N/A — реплики, показывающие, что условие выполнено.",
        "- Оцени все критерии, ни одного не пропускай.",
    ]
    return "\n".join(parts)


def build_user(record: dict, transcript: list[dict] | None = None) -> str:
    tr = record["transcript"] if transcript is None else transcript
    head = (f"Диалог {record.get('conversation_id', '')} · канал: {record.get('channel', '')} · "
            f"язык: {record.get('language', '')} · реплик: {len(tr)}")
    body = "\n".join(f"[{i}] {t.get('timestamp', '')} {t.get('speaker', '')}: {t.get('text', '')}"
                     for i, t in enumerate(tr, 1))
    return head + "\n\n" + body


def build_schema(config: dict) -> dict:
    """Строгая схема: объект на каждый критерий, у ответа — enum из допустимых значений именно этого критерия."""
    props = {}
    for c in config["criteria"]:
        props[c["id"]] = {
            "type": "object", "additionalProperties": False,
            "required": ["explanation", "answer", "evidence"],
            "properties": {
                "explanation": {"type": "string"},
                "answer": {"type": "string", "enum": allowed_answers(c)},
                "evidence": {"type": "array", "items": {"type": "integer"}},
            },
        }
    return {"type": "object", "additionalProperties": False, "required": ["criteria"],
            "properties": {"criteria": {"type": "object", "additionalProperties": False,
                                        "required": [c["id"] for c in config["criteria"]], "properties": props}}}


def normalize_answer(c: dict, a) -> str | None:
    """Для моделей без строгой схемы (локальные): приводит «да», «yes», 15 к виду чек-листа; None — недопустимо."""
    allowed = allowed_answers(c)
    if a is None:
        return None
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        s = fmt_value(a)
        return s if s in allowed else None
    s = str(a).strip()
    if s in allowed:
        return s
    low = s.lower().replace(" ", "").rstrip(".")
    if low in ("n/a", "na", "н/д", "неприменимо", "notapplicable"):
        return NA if NA in allowed else None
    if c["type"] != "scale":
        if low in ("да", "yes", "иә", "true"):
            return "Да"
        if low in ("нет", "no", "жоқ", "false"):
            return "Нет"
    else:
        try:
            v = fmt_value(float(low.replace(",", ".")))
            return v if v in allowed else None
        except ValueError:
            return None
    return None
