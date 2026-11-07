"""Итог по правилам чек-листа (стр. 2 PDF организаторов).

1. N/A — только где разрешён. 2. Да = полный вес, Нет = 0; у шкалы — только указанные ступени.
3. Баллы шкалы = вес × ступень / верхняя ступень. 4. N/A исключаем, веса применимых масштабируем до 100.
5. Итог округляем до `rounding` знаков (половина вверх); нет применимых критериев с весом → итога нет.
Грубая ошибка (тип flag, «Да») — отдельная отметка, итог не обнуляет.
Секция: баллы её критериев после перераспределения / её максимум после перераспределения × 100.
Промежуточные значения не округляем; округлённые поля *_eff — только для показа.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .checklist import NA, YES, allowed_answers, sections


class ScoreError(ValueError):
    pass


def _d(x) -> Decimal:
    return Decimal(str(x))


def _round(x: Decimal, digits: int = 2) -> float:
    return float(x.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def check_answer(c: dict, answer) -> str:
    a = None if answer is None else str(answer).strip()
    allowed = allowed_answers(c)
    if a not in allowed:
        raise ScoreError(f"Критерий {c['id']}: недопустимый ответ {answer!r}; допустимо: {', '.join(allowed)}.")
    return a


def score(config: dict, answers: dict) -> dict:
    """answers: {id критерия: "Да" | "Нет" | "N/A" | "15"}. Недопустимый или пропущенный ответ → ScoreError."""
    digits = int(config.get("rounding", 2))
    rows: dict[str, dict] = {}
    raw: dict[str, tuple[Decimal, Decimal]] = {}
    gross_ids: list[str] = []
    den = Decimal(0)
    for c in config["criteria"]:
        a = check_answer(c, answers.get(c["id"]))
        w = _d(c.get("weight") or 0)
        row = {"answer": a, "applicable": False, "weight": float(w), "points": 0.0, "weight_eff": 0.0, "points_eff": 0.0}
        rows[c["id"]] = row
        if c["type"] == "flag":
            if a == YES:
                gross_ids.append(c["id"])
            continue
        if a == NA or w == 0:
            continue
        if c["type"] == "bool":
            pts = w if a == YES else Decimal(0)
        else:
            top = max(_d(s["value"]) for s in c["steps"])
            pts = w * _d(a) / top
        raw[c["id"]] = (w, pts)
        den += w
    total = None
    k = Decimal(100) / den if den > 0 else Decimal(0)
    if den > 0:
        total = _round(sum(p for _, p in raw.values()) * k, digits)
        for cid, (w, p) in raw.items():
            rows[cid].update(applicable=True, points=_round(p, 4), weight_eff=_round(w * k), points_eff=_round(p * k))
    secs = []
    for name in sections(config):
        ids = [c["id"] for c in config["criteria"] if ((c.get("section") or "").strip() or "Без секции") == name]
        mx = sum((raw[i][0] for i in ids if i in raw), Decimal(0)) * k
        pt = sum((raw[i][1] for i in ids if i in raw), Decimal(0)) * k
        secs.append({"name": name, "ids": ids, "points_eff": _round(pt), "max_eff": _round(mx),
                     "percent": _round(pt / mx * 100) if mx > 0 else None})
    has_flags = any(c["type"] == "flag" for c in config["criteria"])
    return {"total": total, "gross": bool(gross_ids) if has_flags else None, "gross_ids": gross_ids,
            "applicable_weight": float(den), "criteria": rows, "sections": secs}
