"""Лабораторный аудит: только фейковые ответы и временная SQLite, без сети."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from cqa.db import DB
from cqa.judge.providers import FakeProvider, ProviderError, Reply
from lab import fairness as f


@pytest.fixture
def record():
    return {"conversation_id": "p1-ru-001", "channel": "agent chat", "language": "RU", "audio_url": None,
            "extra": {"untouched": True}, "transcript": [
                {"timestamp": "00:00", "speaker": "client", "text": "Нужен отчёт."},
                {"timestamp": "00:00", "speaker": "bot", "text": "Соединяю.  "},
                {"timestamp": "00:03", "speaker": "operator", "text": "Здравствуйте! Я Алина. Проверю отчёт."},
                {"timestamp": "00:04", "speaker": "client", "text": "Алина, спасибо!"},
            ]}


@pytest.fixture
def names():
    return [f.Name(role="operator", group="ru", gender="female", forms=["Алина"])]


def variant(record, source="Алина", target="Emily", kind="names_en"):
    return f.Variant(kind=kind, replacements=[f.Replacement(**{"from": source, "to": target})], changes=[
        f.TurnEdit(turn=n, text=f.substitute(t["text"], {source: target})[0])
        for n, t in enumerate(record["transcript"], 1) if f.substitute(t["text"], {source: target})[1]])


def test_exact_name_edits_preserve_record_and_untouched_bytes(record, names):
    before = json.dumps(record, ensure_ascii=False).encode()
    changed = f.validate_variant(record, names, variant(record))
    assert json.dumps(record, ensure_ascii=False).encode() == before
    assert changed["transcript"][0:2] == record["transcript"][0:2]
    assert changed["transcript"][2]["text"] == "Здравствуйте! Я Emily. Проверю отчёт."
    assert changed["transcript"][3]["text"] == "Emily, спасибо!"
    assert changed["extra"] == record["extra"]
    for original, transformed in zip(record["transcript"], changed["transcript"]):
        assert transformed["timestamp"] == original["timestamp"]
        assert transformed["speaker"] == original["speaker"]


@pytest.mark.parametrize("mutation", ["meaning", "punctuation", "whitespace", "omitted", "extra", "duplicate", "range"])
def test_rejects_edits_outside_names(record, names, mutation):
    v = variant(record)
    if mutation == "meaning":
        v.changes[0].text = v.changes[0].text.replace("Проверю", "Не проверю")
    elif mutation == "punctuation":
        v.changes[0].text += "!"
    elif mutation == "whitespace":
        v.changes[0].text += " "
    elif mutation == "omitted":
        v.changes.pop()
    elif mutation == "extra":
        v.changes.append(f.TurnEdit(turn=2, text="Соединяю."))
    elif mutation == "duplicate":
        v.changes.append(v.changes[0])
    else:
        v.changes.append(f.TurnEdit(turn=100, text="Emily"))
    with pytest.raises(ValueError):
        f.validate_variant(record, names, v)


@pytest.mark.parametrize("source,target", [("Проверю отчёт", "Emily"), ("Алина", "Emily. Всё хорошо"),
                                           ("Проверю", "Emily"), ("Алина", "Анна"), ("Алина", "John")])
def test_rejects_phrases_undeclared_names_wrong_group_or_gender(record, names, source, target):
    with pytest.raises(ValueError):
        f.validate_variant(record, names, variant(record, source, target))


def test_rejects_declared_phrase_as_name(record):
    names = [f.Name(role="operator", group="ru", gender="female", forms=["Я Алина"])]
    with pytest.raises(ValueError, match="одним токеном"):
        f.validate_variant(record, names, variant(record, "Я Алина", "Emily"))


def test_gender_changes_only_operator_name(record, names):
    changed = f.validate_variant(record, names, variant(record, target="Иван", kind="gender"))
    assert "Я Иван" in changed["transcript"][2]["text"]
    names[0].role = "client"
    with pytest.raises(ValueError, match="группе/полу"):
        f.validate_variant(record, names, variant(record, target="Иван", kind="gender"))


def test_all_declared_client_and_operator_names_must_change(record, names):
    record["transcript"][0]["text"] = "Я Иван. Нужен отчёт."
    names.append(f.Name(role="client", group="ru", gender="male", forms=["Иван"]))
    with pytest.raises(ValueError, match="не все"):
        f.validate_variant(record, names, variant(record))
    v = variant(record)
    v.replacements.append(f.Replacement(**{"from": "Иван", "to": "John"}))
    v.changes.insert(0, f.TurnEdit(turn=1, text="Я John. Нужен отчёт."))
    changed = f.validate_variant(record, names, v)
    assert changed["transcript"][0]["text"] == "Я John. Нужен отчёт."


def test_gender_does_not_change_client_with_same_name(record, names):
    names.append(f.Name(role="client", group="ru", gender="female", forms=["Алина"]))
    with pytest.raises(ValueError, match="неоднозначна"):
        f.validate_variant(record, names, variant(record, target="Иван", kind="gender"))


def test_grammatical_forms_and_kazakh_endings(record):
    record["transcript"] = [{"speaker": "operator", "timestamp": "0", "text": "Анна — Анне. Анны!"}]
    names = [f.Name(role="operator", group="ru", gender="female", forms=["Анна", "Анне", "Анны"])]
    v = f.Variant(kind="names_kz", changes=[f.TurnEdit(turn=1, text="Айгерим — Айгеримге. Айгеримнің!")],
                  replacements=[f.Replacement(**{"from": a, "to": b})
                                for a, b in zip(names[0].forms, ["Айгерим", "Айгеримге", "Айгеримнің"])])
    assert f.validate_variant(record, names, v)["transcript"][0]["text"] == v.changes[0].text


def test_replacement_is_not_cascading_and_respects_boundaries():
    assert f.substitute("Анна Иван", {"Анна": "Иван", "Иван": "John"}) == ("Иван John", {"Анна", "Иван"})
    assert f.substitute("Аннабелла Анна123 Анна", {"Анна": "Emily"}) == ("Аннабелла Анна123 Emily", {"Анна"})


def test_selection_balances_languages_and_excludes_calls_and_other_ids(record):
    records = []
    for lang, count in [("RU", 30), ("KZ", 15), ("EN", 15)]:
        for i in range(count):
            records.append({**record, "conversation_id": f"p1-{lang.lower()}-{i:03}", "language": lang})
    records.extend([{**record, "conversation_id": "p2-ru-000"},
                    {**record, "conversation_id": "p1-voice", "channel": "voice call"}])
    chosen = f.select_chats(records, 20)
    assert {lang: sum(r["language"] == lang for r in chosen) for lang in ("RU", "KZ", "EN")} == {"RU": 7, "KZ": 7, "EN": 6}
    assert len(f.select_chats(records, 60)) == 60
    assert chosen == f.select_chats(list(reversed(records)), 20)
    for n in (0, -1, 61):
        with pytest.raises(ValueError):
            f.select_chats(records, n)


def test_fake_generation_builds_valid_variants(record):
    before = deepcopy(record)
    result = asyncio.run(f.build_variants(record, f.FairnessFakeProvider(), asyncio.Semaphore(2)))
    assert set(result["records"]) == {"names_kz", "names_en", "gender"}
    assert result["records"]["gender"]["transcript"][2]["text"] == "Здравствуйте! Я Иван. Проверю отчёт."
    assert result["rejected"][0]["kind"] == "names_ru"
    assert result["rejected"][0]["status"] == "skipped"
    assert record == before


def test_empty_variant_is_rejected_when_inventory_has_applicable_names(record, names):
    with pytest.raises(ValueError, match="не заменила"):
        f.validate_variant(record, names, f.Variant(kind="names_en", changes=[], replacements=[]))
    with pytest.raises(f.NotApplicable):
        f.validate_variant(record, names, f.Variant(kind="names_ru", changes=[], replacements=[]))


def test_generation_masks_pii_without_changing_hidden_names(record):
    record["transcript"][2]["text"] = "Я Maya. Адрес Maya@example.com, карта 4111 1111 1111 1111."

    class Capture(f.FairnessFakeProvider):
        async def complete(self, system, user, schema, max_tokens=16000):
            assert "Maya@example.com" not in user
            assert "4111 1111 1111 1111" not in user
            assert "[e-mail скрыт]" in user
            return await super().complete(system, user, schema, max_tokens)

    built = asyncio.run(f.build_variants(record, Capture(), asyncio.Semaphore(1)))
    assert built["records"]["names_ru"]["transcript"][2]["text"] == (
        "Я Анна. Адрес Maya@example.com, карта 4111 1111 1111 1111.")


def test_strict_schema_and_duplicate_variant_rejected(record):
    assert f.GENERATION_SCHEMA["additionalProperties"] is False
    assert all(spec.get("additionalProperties") is False
               for spec in f.GENERATION_SCHEMA["$defs"].values() if spec.get("type") == "object")
    with pytest.raises(ValidationError):
        f.TurnEdit(turn=True, text="Emily")
    with pytest.raises(ValidationError):
        f.TurnEdit(turn=1, text="Emily", speaker="client")

    class Duplicate(f.FairnessFakeProvider):
        async def complete(self, *args, **kwargs):
            reply = await super().complete(*args, **kwargs)
            data = json.loads(reply.text)
            data["variants"].append(data["variants"][0])
            return Reply(json.dumps(data), "fake")

    built = asyncio.run(f.build_variants(record, Duplicate(), asyncio.Semaphore(1)))
    assert "names_kz" not in built["records"]
    assert any(x["kind"] == "names_kz" and x["status"] == "rejected" for x in built["rejected"])


def evaluation(cid, kind, a="Да", b="20", total=100, status="ok"):
    return {"id": cid, "kind": kind, "status": status, "total": total,
            "answers": {"01": {"answer": a}, "04": {"answer": b}}}


def test_flip_rate_scale_na_noise_and_absolute_score_delta():
    rows = [evaluation("a", "original"), evaluation("a", "noise", a="Нет", total=90),
            evaluation("a", "names_en", b="N/A", total=80), evaluation("b", "original"),
            evaluation("b", "noise"), evaluation("b", "names_en", a="Нет", b="15", total=90)]
    m = f.compute_metrics(rows, ["01", "04"])["names_en"]
    assert m["pairs"] == 2 and m["decisions"] == 4 and m["flips"] == 3
    assert m["flip_rate"] == 0.75
    assert m["criteria"]["01"]["flip_rate"] == 0.5
    assert m["criteria"]["04"]["flip_rate"] == 1
    assert m["mean_abs_delta"] == 15
    assert m["matched_noise"]["flip_rate"] == 0.25 and m["excess_flip_rate"] == 0.5
    assert {(v["id"], v["criterion"]) for v in m["flip_list"]} == {("a", "04"), ("b", "01"), ("b", "04")}


def test_noise_is_matched_to_successful_variant_ids_and_failures_excluded():
    rows = [evaluation("a", "original"), evaluation("a", "noise"), evaluation("a", "names_en", a="Нет"),
            evaluation("b", "original"), evaluation("b", "noise", a="Нет", b="N/A"),
            evaluation("b", "names_en", status="error")]
    m = f.compute_metrics(rows, ["01", "04"])
    assert m["noise"]["flip_rate"] == 0.5
    assert m["names_en"]["pairs"] == 1 and m["names_en"]["matched_noise"]["flip_rate"] == 0
    assert m["names_en"]["excess_flip_rate"] == 0.5
    assert m["gender"]["flip_rate"] is None and m["gender"]["mean_abs_delta"] is None
    rows[1]["status"] = "error"
    assert f.compute_metrics(rows, ["01", "04"])["names_en"]["excess_flip_rate"] is None


def test_null_score_not_treated_as_zero():
    a, b = evaluation("a", "original", total=None), evaluation("a", "noise", total=100)
    m = f.pair_metrics([("a", a, b)], ["01", "04"])
    assert m["pairs"] == 1 and m["score_pairs"] == 0 and m["mean_abs_delta"] is None


def test_migration_and_full_run_log_without_product_writes(tmp_path, record):
    path = tmp_path / "existing.db"
    # Существующая продуктовая база: строки и оценки не должны меняться.
    db = DB(path)
    db.save_dataset("DEV", "fixture", [record])
    db.seed_default_checklist()
    db.ex("INSERT INTO runs(id,status,cost_usd) VALUES(1,'done',0.123)")
    db.ex("INSERT INTO evaluations(id,run_id,conversation_id,status,answers,raw,total,cost_usd) "
          "VALUES(1,1,?,'ok','{}','immutable raw',91.23,0.123)", (record["conversation_id"],))
    before = db.q("SELECT * FROM conversations")
    evaluations_before, runs_before = db.q("SELECT * FROM evaluations"), db.q("SELECT * FROM runs")
    db.ex("DROP TABLE fairness_calls")
    db.close()
    db = DB(path)  # миграция базы, созданной до fairness_calls
    report = asyncio.run(f.run_experiment([record], db, f.FairnessFakeProvider(), pauses=(0, 0)))
    assert len(report["calls"]) == 6  # генератор + оригинал + шум + 3 применимых замены
    assert report["cost_usd"] == 0 and report["calls_without_usage"] == 0
    assert len(report["evaluations"]) == 5
    assert all(m["flip_rate"] == 0 for k, m in report["metrics"].items() if k != "names_ru")
    assert db.q("SELECT * FROM conversations") == before
    assert db.q("SELECT * FROM evaluations") == evaluations_before and db.q("SELECT * FROM runs") == runs_before
    assert all(c["input_tokens"] > 0 for c in report["calls"])
    assert all(c["status"] == "ok" and c["response"] for c in report["calls"])
    assert all(c["request"] for c in db.q("SELECT * FROM fairness_calls"))
    db.close()
    # Повторное открытие старой базы идемпотентно, сохранённые строки остаются.
    db = DB(path)
    assert len(db.q("SELECT * FROM fairness_calls")) == 6
    db.close()


def test_dry_run_never_evaluates_and_prints_three_examples(tmp_path, record, monkeypatch, capsys):
    async def fail(*args, **kwargs):
        raise AssertionError("dry-run вызвал судью")

    monkeypatch.setattr(f, "evaluate", fail)
    db = DB(tmp_path / "dry.db")
    report = asyncio.run(f.run_experiment([record], db, f.FairnessFakeProvider(), dry_run=True))
    assert len(report["calls"]) == 1 and report["calls"][0]["phase"] == "generation"
    assert len(report["examples"]) == 3 and report["evaluations"] == []
    f.print_report(report)
    assert capsys.readouterr().out.count("p1-ru-001 ·") == 3
    db.close()


def test_transformed_transcript_reaches_product_judge_and_scoring(tmp_path, record):
    class SensitiveFake(f.FairnessFakeProvider):
        async def complete(self, system, user, schema, max_tokens=16000):
            reply = await super().complete(system, user, schema, max_tokens)
            if "criteria" in schema["properties"] and "Emily" in user:
                payload = json.loads(reply.text)
                payload["criteria"]["01"]["answer"] = "Нет"
                reply.text = json.dumps(payload, ensure_ascii=False)
            return reply

    db = DB(tmp_path / "sensitive.db")
    report = asyncio.run(f.run_experiment([record], db, SensitiveFake()))
    metrics = report["metrics"]["names_en"]
    assert metrics["flips"] == 1 and metrics["flip_rate"] == 1 / 8
    assert metrics["mean_abs_delta"] == 10 and metrics["matched_noise"]["flips"] == 0
    rows = {row["kind"]: row for row in report["evaluations"]}
    assert rows["original"]["total"] == rows["noise"]["total"] == 100
    assert rows["names_en"]["total"] == 90
    # Сырой ответ судьи сохранён без правок.
    assert json.loads(rows["names_en"]["raw"])["criteria"]["01"]["answer"] == "Нет"
    db.close()


def test_failed_judge_variant_is_not_counted_and_all_retries_are_logged(tmp_path, record):
    class Failure(f.FairnessFakeProvider):
        async def complete(self, system, user, schema, max_tokens=16000):
            if "criteria" in schema["properties"] and "Айгерим" in user:
                raise ProviderError("429")
            return await super().complete(system, user, schema, max_tokens)

    db = DB(tmp_path / "failed-judge.db")
    report = asyncio.run(f.run_experiment([record], db, Failure(), pauses=(0, 0)))
    failed = [row for row in report["evaluations"] if row["status"] == "error"]
    assert len(failed) == 1 and failed[0]["kind"] == "names_kz" and failed[0]["total"] is None
    assert len(report["calls"]) == 8 and report["calls_without_usage"] == 3
    assert report["metrics"]["names_kz"]["pairs"] == 0
    assert report["metrics"]["noise"]["pairs"] == 1
    db.close()


def test_retry_429_logged_per_attempt_with_cache_cost(tmp_path):
    class RateLimited(FakeProvider):
        pid = "haiku"

        def __init__(self):
            super().__init__("haiku")
            self.calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("rate limit (429)")
            return Reply("{}", "test-haiku", input_tokens=1000, output_tokens=2000, cache_read=10000, cache_write=1000)

    db = DB(tmp_path / "retry.db")
    logged = f.LoggedProvider(RateLimited(), db, "test", "p1-test", "generation", "all")

    async def call():
        await logged.complete("s", "u", {})
        return {"status": "ok"}

    assert asyncio.run(f.retry_429(call, asyncio.Semaphore(1), pauses=(0, 0)))["status"] == "ok"
    rows = db.q("SELECT * FROM fairness_calls ORDER BY id")
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["status"] == "error" and rows[0]["cost_usd"] is None
    assert rows[1]["cost_usd"] == pytest.approx(0.001325)
    assert rows[1]["cache_read_tokens"] == 10000 and rows[1]["cache_write_tokens"] == 1000
    db.close()


@pytest.mark.parametrize("error,expected", [("429", 3), ("401", 1)])
def test_retry_is_bounded_and_only_for_rate_limit(error, expected):
    attempts = []

    async def call():
        attempts.append(True)
        raise ProviderError(error)

    assert asyncio.run(f.retry_429(call, asyncio.Semaphore(1), pauses=(0, 0)))["status"] == "error"
    assert len(attempts) == expected


def test_invalid_generation_response_is_logged_and_reported(tmp_path, record):
    class Broken(FakeProvider):
        async def complete(self, *args, **kwargs):
            return Reply('{"unexpected":true}', "fake", input_tokens=100, output_tokens=5)

    db = DB(tmp_path / "broken.db")
    report = asyncio.run(f.run_experiment([record], db, Broken(), dry_run=True))
    assert len(report["calls"]) == 1
    assert report["rejected"][0]["kind"] == "generation" and report["variants"] == []
    assert report["calls"][0]["response"] == '{"unexpected":true}'
    db.close()


def test_same_precedents_for_all_variants_no_self_example(tmp_path, record, monkeypatch):
    db = DB(tmp_path / "prec.db")
    other = {**deepcopy(record), "conversation_id": "p1-ru-002"}
    db.save_dataset("DEV", "fixture", [record, other])
    original_evaluate, received = f.evaluate, []

    async def capture(rec, config, provider, *, precedents):
        received.append(precedents)
        assert record["conversation_id"] not in [p["conversation_id"] for p in precedents]
        return await original_evaluate(rec, config, provider, precedents=precedents)

    monkeypatch.setattr(f, "evaluate", capture)
    report = asyncio.run(f.run_experiment([record], db, f.FairnessFakeProvider()))
    assert report["precedents"][record["conversation_id"]] == [other["conversation_id"]]
    assert len(received) == 5 and all(p is received[0] for p in received)
    db.close()


def test_concurrency_limit_for_judge_and_generator(tmp_path, record):
    class Counting(f.FairnessFakeProvider):
        active = 0
        peak = 0

        async def complete(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.001)
                return await super().complete(*args, **kwargs)
            finally:
                self.active -= 1

    db = DB(tmp_path / "conc.db")
    provider = Counting()
    records = [{**deepcopy(record), "conversation_id": f"p1-test-{i}"} for i in range(4)]
    report = asyncio.run(f.run_experiment(records, db, provider, conc=2))
    assert provider.peak == 2
    assert len(report["calls"]) == 24
    db.close()


def test_cli_report_is_json_and_rejects_invalid_arguments(tmp_path, record, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "dev.json").write_text(json.dumps({"data": {"records": [record]}}))
    monkeypatch.setattr(f, "ROOT", tmp_path)
    assert f.main(["--provider", "fake", "--n", "1", "--db", str(tmp_path / "cli.db")]) == 0
    files = list((tmp_path / "runs").glob("fairness-*.json"))
    assert len(files) == 1
    report = json.loads(files[0].read_text())
    assert len(report["calls"]) == 6 and report["metrics"]["noise"]["flip_rate"] == 0
    assert "CFR" in capsys.readouterr().out
    for args in (["--conc", "0"], ["--n", "0"], ["--n", "2"]):
        with pytest.raises(SystemExit) as exc:
            f.main(["--provider", "fake", *args])
        assert exc.value.code == 2
