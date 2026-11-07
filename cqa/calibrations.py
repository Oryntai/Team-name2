"""Калибровка: сравнение с исходной оценкой модели, без апелляций и новых вызовов."""
from __future__ import annotations

from .scoring import score


def compare(calibration: dict, evaluation: dict, config: dict) -> dict:
    model_answers = {cid: a["answer"] for cid, a in evaluation["answers"].items()}
    human = score(config, calibration["answers"])
    model = score(config, model_answers)
    criteria = []
    for c in config["criteria"]:
        cid = c["id"]
        answer = evaluation["answers"][cid]
        criteria.append({"id": cid, "title": c["title"], "human_answer": calibration["answers"][cid],
                         "model_answer": model_answers[cid], "matched": calibration["answers"][cid] == model_answers[cid],
                         "explanation": answer.get("explanation", ""), "evidence": answer.get("evidence") or []})
    delta = None if human["total"] is None or model["total"] is None else round(human["total"] - model["total"], 4)
    return {**calibration, "criteria": criteria,
            "human": {"total": human["total"], "gross": human["gross"]},
            "model": {"total": model["total"], "gross": model["gross"]}, "delta": delta}


def summarize(items: list[dict]) -> dict:
    # Версии с одинаковыми id критериев могут иметь разные определения — не смешиваем их.
    criteria = {}
    deltas = []
    for item in items:
        if item["delta"] is not None:
            deltas.append(item["delta"])
        for c in item["criteria"]:
            key = (item["checklist_id"], c["id"])
            row = criteria.setdefault(key, {"checklist_id": item["checklist_id"], "id": c["id"], "title": c["title"],
                                            "matched": 0, "total": 0})
            row["matched"] += int(c["matched"])
            row["total"] += 1
    return {"count": len(items), "criteria": list(criteria.values()), "scored_count": len(deltas),
            "mean_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "mean_absolute_delta": round(sum(abs(d) for d in deltas) / len(deltas), 4) if deltas else None}
