"""Формат results.json — приложение к гайду «Как сдать результаты» (Формат JSON 1.0, 3 стр.).

Пример и запись об ошибке — из приложения дословно. Проверка не опирается на cqa/scoring.py: итог пересчитывается
по таблице «Правила заполнения», а наш расчёт сверяется с примером из приложения (85,38).
"""
import copy
import json

import pytest

from cqa import results as rs
from cqa.db import DB
from cqa.incremental import fingerprints
from cqa.scoring import score

APPENDIX_EXAMPLE = {
    "schema_version": "1.0",
    "team_name": "Название команды",
    "model": {"mode": "local", "name": "Название модели"},
    "results": [{
        "conversation_id": "demo-001",
        "status": "ok",
        "criteria": [
            {"criterion_id": "contact", "value": True, "rationale": "Оператор поздоровался и представился.",
             "evidence_turns": [4]},
            {"criterion_id": "discovery", "value": None, "rationale": "Клиент сразу ясно изложил вопрос.",
             "evidence_turns": [1]},
            {"criterion_id": "identification", "value": None, "rationale": "Общий вопрос не требует идентификации.",
             "evidence_turns": [1]},
            {"criterion_id": "consultation", "value": 15, "rationale": "Основной вопрос решён, упущена деталь.",
             "evidence_turns": [5, 6]},
            {"criterion_id": "empathy", "value": True, "rationale": "Вежливо признал неудобство клиента.",
             "evidence_turns": [4, 5]},
            {"criterion_id": "dialogue", "value": 7, "rationale": "Есть повтор, но ход диалога понятен.",
             "evidence_turns": [5, 6]},
            {"criterion_id": "closure", "value": True, "rationale": "Предложил дальнейшую помощь и попрощался.",
             "evidence_turns": [8]},
            {"criterion_id": "critical", "value": False, "rationale": "Грубых нарушений в репликах нет.",
             "evidence_turns": [4, 5, 6, 8]},
        ],
        "score": {"final_score": 85.38, "critical_flag": False},
    }],
}
APPENDIX_ERROR = {"conversation_id": "demo-002", "status": "error", "criteria": [], "score": None,
                  "error": "Не удалось получить ответ модели."}
CFG = rs.official_config()
MODEL = "claude-haiku-5-5"


def doc_with(mutate):
    d = copy.deepcopy(APPENDIX_EXAMPLE)
    mutate(d)
    return d


def crit(d, code):
    return next(c for c in d["results"][0]["criteria"] if c["criterion_id"] == code)


def test_appendix_example_and_error_record_pass():
    d = copy.deepcopy(APPENDIX_EXAMPLE)
    d["results"].append(APPENDIX_ERROR)
    assert rs.validate(d, expected_ids=["demo-001", "demo-002"], expect_count=2) == []


def test_codes_weights_and_steps_match_our_checklist():
    """Таблица приложения = наш действующий чек-лист: коды по порядку, веса, шкалы и где разрешён N/A."""
    assert list(rs.CODES.values()) == list(rs.SPEC)
    for c in CFG["criteria"]:
        allowed, weight = rs.SPEC[rs.CODES[c["id"]]]
        assert c["weight"] == weight, c["id"]
        assert (None in allowed) == bool(c["na"]), c["id"]
        if c["type"] == "scale":
            assert sorted(s["value"] for s in c["steps"]) == sorted(x for x in allowed if type(x) is int)


