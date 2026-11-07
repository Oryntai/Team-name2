"""Размышления модели: пересказ (thinking.display = summarized) сохраняется по каждому вызову и отдаётся в API."""
from cqa import checklist as ck
from cqa.db import DB
from cqa.vote import merge_results

CFG = ck.load_default()


def result(answer, thinking):
    return {"status": "ok", "answers": {c["id"]: {"answer": "Нет" if c["type"] == "flag" else answer if c["type"] == "bool" else
                                                    str(c["steps"][0]["value"]), "explanation": "x", "evidence": [1]}
                                        for c in CFG["criteria"]},
            "thinking": thinking, "raw": "{}", "cost_usd": 0.001, "input_tokens": 1, "output_tokens": 1, "latency_s": 1}


def test_votes_keep_thinking_of_each_successful_call_in_order():
    merged = merge_results([result("Да", "мысли 1"), {"status": "error", "error": "429"}, result("Нет", "мысли 3")], CFG)
    assert merged["thinking"] == ["мысли 1", "мысли 3"]


def test_thinking_round_trip_in_db(tmp_path):
    db = DB(tmp_path / "t.db")
    ev_id = db.insert_evaluation({"conversation_id": "c", "checklist_id": 1, "status": "ok", "answers": {},
                                  "thinking": ["Сначала проверяю приветствие…", ""]})
    assert db.evaluation(ev_id)["thinking"] == ["Сначала проверяю приветствие…", ""]
    assert db.evaluations_for("c")[0]["thinking"] == ["Сначала проверяю приветствие…", ""]
