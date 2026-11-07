"""Автотест по ТЗ и PDF чек-листа: всё, что там написано явно. Пишется один раз; меняется, только если ошибочен сам тест.

Источники: CQA_Hackathon_Student_Guide_v3.pdf (стр. 1–4), CQA_Chat_Quality_Checklist.pdf (стр. 1–3).
"""
import asyncio
import copy
import json
import pathlib

import pytest

from cqa import checklist as ck
from cqa.judge import build_schema, build_system, build_user, evaluate, get_provider
from cqa.scoring import ScoreError, score

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = ck.load_default()
ALL_YES = {"01": "Да", "02": "Да", "03": "Да", "04": "20", "05": "Да", "06": "10", "07": "Да", "08": "Нет"}


# --- Чек-лист: начальная конфигурация = PDF, стр. 1–2 ---

def test_default_checklist_matches_pdf():
    c = {x["id"]: x for x in CFG["criteria"]}
    assert list(c) == ["01", "02", "03", "04", "05", "06", "07", "08"]
    assert [c[i]["title"] for i in c] == [
        "Установление контакта", "Выявление потребности", "Идентификация", "Корректная и полная консультация",
        "Эмпатия и клиентоориентированность", "Управление диалогом, скорость и грамотность", "Завершение",
        "Грубые ошибки"]
    assert {i: c[i]["weight"] for i in c} == {"01": 10, "02": 15, "03": 20, "04": 20, "05": 15, "06": 15, "07": 5, "08": 0}
    assert {i: c[i]["na"] for i in c} == {"01": False, "02": True, "03": True, "04": True, "05": False, "06": False,
                                          "07": True, "08": False}
    assert ck.allowed_answers(c["04"]) == ["20", "15", "10", "5", "0", "N/A"]
    assert ck.allowed_answers(c["06"]) == ["10", "7", "4", "0"]
    assert c["08"]["type"] == "flag"
    assert ck.sections(CFG) == ["Начало и понимание запроса", "Безопасность и решение", "Ведение и завершение диалога",
                                "Грубые нарушения"]
    assert sum(x["weight"] for x in CFG["criteria"]) == 100
    assert CFG["rounding"] == 2
    assert ck.validate(CFG) == []


# --- Итог: порядок расчёта, стр. 2 ---

def test_pdf_example_93_08():
    a = dict(ALL_YES, **{"02": "N/A", "03": "N/A", "06": "7"})
    assert score(CFG, a)["total"] == 93.08  # (10 + 20 + 15 + 10,5 + 5) / 65 × 100


def test_top_step_gives_full_weight():
    assert score(CFG, ALL_YES)["total"] == 100.0


def test_scale_points_proportional_to_weight():
    s = score(CFG, dict(ALL_YES, **{"04": "15", "06": "4"}))
    assert s["criteria"]["04"]["points"] == 15.0
    assert s["criteria"]["06"]["points"] == 6.0  # 15 × 4 / 10
    assert s["total"] == 86.0


def test_no_is_zero_yes_is_full_weight():
    s = score(CFG, dict(ALL_YES, **{"01": "Нет"}))
    assert s["criteria"]["01"]["points"] == 0.0 and s["total"] == 90.0


def test_na_only_where_allowed():
    for cid in ("01", "05", "06"):
        with pytest.raises(ScoreError):
            score(CFG, dict(ALL_YES, **{cid: "N/A"}))


def test_scale_only_listed_steps():
    for cid, bad in (("04", "12"), ("06", "5"), ("04", "Да")):
        with pytest.raises(ScoreError):
            score(CFG, dict(ALL_YES, **{cid: bad}))


def test_na_redistributes_weight_max_stays_100():
    s = score(CFG, dict(ALL_YES, **{"02": "N/A", "03": "N/A", "04": "N/A", "07": "N/A"}))
    assert s["total"] == 100.0
    assert abs(sum(r["weight_eff"] for r in s["criteria"].values()) - 100) < 0.05


def test_no_applicable_weighted_criteria_no_total():
    cfg = {"name": "t", "rounding": 2, "criteria": [
        {"id": "a", "title": "A", "type": "bool", "weight": 10, "na": True, "yes": "y", "na_when": "w"},
        {"id": "g", "title": "G", "type": "flag", "weight": 0, "na": False, "yes": "y"}]}
    assert score(cfg, {"a": "N/A", "g": "Нет"})["total"] is None


def test_gross_flag_separate_does_not_zero_total():
    s = score(CFG, dict(ALL_YES, **{"08": "Да"}))
    assert s["gross"] is True and s["gross_ids"] == ["08"] and s["total"] == 100.0
    assert score(CFG, ALL_YES)["gross"] is False


