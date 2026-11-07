"""Read-only analytics over the latest successful evaluation per checklist/conversation.

Quality metrics and CSV use final answers, as in the card. Agreement and accuracy
always use original model answers. Appeals count all history in the selected scope.
Rates are fractions (0..1); an empty denominator is None, never a fabricated zero.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
import json
import math
from pathlib import Path
import random

from .checklist import load_default
from .scoring import score

REFERENCE = Path(__file__).parent / "precedents" / "dev_reference.json"
DECIDED = {"confirmed", "changed", "decided"}
DEFINITION_FIELDS = ("id", "title", "type", "yes", "no", "na", "na_when", "steps", "guidance")


def _rate(n, total):
    return n / total if total else None


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _compatible(left, right, cid):
    a = next((c for c in left["criteria"] if c["id"] == cid), None)
    b = next((c for c in right["criteria"] if c["id"] == cid), None)
    return bool(a and b and left.get("rules", []) == right.get("rules", [])
                and all(a.get(f) == b.get(f) for f in DEFINITION_FIELDS))


def disagreement(answer):
    """Unknown vote history stays unknown; one successful vote is unanimous."""
    votes = answer.get("votes")
    if isinstance(votes, list) and votes:
        return len(set(votes)) > 1
    agreement = answer.get("agreement")
    if isinstance(agreement, str):
        try:
            agreed, total = map(int, agreement.split("/"))
            if 0 < agreed <= total:
                return agreed < total
        except ValueError:
            pass
    return None


def _violation(criterion, answer):
    if answer == "N/A":
        return False
    if criterion["type"] == "flag":
        return answer == "Да"
    if criterion["type"] == "scale":
        return float(answer) < max(float(s["value"]) for s in criterion["steps"])
    return answer == "Нет"


def _approved(db, config, conversations, appeals, checklists):
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))["dialogs"]
    default = load_default()
    labels = {(cid, key): value["answer"]
              for cid in conversations for key, value in reference.get(cid, {}).items()
              if _compatible(config, default, key)}
    # Decisions on older evaluations still represent a human label for the same
    # definition. A later decision wins, including confirmations after a change.
    current = {}
    for appeal in sorted((a for a in appeals if a["status"] in DECIDED),
                         key=lambda a: (a["decided_at"] or "", a["id"])):
        key = appeal["criterion_id"]
        old = checklists[appeal["checklist_id"]]
        if not _compatible(config, old, key):
            continue
        evkey = (appeal["evaluation_id"], key)
        if evkey not in current:
            answers = json.loads(appeal["answers"] or "{}")
            current[evkey] = answers.get(key, {}).get("answer")
        if appeal["new_answer"] is not None:
            current[evkey] = appeal["new_answer"]
        if current[evkey] is not None:
            labels[appeal["conversation_id"], key] = current[evkey]
    return labels


def _appeal_counts(rows):
    # Ответ проверяющего на сомнение модели — не оспаривание оператором: в счёт апелляций не идёт.
    rows = [a for a in rows if a.get("reason_code") != "model_doubt"]
    return {"submitted": len(rows), "decided": sum(a["status"] in DECIDED for a in rows),
            "changed": sum(a["status"] == "changed" for a in rows)}


def _summary(dialogues):
    decisions = [c for d in dialogues for c in d["criteria"]]
    known = [c for c in decisions if c["disagreement"] is not None]
    return {"evaluated": len(dialogues), "average_total": _mean(d["total"] for d in dialogues),
            "scored": sum(d["total"] is not None for d in dialogues),
            "gross": sum(d["gross"] is True for d in dialogues),
            "gross_n": sum(d["gross"] is not None for d in dialogues),
            "gross_rate": _rate(sum(d["gross"] is True for d in dialogues),
                                sum(d["gross"] is not None for d in dialogues)),
            "disagreements": sum(c["disagreement"] is True for c in known),
            "agreement_n": len(known), "decisions": len(decisions),
            "disagreement_rate": _rate(sum(c["disagreement"] is True for c in known), len(known))}


def _insights(db, dialogues, criteria):
    if not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='insights'"):
        return None
    selected = {(d["id"], d["evaluation_id"]): d for d in dialogues}
    latest = {}
    for row in db.q("SELECT conversation_id, evaluation_id, created_at, data FROM insights ORDER BY created_at, rowid"):
        key = (row["conversation_id"], row["evaluation_id"])
        if key in selected:
            latest[key] = row["data"]
    topics, flags = defaultdict(list), Counter()
    covered = 0
    for key, raw in latest.items():
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        # Разбор ещё идёт или завершился ошибкой — не данные (у готового status = "ok").
        if not isinstance(data, dict) or data.get("status", "ok") != "ok":
            continue
        covered += 1
        topic = data.get("topic")
        if isinstance(topic, str) and topic.strip():
            topics[topic].append(selected[key])
        flags.update({flag["code"] for flag in (data.get("flags") or [])
                      if isinstance(flag, dict) and isinstance(flag.get("code"), str)})
    titles = {c["id"]: c["title"] for c in criteria}
    rows = []
    for topic, ds in sorted(topics.items(), key=lambda x: (-len(x[1]), x[0])):
        violations = Counter(c["id"] for d in ds for c in d["criteria"] if c["violation"])
        top = sorted(violations, key=lambda k: (-violations[k], k))
        rows.append({"topic": topic, "count": len(ds), "average_total": _mean(d["total"] for d in ds),
                     "top_violation": {"id": top[0], "title": titles[top[0]], "count": violations[top[0]]} if top else None})
    return {"covered": covered, "topics": rows,
            "flags": [{"code": code, "count": n} for code, n in sorted(flags.items(), key=lambda x: (-x[1], x[0]))]}


def report(db, dataset_id=None, checklist_id=None):
    """No writes, model calls, or mutations of persisted evaluations."""
    with db.lock:
        return _report(db, dataset_id or None, checklist_id)


def _report(db, dataset_id, checklist_id):
    checklists = {c["id"]: c["config"] for c in db.q("SELECT id, config FROM checklists")}
    checklists = {key: json.loads(value) for key, value in checklists.items()}
    checklist_id = checklist_id if checklist_id is not None else max(checklists, default=None)
    if checklist_id not in checklists:
        raise ValueError("Чек-лист не найден.")
    config = checklists[checklist_id]
    rows = db.q(
        "SELECT e.*, c.language, c.channel, c.dataset_id FROM evaluations e "
        "JOIN conversations c ON c.id=e.conversation_id "
        "WHERE e.id IN (SELECT MAX(id) FROM evaluations WHERE status='ok' AND checklist_id=? "
        "GROUP BY conversation_id) AND (? IS NULL OR c.dataset_id=?) ORDER BY c.id",
        (checklist_id, dataset_id, dataset_id))
    all_appeals = db.q(
        "SELECT a.*, e.conversation_id, e.checklist_id, e.answers FROM appeals a "
        "JOIN evaluations e ON e.id=a.evaluation_id JOIN conversations c ON c.id=e.conversation_id "
        "WHERE e.status='ok' AND (? IS NULL OR c.dataset_id=?)", (dataset_id, dataset_id))
    scope_appeals = [a for a in all_appeals if a["checklist_id"] == checklist_id]
    appeals_by_eval = defaultdict(list)
    for a in scope_appeals:
        appeals_by_eval[a["evaluation_id"]].append(a)
    approved = _approved(db, config, {r["conversation_id"] for r in rows}, all_appeals, checklists)
    dialogues = []
    for ev in rows:
        answers = json.loads(ev["answers"])
        final = {key: a["answer"] for key, a in answers.items()}
        decisions = sorted((a for a in appeals_by_eval[ev["id"]] if a["status"] in DECIDED),
                           key=lambda a: (a["decided_at"] or "", a["id"]))
        for a in decisions:
            if a["new_answer"] is not None:
                final[a["criterion_id"]] = a["new_answer"]
        scored = score(config, final)
        criteria = []
        for c in config["criteria"]:
            key, original = c["id"], answers[c["id"]]
            label = approved.get((ev["conversation_id"], key))
            decision = next((a for a in reversed(decisions) if a["criterion_id"] == key), None)
            criteria.append({"id": key, "title": c["title"], "answer": final[key],
                             "original_answer": original["answer"], "approved_answer": label,
                             "violation": _violation(c, final[key]), "na": final[key] == "N/A",
                             "disagreement": disagreement(original), "weight": c["weight"],
                             "points": scored["criteria"][key]["points_eff"],
                             "evidence": original.get("evidence", []),
                             "explanation": original.get("explanation", ""),
                             "reviewer_reason": decision["reason"] if decision else None})
        dialogues.append({"id": ev["conversation_id"], "evaluation_id": ev["id"],
                          "language": ev["language"], "channel": ev["channel"],
                          "total": scored["total"], "gross": scored["gross"], "criteria": criteria})
    criteria = []
    for criterion in config["criteria"]:
        key = criterion["id"]
        cs = [next(c for c in d["criteria"] if c["id"] == key) for d in dialogues]
        applicable = [c for c in cs if not c["na"]]
        known = [c for c in cs if c["disagreement"] is not None]
        labelled = [c for c in cs if c["approved_answer"] is not None]
        matches = sum(c["original_answer"] == c["approved_answer"] for c in labelled)
        criteria.append({"id": key, "title": criterion["title"], "type": criterion["type"],
                         "n": len(cs), "applicable_n": len(applicable),
                         "violations": sum(c["violation"] for c in cs),
                         "violation_rate": _rate(sum(c["violation"] for c in cs), len(applicable)),
                         "na_rate": _rate(sum(c["na"] for c in cs), len(cs)),
                         "disagreement_rate": _rate(sum(c["disagreement"] is True for c in known), len(known)),
                         "agreement_n": len(known),
                         "average_points": _mean(c["points"] for c in applicable) if criterion["type"] != "flag" else None,
                         "accuracy": {"rate": _rate(matches, len(labelled)), "n": len(labelled), "matches": matches},
                         "appeals": _appeal_counts([a for a in scope_appeals if a["criterion_id"] == key]),
                         "violating_dialogues": [{k: d[k] for k in ("id", "evaluation_id", "language", "channel", "total")}
                                                 for d in dialogues if any(c["id"] == key and c["violation"] for c in d["criteria"])]})
    histogram = [{"from": n, "to": n + 10, "count": 0} for n in range(0, 100, 10)]
    queue, remaining = [], []
    flag_ids = {c["id"] for c in config["criteria"] if c["type"] == "flag"}
    for d in dialogues:
        if d["total"] is not None:
            histogram[min(int(d["total"] // 10), 9)]["count"] += 1
        reasons = []
        if d["gross"]:
            reasons.append({"code": "gross", "criteria": [c["id"] for c in d["criteria"]
                                                           if c["violation"] and c["id"] in flag_ids]})
        uncertain = [c["id"] for c in d["criteria"] if c["disagreement"]]
        if uncertain:
            reasons.append({"code": "disagreement", "criteria": uncertain})
        if d["total"] is not None and d["total"] < 60:
            reasons.append({"code": "low_score"})
        item = {k: d[k] for k in ("id", "evaluation_id", "language", "channel", "total")}
        if reasons:
            queue.append({**item, "reasons": reasons})
        else:
            remaining.append(item)
    sample = random.Random(26).sample(remaining, math.ceil(len(remaining) * .1))
    queue += [{**d, "reasons": [{"code": "sample"}]} for d in sorted(sample, key=lambda x: x["id"])]
    slices = {}
    for field in ("language", "channel"):
        groups = defaultdict(list)
        for d in dialogues:
            groups[d[field] or "Не указан"].append(d)
        slices[field] = [{"value": key, **_summary(ds)} for key, ds in sorted(groups.items())]
    return {"checklist_id": checklist_id, "dataset_id": dataset_id, "basis": "final",
            "summary": {**_summary(dialogues), "appeals": _appeal_counts(scope_appeals)},
            "criteria": criteria, "histogram": histogram, "slices": slices, "review_queue": queue,
            "sample": {"seed": 26, "eligible": len(remaining), "selected": len(sample)},
            "insights": _insights(db, dialogues, config["criteria"]), "dialogues": dialogues}


def export_csv(data):
    """UTF-8 BOM for spreadsheet apps; quoting preserves newlines and evidence JSON."""
    out = io.StringIO(newline="")
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["id", "язык", "канал", "критерий", "ответ", "баллы", "вес", "итог", "грубая ошибка",
                     "оценки разошлись", "реплики-доказательства", "обоснование", "ответ модели", "решение проверяющего"])
    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    for d in data["dialogues"]:
        for c in d["criteria"]:
            writer.writerow([safe(v) for v in [d["id"], d["language"], d["channel"], f'{c["id"]} · {c["title"]}',
                             c["answer"], c["points"], c["weight"], d["total"], d["gross"], c["disagreement"],
                             json.dumps(c["evidence"], ensure_ascii=False), c["explanation"],
                             c["original_answer"], c["reviewer_reason"]]])
    return out.getvalue()
