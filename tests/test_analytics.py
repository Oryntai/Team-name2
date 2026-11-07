"""Analytics uses only a small temporary database; no model or network calls."""
from copy import deepcopy
import csv
import io
import json
import random

import pytest
from fastapi.testclient import TestClient

from cqa import analytics
from cqa.api import create_app
from cqa.db import DB
from cqa.scoring import score


CONFIG = {"name": "Тест", "rounding": 2, "rules": [], "criteria": [
    {"id": "01", "title": "Контакт", "type": "bool", "weight": 50, "na": True},
    {"id": "02", "title": "Консультация", "type": "scale", "weight": 50, "na": True,
     "steps": [{"value": 20, "text": "Полная"}, {"value": 10, "text": "Частичная"}, {"value": 0, "text": "Нет"}]},
    {"id": "08", "title": "Грубые ошибки", "type": "flag", "weight": 0, "na": False},
]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    db = DB(tmp_path / "analytics.db")
    db.create_checklist(CONFIG)
    ref = tmp_path / "reference.json"
    ref.write_text(json.dumps({"dialogs": {"a": {"01": {"answer": "Нет"}, "02": {"answer": "20"}}}}))
    monkeypatch.setattr(analytics, "REFERENCE", ref)
    monkeypatch.setattr(analytics, "load_default", lambda: deepcopy(CONFIG))
    yield db
    db.close()


def conversation(db, cid, dataset="DEV", language="RU", channel="agent chat"):
    db.save_dataset(dataset, "test", [{"conversation_id": cid, "language": language, "channel": channel,
                                       "transcript": [{"speaker": "operator", "text": "Текст", "timestamp": "00:00"}]}])


def evaluation(db, cid, values=("Да", "20", "Нет"), cl=1, status="ok", votes=True, extra=None):
    cfg = db.checklist(cl)["config"]
    answers = {c["id"]: {"answer": v, "evidence": [1], "explanation": 'Строка, "цитата"\nНовая строка',
                           **({"votes": [v, v, v], "agreement": "3/3"} if votes else {})}
               for c, v in zip(cfg["criteria"], values)}
    for key, value in (extra or {}).items():
        answers[key].update(value)
    scored = score(cfg, {k: v["answer"] for k, v in answers.items()})
    return db.insert_evaluation({"conversation_id": cid, "checklist_id": cl, "status": status,
                                 "answers": answers, "total": scored["total"], "gross": scored["gross"],
                                 "model": "fake", "provider": "fake", "cost_usd": 0})


def appeal(db, ev, key, status="open", new=None):
    aid = db.create_appeal(ev, key, "Прошу проверить")
    if status != "open":
        db.decide_appeal(aid, status, new, "Решение проверяющего")
    return aid


def populated(db):
    conversation(db, "a")
    conversation(db, "b", language="KZ", channel="voice call")
    conversation(db, "c")
    evaluation(db, "a", ("Нет", "0", "Да"))  # superseded
    ev = evaluation(db, "a", ("Да", "10", "Да"), extra={"01": {"votes": ["Да", "Нет", "Да"], "agreement": "2/3"}})
    evaluation(db, "a", status="error")  # latest failure must not hide success
    evaluation(db, "b", ("N/A", "20", "Нет"), votes=False)
    evaluation(db, "c", ("Нет", "0", "Нет"))
    appeal(db, ev, "02", "changed", "20")
    appeal(db, ev, "02", "confirmed")  # confirmation must preserve the change
    appeal(db, ev, "01")
    return ev


def test_latest_success_metrics_final_answers_and_no_writes(db):
    populated(db)
    before = {table: db.q(f"SELECT * FROM {table}") for table in ("evaluations", "appeals", "runs", "checklists")}
    data = analytics.report(db)
    s = data["summary"]
    assert s["evaluated"] == 3
    assert s["average_total"] == pytest.approx(200 / 3)
    assert s["gross_rate"] == pytest.approx(1 / 3)
    assert s["disagreement_rate"] == pytest.approx(1 / 6)
    assert s["agreement_n"] == 6 and s["decisions"] == 9
    assert s["appeals"] == {"submitted": 3, "decided": 2, "changed": 1}
    c1, c2, flag = data["criteria"]
    assert c1["violation_rate"] == .5 and c1["na_rate"] == pytest.approx(1 / 3)
    assert c1["average_points"] == 25
    assert c2["average_points"] == pytest.approx(150 / 3)
    assert c2["violations"] == 1
    assert flag["violations"] == 1  # «Да» is the violation for a flag
    assert flag["average_points"] is None
    assert [d["id"] for d in c1["violating_dialogues"]] == ["c"]
    assert data["summary"]["scored"] == 3
    assert {g["value"]: g["average_total"] for g in data["slices"]["language"]} == {"KZ": 100, "RU": 50}
    assert {g["value"]: g["evaluated"] for g in data["slices"]["channel"]} == {"agent chat": 2, "voice call": 1}
    assert data["histogram"][0]["count"] == 1
    assert data["histogram"][-1]["count"] == 2  # 100 is included
    assert sum(b["count"] for b in data["histogram"]) == 3
    assert before == {table: db.q(f"SELECT * FROM {table}") for table in before}


def test_filters_default_latest_checklist_and_appeal_history(db):
    populated(db)
    new, _ = db.create_checklist(CONFIG)
    evaluation(db, "a", cl=new)
    conversation(db, "other", dataset="PROD")
    evaluation(db, "other", cl=new)
    assert analytics.report(db)["summary"]["evaluated"] == 2
    assert analytics.report(db, "DEV")["summary"]["evaluated"] == 1
    assert analytics.report(db, "DEV", 1)["summary"]["evaluated"] == 3
    assert analytics.report(db, "PROD", 1)["summary"]["evaluated"] == 0
    assert analytics.report(db, checklist_id=new)["summary"]["appeals"]["submitted"] == 0
    # Appeal history still counts after a new successful evaluation supersedes it.
    evaluation(db, "a")
    assert analytics.report(db, checklist_id=1)["summary"]["appeals"]["submitted"] == 3
    with pytest.raises(ValueError, match="Чек-лист"):
        analytics.report(db, checklist_id=999)


def test_accuracy_original_vs_reference_and_reviewer_overrides(db):
    ev = populated(db)
    c1, c2, _ = analytics.report(db)["criteria"]
    assert c1["accuracy"] == {"rate": 0, "n": 1, "matches": 0}
    # Final is 20, original model is 10: comparison must NOT use final.
    assert c2["accuracy"] == {"rate": 0, "n": 1, "matches": 0}
    appeal(db, ev, "01", "confirmed")  # now approved «Да», overrides DEV «Нет»
    assert analytics.report(db)["criteria"][0]["accuracy"]["rate"] == 1
    # Human labels on an older evaluation survive reevaluation of the same definition.
    evaluation(db, "a")
    assert analytics.report(db)["criteria"][1]["accuracy"] == {"rate": 1, "n": 1, "matches": 1}


def test_accuracy_skips_changed_definitions_but_allows_weight_changes(db):
    populated(db)
    cfg = deepcopy(CONFIG)
    cfg["criteria"][0]["yes"] = "Новое определение"
    cl, _ = db.create_checklist(cfg)
    evaluation(db, "a", cl=cl)
    assert analytics.report(db)["criteria"][0]["accuracy"]["n"] == 0
    assert analytics.report(db)["criteria"][1]["accuracy"]["n"] == 1
    cfg = deepcopy(CONFIG)
    cfg["criteria"][0]["weight"] = 20
    cl, _ = db.create_checklist(cfg)
    evaluation(db, "a", cl=cl)
    assert analytics.report(db)["criteria"][0]["accuracy"]["n"] == 1


def test_empty_and_all_na_have_no_fabricated_rates(db):
    data = analytics.report(db)
    assert data["summary"]["evaluated"] == 0
    assert data["summary"]["average_total"] is None
    assert data["summary"]["gross_rate"] is None
    assert data["summary"]["disagreement_rate"] is None
    assert data["criteria"][0]["violation_rate"] is None
    assert data["criteria"][0]["accuracy"]["n"] == 0
    assert data["review_queue"] == [] and data["insights"] is None
    conversation(db, "a")
    evaluation(db, "a", ("N/A", "N/A", "Нет"))
    data = analytics.report(db)
    assert data["summary"]["average_total"] is None and data["summary"]["scored"] == 0
    assert data["criteria"][0]["na_rate"] == 1
    assert data["criteria"][0]["average_points"] is None
    assert data["criteria"][0]["violation_rate"] is None
    assert sum(b["count"] for b in data["histogram"]) == 0
    assert all(r["code"] != "low_score" for d in data["review_queue"] for r in d["reasons"])


def test_unevaluated_and_failed_only_dialogues_are_excluded(db):
    conversation(db, "pending")
    conversation(db, "failed")
    evaluation(db, "failed", status="error")
    assert analytics.report(db)["summary"]["evaluated"] == 0
    assert len(list(csv.reader(io.StringIO(analytics.export_csv(analytics.report(db)))))) == 1


def test_sixty_is_not_low_and_histogram_boundary_is_inclusive(db):
    cfg = deepcopy(CONFIG)
    cfg["criteria"][0]["weight"] = 20
    cfg["criteria"][1]["weight"] = 80
    cl, _ = db.create_checklist(cfg)
    conversation(db, "sixty")
    evaluation(db, "sixty", ("Да", "10", "Нет"), cl=cl)
    data = analytics.report(db)
    assert data["summary"]["average_total"] == 60
    assert data["histogram"][6]["count"] == 1
    assert all(r["code"] != "low_score" for d in data["review_queue"] for r in d["reasons"])


def test_reviewer_labels_cross_compatible_checklists_and_follow_decision_time(db):
    conversation(db, "a")
    ev = evaluation(db, "a")
    first = appeal(db, ev, "01", "changed", "Нет")
    second = appeal(db, ev, "01", "changed", "Да")
    db.ex("UPDATE appeals SET decided_at='2026-03-01' WHERE id=?", (first,))
    db.ex("UPDATE appeals SET decided_at='2026-02-01' WHERE id=?", (second,))
    cfg = deepcopy(CONFIG)
    cfg["criteria"][0]["weight"] = 20
    cl, _ = db.create_checklist(cfg)
    evaluation(db, "a", cl=cl)
    assert analytics.report(db)["criteria"][0]["accuracy"] == {"rate": 0, "n": 1, "matches": 0}


@pytest.mark.parametrize("answer, expected", [
    ({}, None), ({"votes": []}, None), ({"votes": ["Да"]}, False),
    ({"votes": ["Да", "Нет"]}, True), ({"agreement": "2/3"}, True),
    ({"agreement": "1/1"}, False), ({"agreement": "3/3"}, False),
    ({"agreement": "broken"}, None), ({"agreement": "0/0"}, None),
])
def test_disagreement_history(answer, expected):
    assert analytics.disagreement(answer) is expected


def test_review_queue_reasons_and_stable_sample(db):
    populated(db)
    for i in range(20):
        cid = f"clean-{i:02}"
        conversation(db, cid)
        evaluation(db, cid)
    state = random.getstate()
    data = analytics.report(db)
    assert random.getstate() == state  # private seed does not affect other code
    assert data["sample"] == {"seed": 26, "eligible": 21, "selected": 3}
    queue = {d["id"]: d["reasons"] for d in data["review_queue"]}
    assert queue["a"] == [{"code": "gross", "criteria": ["08"]}, {"code": "disagreement", "criteria": ["01"]}]
    assert queue["c"] == [{"code": "low_score"}]
    assert sum(r == [{"code": "sample"}] for r in queue.values()) == 3
    assert data["review_queue"] == analytics.report(db)["review_queue"]


def test_insights_optional_latest_matching_evaluation_and_flag_counts(db):
    ev = populated(db)
    db.ex("CREATE TABLE insights(conversation_id TEXT, evaluation_id INTEGER, created_at TEXT, model TEXT, cost_usd REAL, data TEXT)")
    def add(cid, eid, created, data):
        db.ex("INSERT INTO insights VALUES(?,?,?,'fake',0,?)", (cid, eid, created, json.dumps(data)))
    add("a", ev, "2026-01-01", {"topic": "Старая тема", "flags": []})
    add("a", ev, "2026-02-01", {"topic": "Вход", "flags": [{"code": "risk", "turns": [1]}, {"code": "risk", "turns": [2]}]})
    add("a", ev - 1, "2026-03-01", {"topic": "Не та оценка", "flags": [{"code": "stale"}]})
    b = db.one("SELECT id FROM evaluations WHERE conversation_id='b'")["id"]
    add("b", b, "2026-02-01", {"topic": "Вход", "flags": [{"code": "risk"}]})
    insights = analytics.report(db)["insights"]
    assert insights["covered"] == 2
    assert insights["topics"] == [{"topic": "Вход", "count": 2, "average_total": 100,
                                    "top_violation": {"id": "08", "title": "Грубые ошибки", "count": 1}}]
    assert insights["flags"] == [{"code": "risk", "count": 2}]
    assert analytics.report(db, "missing")["insights"] == {"covered": 0, "topics": [], "flags": []}
    c = db.one("SELECT id FROM evaluations WHERE conversation_id='c'")["id"]
    db.ex("INSERT INTO insights VALUES('c',?,'2026-03-01','fake',0,'broken JSON')", (c,))
    assert analytics.report(db)["insights"] == insights


def test_csv_round_trip_and_final_vs_model(db):
    populated(db)
    db.ex("UPDATE evaluations SET answers=json_set(answers, '$.01.explanation', ?) WHERE conversation_id='c'", ('=HYPERLINK("x")',))
    result = analytics.export_csv(analytics.report(db))
    assert result.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(result.lstrip("\ufeff"))))
    assert len(rows) == 9
    row = next(r for r in rows if r["id"] == "a" and r["критерий"].startswith("02"))
    assert row["ответ"] == "20" and row["ответ модели"] == "10"
    assert row["баллы"] == "50.0" and row["итог"] == "100.0"
    assert row["реплики-доказательства"] == "[1]"
    assert row["обоснование"] == 'Строка, "цитата"\nНовая строка'
    assert row["решение проверяющего"] == "Решение проверяющего"
    assert next(r for r in rows if r["id"] == "c")["обоснование"].startswith("'=HYPERLINK")