def test_our_score_matches_appendix_example():
    answers = {"01": "Да", "02": "N/A", "03": "N/A", "04": "15", "05": "Да", "06": "7", "07": "Да", "08": "Нет"}
    assert score(CFG, answers)["total"] == 85.38
    values = {c["criterion_id"]: c["value"] for c in APPENDIX_EXAMPLE["results"][0]["criteria"]}
    assert rs.spec_score(values) == 85.38


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: crit(d, "consultation").update(value=False), "consultation"),  # false — не число 0
    (lambda d: crit(d, "dialogue").update(value=5), "dialogue"),
    (lambda d: crit(d, "contact").update(value=None), "contact"),  # у contact N/A нет
    (lambda d: crit(d, "discovery").update(value="Да"), "discovery"),
    (lambda d: crit(d, "dialogue").update(value=7.0), "dialogue"),
    (lambda d: crit(d, "closure").update(rationale=" "), "обоснование"),
    (lambda d: crit(d, "closure").update(evidence_turns=[0]), "evidence_turns"),
    (lambda d: crit(d, "closure").update(evidence_turns=["8"]), "evidence_turns"),
    (lambda d: d["results"][0]["criteria"].pop(), "нет критериев critical"),
    (lambda d: d["results"][0]["criteria"].append(dict(crit(d, "contact"))), "дважды"),
    (lambda d: crit(d, "empathy").update(criterion_id="politeness"), "неизвестный"),
    (lambda d: crit(d, "contact").update(weight=10), "лишние поля"),
    (lambda d: d["results"][0]["score"].update(final_score=85.0), "по правилам приложения"),
    (lambda d: d["results"][0]["score"].update(final_score=85.384), "двумя знаками"),
    (lambda d: d["results"][0]["score"].update(critical_flag=True), "critical_flag"),
    (lambda d: d["results"][0].update(status="done"), "status"),
    (lambda d: d["model"].update(mode="cloud"), "model.mode"),
    (lambda d: d.update(schema_version="1"), "schema_version"),
    (lambda d: d.update(team_name=""), "team_name"),
    (lambda d: d["results"].append(copy.deepcopy(d["results"][0])), "Повторяются ID"),
    (lambda d: d["results"].append({**APPENDIX_ERROR, "score": 0}), "score — null"),
    (lambda d: d["results"].append({**APPENDIX_ERROR, "error": ""}), "причина"),
])
def test_rule_violations_are_caught(mutate, needle):
    errs = rs.validate(doc_with(mutate))
    assert any(needle in e for e in errs), errs


def test_ids_count_and_turn_range():
    d = copy.deepcopy(APPENDIX_EXAMPLE)
    assert any("Нет записей для: demo-002" in e for e in rs.validate(d, expected_ids=["demo-001", "demo-002"]))
    assert any("ровно 10" in e for e in rs.validate(d, expect_count=10))
    assert any("вне диалога" in e for e in rs.validate(d, n_turns={"demo-001": 6}))
    assert rs.validate(d, n_turns={"demo-001": 8}) == []


def test_strict_json_reading(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(rs.dump(APPENDIX_EXAMPLE), encoding="utf-8")
    assert rs.load_strict(p) == APPENDIX_EXAMPLE
    for bad in ('{"a": 1,}', '{"a": 1} // комментарий', '{"a": NaN}', '﻿{"a": 1}'):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            rs.load_strict(p)


# --- выгрузка из базы ---

def answers(values: dict, model: str = MODEL, config: dict = CFG, evidence=(1,)) -> dict:
    fp = fingerprints(config, model)
    return {k: {"answer": v, "explanation": f"Обоснование {k} [1].", "evidence": list(evidence), "fingerprint": fp[k]}
            for k, v in values.items()}


ALL = {"01": "Да", "02": "N/A", "03": "N/A", "04": "15", "05": "Да", "06": "7", "07": "Да", "08": "Нет"}
TR = [{"timestamp": f"00:0{i}", "speaker": "operator" if i % 2 else "client", "text": f"реплика {i}"} for i in range(1, 9)]


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "cqa.db")
    d.seed_default_checklist()
    d.save_dataset("CTRL", "test", [{"conversation_id": f"c{i}", "channel": "chat", "language": "RU", "transcript": TR}
                                    for i in range(1, 5)])
    return d


def add(db, cid, status="ok", provider="haiku", model=MODEL, ans=None, error=None):
    db.insert_evaluation({"conversation_id": cid, "checklist_id": 1, "provider": provider, "model": model,
                          "status": status, "error": error, "answers": ans or {}})


def test_export_takes_only_model_answers_by_official_checklist(db):
    changed = copy.deepcopy(CFG)
    changed["criteria"][4]["title"] = "Персонализация"  # замена критерия 05 — как на защите
    add(db, "c1", ans=answers(ALL, evidence=(4, 99, 4)))
    add(db, "c2", ans=answers(ALL))
    add(db, "c2", ans=answers(ALL, config=changed))  # новее, но по изменённому чек-листу
    add(db, "c3", status="error", error="Лимит запросов Claude (429), повторите позже.")
    add(db, "c4", provider="fake", model="fake", ans=answers(ALL, model="fake"))  # заглушка — не модель
    doc = rs.build(db, ["c1", "c2", "c3", "c4"], CFG, "Команда")
    assert rs.validate(doc, expected_ids=["c1", "c2", "c3", "c4"], expect_count=4, n_turns=rs.turn_counts(db, ["c1"])) == []
    r1, r2, r3, r4 = doc["results"]
    assert doc["model"] == {"mode": "external_api", "name": MODEL}
    assert [c["criterion_id"] for c in r1["criteria"]] == list(rs.SPEC)
    assert [c["value"] for c in r1["criteria"]] == [True, None, None, 15, True, 7, True, False]
    assert r1["criteria"][0]["evidence_turns"] == [4]  # 99 — нет такой реплики, повтор убран
    assert r1["score"] == {"final_score": 85.38, "critical_flag": False}
    assert r2["status"] == "ok"  # взята оценка по действующему чек-листу, а не более новая по изменённому
    assert r3 == {"conversation_id": "c3", "status": "error", "criteria": [], "score": None,
                  "error": "Лимит запросов Claude (429), повторите позже."}
    assert r4["status"] == "error" and r4["error"] == rs.NOT_EVALUATED
    assert json.loads(rs.dump(doc)) == doc