def test_rounding_two_digits_half_up():
    cfg = {"name": "t", "rounding": 2, "criteria": [
        {"id": "a", "title": "A", "type": "bool", "weight": 1, "na": False, "yes": "y"},
        {"id": "b", "title": "B", "type": "bool", "weight": 31, "na": False, "yes": "y"}]}
    assert score(cfg, {"a": "Да", "b": "Нет"})["total"] == 3.13  # 3,125 → 3,13 (не банковское 3,12)


def test_sections_shown():
    s = score(CFG, dict(ALL_YES, **{"02": "N/A", "03": "N/A", "06": "7"}))
    secs = {x["name"]: x for x in s["sections"]}
    assert secs["Начало и понимание запроса"]["percent"] == 100.0
    assert secs["Ведение и завершение диалога"]["percent"] == 87.14  # (15 + 10,5 + 5) / 35
    assert secs["Грубые нарушения"]["percent"] is None


# --- Чек-лист меняется через настройки, новый критерий применяется (стр. 1–2) ---

def test_replace_criterion_applies():
    cfg = copy.deepcopy(CFG)
    new = {"id": "05", "section": "Ведение и завершение диалога", "title": "Персонализация", "type": "scale",
           "weight": 10, "na": True, "yes": "оцените обращение по имени", "na_when": "имя клиента неизвестно",
           "steps": [{"value": 2, "text": "по имени"}, {"value": 1, "text": "иногда"}, {"value": 0, "text": "никогда"}]}
    cfg["criteria"][4] = new
    assert ck.validate(cfg) == []
    assert "Персонализация" in build_system(cfg) and "Эмпатия" not in build_system(cfg)
    enum = build_schema(cfg)["properties"]["criteria"]["properties"]["05"]["properties"]["answer"]["enum"]
    assert enum == ["2", "1", "0", "N/A"]
    assert score(cfg, dict(ALL_YES, **{"05": "1"}))["criteria"]["05"]["points"] == 5.0


def test_invalid_checklist_rejected():
    bad = copy.deepcopy(CFG)
    bad["criteria"][1]["na_when"] = ""            # N/A без условия
    bad["criteria"][3]["steps"] = [{"value": 5, "text": "x"}]  # шкала из одной ступени
    bad["criteria"][7]["weight"] = 5              # у грубой ошибки нет баллов
    bad["criteria"].append(dict(bad["criteria"][0]))  # повтор номера
    errs = ck.validate(bad)
    assert len(errs) >= 4


# --- Оценка: схема и промпт (стр. 2–3) ---

def test_schema_enum_matches_allowed_answers():
    props = build_schema(CFG)["properties"]["criteria"]["properties"]
    for c in CFG["criteria"]:
        assert props[c["id"]]["properties"]["answer"]["enum"] == ck.allowed_answers(c)
        assert ("N/A" in props[c["id"]]["properties"]["answer"]["enum"]) == c["na"]


def test_prompt_keeps_original_timestamps_and_order():
    rec = {"conversation_id": "x", "channel": "agent chat", "language": "KZ",
           "transcript": [{"timestamp": "00:05", "speaker": "client", "text": "Сәлеметсіз бе"},
                          {"timestamp": "00:05", "speaker": "bot", "text": "AIM"},
                          {"timestamp": "00:03", "speaker": "operator", "text": "Сәлем"}]}
    u = build_user(rec)
    assert "[1] 00:05 client: Сәлеметсіз бе" in u and "[3] 00:03 operator: Сәлем" in u
    assert u.index("[1]") < u.index("[2]") < u.index("[3]")
    assert "язык: KZ" in u


def test_bot_and_system_are_context_rule_in_prompt():
    s = build_system(CFG)
    assert "Бот и системные события — контекст" in s
    for c in CFG["criteria"]:
        assert f"## {c['id']}. {c['title']}" in s


def test_evaluate_with_fake_provider_gives_valid_answers_for_rus_kaz_eng():
    dev = ROOT / "data" / "dev.json"
    if not dev.exists():
        pytest.skip("нет data/dev.json")
    recs = json.loads(dev.read_text())["data"]["records"]
    langs = {}
    for r in recs:
        langs.setdefault(r["language"], r)
    assert set(langs) == {"RU", "KZ", "EN"}
    assert {t["speaker"] for r in recs for t in r["transcript"]} <= {"client", "operator", "bot", "system"}
    for r in langs.values():
        res = asyncio.run(evaluate(r, CFG, get_provider("fake")))
        assert res["status"] == "ok", res["error"]
        s = score(CFG, {k: v["answer"] for k, v in res["answers"].items()})
        assert s["total"] is not None
        assert all(v["evidence"] == [1] for v in res["answers"].values())