def test_http_endpoints_filters_validation_and_export(db, tmp_path):
    populated(db)
    app = create_app(db.path, web_dir=tmp_path, allow_fake=True)
    try:
        with TestClient(app) as client:
            response = client.get("/api/analytics?dataset_id=DEV&checklist_id=1")
            assert response.status_code == 200
            assert response.json()["summary"]["evaluated"] == 3
            assert "dialogues" not in response.json()
            assert client.get("/api/analytics?dataset_id=").json()["summary"]["evaluated"] == 3
            assert client.get("/api/analytics?dataset_id=missing").json()["summary"]["evaluated"] == 0
            assert client.get("/api/analytics?checklist_id=999").status_code == 422
            assert client.get("/api/analytics?checklist_id=no").status_code == 422
            assert client.get("/api/analytics/export.csv?checklist_id=999").status_code == 422
            response = client.get("/api/analytics/export.csv?dataset_id=DEV&checklist_id=1")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/csv")
            assert 'attachment; filename="cqa-analytics.csv"' == response.headers["content-disposition"]
            assert response.headers["cache-control"] == "no-store"
            rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
            assert len(rows) == 10
            response = client.get("/api/analytics/export.csv?dataset_id=missing")
            assert len(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))) == 1
    finally:
        app.state.db.close()