def test_model_modes(db):
    claude = {"provider": "haiku", "model": MODEL}
    local = {"provider": "local", "model": "qwen3.5-9b"}
    assert rs.model_block([claude], "haiku") == {"mode": "external_api", "name": MODEL}
    assert rs.model_block([local], "haiku") == {"mode": "local", "name": "qwen3.5-9b"}
    assert rs.model_block([claude, local], "haiku") == {"mode": "hybrid", "name": f"{MODEL}, qwen3.5-9b"}
    assert rs.model_block([], "haiku") == {"mode": "external_api", "name": MODEL}


def test_export_takes_only_requested_model(db):
    """Облако и своя модель в одной базе: в файл идут только ответы модели из --provider, без смешения."""
    own_model = "qwen3.5-9b-cqa"
    add(db, "c1", ans=answers(ALL))
    add(db, "c1", provider="local", model=own_model, ans=answers({**ALL, "01": "Нет"}, model=own_model))  # новее
    add(db, "c2", provider="local", model=own_model, ans=answers(ALL, model=own_model))
    cloud = rs.build(db, ["c1", "c2"], CFG, "Команда", provider="haiku")
    own = rs.build(db, ["c1", "c2"], CFG, "Команда", provider="local")
    assert cloud["model"] == {"mode": "external_api", "name": MODEL}
    assert cloud["results"][0]["criteria"][0]["value"] is True  # ответ Haiku, а не более новый ответ своей модели
    assert cloud["results"][1]["status"] == "error"  # Haiku этот диалог не оценивал
    assert own["model"] == {"mode": "local", "name": own_model}
    assert own["results"][0]["criteria"][0]["value"] is False
    assert [r["status"] for r in own["results"]] == ["ok", "ok"]


def test_cli_prod_end_to_end_from_file(tmp_path, monkeypatch):
    """Контрольный час одной командой: файл набора → оценка (заглушка) → results.json → проверка."""
    monkeypatch.setenv("CQA_ALLOW_FAKE", "1")
    src = tmp_path / "ctrl.json"
    src.write_text(json.dumps({"error": None, "isSuccess": True, "data": {
        "dataset_group_id": "CTRL-1", "total_records": 2,
        "records": [{"conversation_id": f"ctrl-{i}", "channel": "chat", "language": "RU", "transcript": TR,
                     "audio_url": None} for i in (1, 2)]}}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "results" / "prod" / "results.json"
    code = rs.main(["prod", "--file", str(src), "--provider", "fake", "--expect", "2", "--out", str(out),
                    "--db", str(tmp_path / "cqa.db"), "--team", "Команда"])
    doc = rs.load_strict(out)
    assert code == 0, doc
    assert [r["conversation_id"] for r in doc["results"]] == ["ctrl-1", "ctrl-2"]
    assert rs.validate(doc, expect_count=2) == []
    assert rs.main(["check", str(out), "--expect", "2", "--db", str(tmp_path / "cqa.db")]) == 0
    # повторный запуск не вызывает модель: оценки по действующему чек-листу уже есть
    db = DB(tmp_path / "cqa.db")
    n = len(db.list_runs())
    assert rs.main(["prod", "--dataset", "CTRL-1", "--provider", "fake", "--expect", "2", "--out", str(out),
                    "--db", str(tmp_path / "cqa.db"), "--team", "Команда"]) == 0
    assert len(db.list_runs()) == n
    # --fresh: весь набор оценивается заново, старые оценки в файл не попадают
    before = db.one("SELECT MAX(id) AS m FROM evaluations")["m"]
    assert rs.main(["prod", "--dataset", "CTRL-1", "--provider", "fake", "--expect", "2", "--out", str(out),
                    "--db", str(tmp_path / "cqa.db"), "--team", "Команда", "--fresh"]) == 0
    assert len(db.list_runs()) == n + 1
    assert all(rs.pick(db, c, rs.official_config(), True, before)[0] for c in ("ctrl-1", "ctrl-2"))
