"""«Модель спрашивает»: спорные решения (голоса разошлись) → вопросы к чек-листу → ответ проверяющего.

Без сети: провайдер `fake`; оценку с разошедшимися голосами кладём в базу так же, как её сохраняет прогон
(answers с votes/agreement и сырые ответы трёх голосов в raw).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa import doubts
from cqa.api import create_app
from tests.test_api import CHAT, PAYLOAD, run_and_wait, upload

CRIT = "07"


def raw_vote(answer: str, explanation: str, evidence: list[int]) -> str:
    return json.dumps({"criteria": {CRIT: {"answer": answer, "explanation": explanation, "evidence": evidence}}},
                      ensure_ascii=False)


def split_copy(db, ev_id: int, votes=("Нет", "Да", "Нет")) -> int:
    """Новая оценка того же диалога: по 07 голоса разошлись, остальное — как в исходной."""
    ev = db.evaluation(ev_id)
    answers = {k: {**v, "votes": [v["answer"]] * 3, "agreement": "3/3"} for k, v in ev["answers"].items()}
    winner = max(set(votes), key=votes.count)
    answers[CRIT] = {**answers[CRIT], "answer": winner, "votes": list(votes),
                     "agreement": f"{votes.count(winner)}/{len(votes)}",
                     "explanation": "Голос большинства: одно прощание без итога [6]."}
    raw = [raw_vote(v, f"Довод за «{v}»: {'одно прощание без итога' if v == 'Нет' else 'шаг назван, попрощался'} [6].",
                    [6]) for v in votes]
    return db.insert_evaluation({**ev, "answers": answers, "raw": json.dumps(raw, ensure_ascii=False),
                                 "run_id": None, "created_at": None})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: None)
    app = create_app(tmp_path / "doubts.db", web_dir=tmp_path / "web", data_dir=tmp_path / "data", allow_fake=True)
    with TestClient(app) as client:
        upload(client)
        run = run_and_wait(client, {"dataset_id": "TEST", "provider": "fake"})
        first = next(i["evaluation_id"] for i in run["items"] if i["conversation_id"] == CHAT["conversation_id"])
        yield client, app.state.db, split_copy(app.state.db, first)


def test_split_only_for_non_unanimous_votes():
    assert doubts.split({"agreement": "2/3"}) and doubts.split({"agreement": "1/3"})
    assert not doubts.split({"agreement": "3/3"}) and not doubts.split({}) and not doubts.split(None)
    assert not doubts.split({"agreement": "x/3"})


def test_positions_from_raw_votes_majority_first(env):
    _, db, ev_id = env
    pos = doubts.positions(db, db.evaluation(ev_id), CRIT)
    assert [(p["answer"], p["count"]) for p in pos] == [("Нет", 2), ("Да", 1)]
    assert "одно прощание" in pos[0]["explanation"] and "шаг назван" in pos[1]["explanation"]
    assert pos[0]["evidence"] == [6]


def test_positions_follow_reused_answer_to_its_source(env):
    _, db, ev_id = env
    ev = db.evaluation(ev_id)
    answers = {k: {**v, "reused_from": ev_id} if k == CRIT else v for k, v in ev["answers"].items()}
    newer = db.insert_evaluation({**ev, "answers": answers, "raw": "[]", "run_id": None, "created_at": None})
    pos = doubts.positions(db, db.evaluation(newer), CRIT)
    assert [p["answer"] for p in pos] == ["Нет", "Да"] and "шаг назван" in pos[1]["explanation"]


def test_summary_counts_disputes_without_model_calls(env):
    c, _, ev_id = env
    s = c.get("/api/doubts", params={"dataset_id": "TEST"}).json()
    assert s["disputed"] == 1 and s["dialogs"] == 2 and s["decisions"] >= 8
    row = s["criteria"][0]
    assert row["id"] == CRIT and row["disputed"] == 1 and row["questions"] is None
    assert row["conversation_ids"] == [CHAT["conversation_id"]]
    assert c.get("/api/doubts", params={"dataset_id": "NOPE"}).status_code == 404


def test_summary_lists_every_criterion_of_newest_version(env):
    """«0 споров из N» по новому критерию — тоже ответ: формулировка читается однозначно."""
    c, _, _ = env
    rows = {r["id"]: r for r in c.get("/api/doubts", params={"dataset_id": "TEST"}).json()["all"]}
    assert set(rows) == {"01", "02", "03", "04", "05", "06", "07", "08"}
    assert rows[CRIT]["disputed"] == 1 and rows["01"]["disputed"] == 0
    assert rows["01"]["decisions"] == 2 and rows[CRIT]["decisions"] == 2


def test_card_shows_vote_positions_only_for_split_criterion(env):
    c, _, ev_id = env
    crits = {x["id"]: x for x in c.get(f"/api/evaluations/{ev_id}").json()["criteria"]}
    assert [p["answer"] for p in crits[CRIT]["positions"]] == ["Нет", "Да"]
    assert crits["01"]["positions"] is None


def test_questions_generated_once_then_cached(env):
    c, db, ev_id = env
    body = {"criterion_id": CRIT, "dataset_id": "TEST", "provider": "fake"}
    r = c.post("/api/doubts/questions", json=body)
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["cached"] is False and q["disputes"][0]["evaluation_id"] == ev_id
    options = q["questions"][0]["options"]
    assert {o["answer"] for o in options} == {"Нет", "Да"}  # только ответы, которые дали голоса
    assert all(o["rule"] and o["label"] for o in options)
    again = c.post("/api/doubts/questions", json=body).json()
    assert again["cached"] is True and again["questions"] == q["questions"] and again["cost_usd"] == 0
    assert c.get("/api/doubts", params={"dataset_id": "TEST"}).json()["criteria"][0]["questions"] == q["questions"]
    one = c.post("/api/doubts/questions", json={"criterion_id": CRIT, "evaluation_id": ev_id, "provider": "fake"})
    assert one.status_code == 200 and one.json()["disputes"][0]["conversation_id"] == CHAT["conversation_id"]


def test_questions_need_a_dispute(env):
    c, _, ev_id = env
    r = c.post("/api/doubts/questions", json={"criterion_id": "01", "dataset_id": "TEST", "provider": "fake"})
    assert r.status_code == 422 and "не расходятся" in r.json()["detail"]
    r = c.post("/api/doubts/questions", json={"criterion_id": "01", "evaluation_id": ev_id, "provider": "fake"})
    assert r.status_code == 422


def test_resolve_changes_answer_recounts_total_and_closes_dispute(env):
    c, db, ev_id = env
    before = c.get(f"/api/evaluations/{ev_id}").json()
    r = c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT, "answer": "Да",
                                            "question": "Шаг назван раньше, в конце прощание — это итог?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["appeal"]["reason_code"] == "model_doubt" and out["appeal"]["status"] == "changed"
    crit = next(x for x in out["evaluation"]["criteria"] if x["id"] == CRIT)
    assert crit["original"]["answer"] == "Нет" and crit["final_answer"] == "Да"
    assert out["evaluation"]["final"]["total"] >= before["final"]["total"]
    assert c.get("/api/doubts", params={"dataset_id": "TEST"}).json()["disputed"] == 0
    again = c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT, "answer": "Нет"})
    assert again.status_code == 409


def test_resolve_same_as_model_confirms(env):
    c, _, ev_id = env
    out = c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT, "answer": "Нет"}).json()
    assert out["appeal"]["status"] == "confirmed" and out["appeal"]["new_answer"] is None
    assert "голоса модели" in out["appeal"]["reason"]


def test_resolve_rejects_unanimous_invalid_and_open_appeal(env):
    c, db, ev_id = env
    assert c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": "01",
                                               "answer": "Да"}).status_code == 422
    assert c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT,
                                               "answer": "20"}).status_code == 422
    r = c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": CRIT, "comment": "Оспариваю"})
    assert r.status_code == 201
    assert c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT,
                                               "answer": "Да"}).status_code == 409


def test_operator_cannot_file_model_doubt_appeal(env):
    c, _, ev_id = env
    r = c.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": CRIT, "comment": "x",
                                     "reason_code": "model_doubt"})
    assert r.status_code == 422


def test_analytics_does_not_count_doubt_answers_as_appeals(env):
    c, _, ev_id = env
    c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT, "answer": "Да"})
    a = c.get("/api/analytics", params={"dataset_id": "TEST"}).json()
    row = next(x for x in a["criteria"] if x["id"] == CRIT)
    assert row["appeals"]["submitted"] == 0 and a["summary"]["appeals"]["submitted"] == 0


def test_resolved_doubt_becomes_precedent_for_similar_dialogs(env):
    c, db, ev_id = env
    c.post("/api/doubts/resolve", json={"evaluation_id": ev_id, "criterion_id": CRIT, "answer": "Да",
                                        "reason": "Шаг назван раньше — это итог."})
    other = next(r for r in PAYLOAD["data"]["records"] if r["conversation_id"] != CHAT["conversation_id"])
    examples = __import__("cqa.precedents", fromlist=["select"]).select(
        db, db.record(other["conversation_id"]), db.checklist(1)["config"], k=5)
    mine = [p for p in examples if p["conversation_id"] == CHAT["conversation_id"]]
    assert mine and mine[0]["source"] == "reviewer" and mine[0]["labels"][CRIT]["answer"] == "Да"


def test_schema_is_accepted_by_claude_structured_outputs():
    """Ограничения, которые Claude не принимает в строгой схеме, сюда не попадают (урок 7.11)."""
    crit = {"id": "04", "type": "scale", "na": True, "steps": [{"value": v, "text": "x"} for v in (20, 15, 10, 5, 0)]}
    text = json.dumps(doubts.build_schema(crit, ["a", "b"], ["15", "10"]))
    for banned in ("minimum", "maximum", "maxLength", "minLength", "minItems", "maxItems"):
        assert banned not in text
    enums = []

    def walk(x):
        if isinstance(x, dict):
            if "enum" in x:
                enums.append(x["enum"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(json.loads(text))
    assert enums and all(None not in e for e in enums)
    assert ["15", "10"] in enums  # варианты — только ответы голосов


def test_prompt_masks_personal_data_and_cites_replies(env):
    _, db, ev_id = env
    d = doubts.dispute(db, db.evaluation(ev_id), CRIT)
    cfg = db.checklist(1)["config"]
    user = doubts.build_user(cfg, CRIT, [d], {CHAT["conversation_id"]: db.record(CHAT["conversation_id"])})
    assert "4111 1111 1111 1111" not in user and "[6] оператор:" in user and "[5] клиент:" in user
    assert "Довод за «Да»" in user and "Довод за «Нет»" in user
