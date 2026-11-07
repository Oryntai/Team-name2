"""Голосование: без сети, с разнообразными ответами и проверкой полного пути до API."""
import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from cqa import api, judge, runner
from cqa.results import record
from cqa.vote import evaluate_voted, medoid_index, merge_results
from test_runner_retry import CFG, OK, StubDB


CONFIG = {"criteria": [{"id": "a", "type": "bool"}, {"id": "b", "type": "bool"}]}
RECORD = StubDB().record("x")


def result(a="Да", b="Да", *, index=0, status="ok"):
    return {"status": status, "error": "429" if status == "error" else None,
            "provider": "fake", "model": "fake", "prompt_version": judge.PROMPT_VERSION,
            "pii_masked": [], "input_tokens": 10 + index, "output_tokens": 20 + index,
            "cost_usd": 0.01 * (index + 1), "latency_s": index + 1,
            "raw": f"raw-{index}", "answers": {
                cid: {"answer": value, "explanation": f"explanation-{index}", "evidence": [index + 1]}
                for cid, value in (("a", a), ("b", b))}}


def test_majority_uses_medoid_explanation_and_keeps_input_unchanged():
    calls = [result("Да", "Нет"), result(index=1), result("Нет", index=2)]
    before = copy.deepcopy(calls)
    assert medoid_index(calls) == 1
    merged = merge_results(calls, CONFIG)
    assert merged["answers"]["a"] == {
        "answer": "Да", "explanation": "explanation-1", "evidence": [2],
        "votes": ["Да", "Да", "Нет"], "agreement": "2/3"}
    assert merged["answers"]["b"]["answer"] == "Да"
    assert calls == before


@pytest.mark.parametrize("values,winner", [
    (["20", "0", "10"], "10"),
    (["20", "0", "10", "5"], "5"),
    (["20", "5"], "5"),
    (["0", "10", "20", "20", "10"], "10"),  # две моды: медоид, не медиана
    (["20", "N/A", "10"], "20"),  # N/A не имеет числового порядка
])
def test_scale_ties(values, winner):
    config = {"criteria": [{"id": "a", "type": "scale"}]}
    merged = merge_results([result(v, index=i) for i, v in enumerate(values)], config)
    a = merged["answers"]["a"]
    assert a["answer"] == winner
    assert a["agreement"] == f"{values.count(winner)}/{len(values)}"
    assert a["evidence"] == [values.index(winner) + 1]


def test_all_different_non_scale_uses_global_medoid():
    calls = [result("Да", "Нет"), result("Нет", index=1), result("N/A", index=2)]
    assert medoid_index(calls) == 1
    a = merge_results(calls, CONFIG)["answers"]["a"]
    assert a["answer"] == "Нет" and a["agreement"] == "1/3"
    assert a["explanation"] == "explanation-1" and a["evidence"] == [2]


def test_medoid_tie_and_answer_tie_choose_first():
    calls = [result("Нет", "Нет"), result(index=1)]
    assert medoid_index(calls) == 0
    assert merge_results(calls, CONFIG)["answers"]["a"]["answer"] == "Нет"


def test_explanation_from_first_winner_when_medoid_disagrees():
    calls = [result("Нет"), result(index=1), result("Да", "Нет", index=2)]
    # Остальные критерии делают первый вызов медоидом вопреки ответу по a.
    config = {"criteria": [{"id": cid, "type": "bool"} for cid in ("a", "b", "c", "d")]}
    for r, values in zip(calls, [("Да", "Да"), ("Да", "Нет"), ("Нет", "Да")]):
        for cid, value in zip(("c", "d"), values):
            r["answers"][cid] = {"answer": value}
    assert medoid_index(calls) == 0
    a = merge_results(calls, config)["answers"]["a"]
    assert a["answer"] == "Да" and a["explanation"] == "explanation-1" and a["evidence"] == [2]


def test_partial_failures_count_all_expenses_and_raw_but_only_successful_votes():
    calls = [result("Нет"), result(index=1, status="error"), result(index=2)]
    merged = merge_results(calls, CONFIG)
    assert merged["status"] == "ok" and merged["error"] is None
    assert merged["answers"]["a"]["votes"] == ["Нет", "Да"]
    assert merged["answers"]["a"]["agreement"] == "1/2"
    assert merged["input_tokens"] == 33 and merged["output_tokens"] == 63
    assert merged["cost_usd"] == pytest.approx(0.06)
    assert merged["latency_s"] == 3 and merged["model_calls"] == 3
    assert json.loads(merged["raw"]) == ["raw-0", "raw-1", "raw-2"]


def test_all_failed_returns_error_without_partial_answers():
    merged = merge_results([result(status="error"), result(index=1, status="error")], CONFIG)
    assert merged["status"] == "error" and "429" in merged["error"]
    assert merged["answers"] == {} and merged["model_calls"] == 2
    assert merged["input_tokens"] == 21 and merged["cost_usd"] == pytest.approx(0.03)


def test_evaluate_voted_parallel_semaphore_and_input_order(monkeypatch):
    async def execute():
        active = peak = count = 0
        two_started = asyncio.Event()
        finished = []

        async def evaluate(rec, config, provider, *, only):
            nonlocal active, peak, count
            i = count
            count += 1
            active += 1
            peak = max(peak, active)
            if i == 0:
                await two_started.wait()
            if i == 1:
                two_started.set()
            await asyncio.sleep(0)
            active -= 1
            finished.append(i)
            assert only == ["a"] and rec is RECORD
            return result("Нет" if i == 1 else "Да", index=i)

        monkeypatch.setattr(judge, "evaluate", evaluate)
        merged = await asyncio.wait_for(
            evaluate_voted(RECORD, CONFIG, judge.get_provider("fake"), 3,
                           sem=asyncio.Semaphore(2), only=["a"]), timeout=1)
        assert peak == 2 and count == 3 and finished[0] == 1
        assert merged["answers"]["a"]["votes"] == ["Да", "Нет", "Да"]
        assert json.loads(merged["raw"]) == ["raw-0", "raw-1", "raw-2"]

    asyncio.run(execute())


@pytest.mark.parametrize("success", [False, True])
def test_evaluate_voted_handles_raised_exceptions(monkeypatch, success):
    count = 0

    async def evaluate(*args):
        nonlocal count
        count += 1
        if success and count == 2:
            return result(index=1)
        raise RuntimeError("сломалось")

    monkeypatch.setattr(judge, "evaluate", evaluate)
    merged = asyncio.run(evaluate_voted(RECORD, CONFIG, judge.get_provider("fake"), 3))
    assert count == merged["model_calls"] == 3
    assert merged["status"] == ("ok" if success else "error")
    if success:
        assert merged["answers"]["a"]["agreement"] == "1/1"
    else:
        assert "RuntimeError: сломалось" in merged["error"] and merged["answers"] == {}
    assert len(json.loads(merged["raw"])) == 3


def run_one(monkeypatch, responses, *, votes=3, previous=None, mode="full", config=CFG):
    count = 0

    async def evaluate(rec, cfg, provider, *, precedents):
        assert precedents == []
        nonlocal count
        i = count
        count += 1
        return copy.deepcopy(responses[i % len(responses)])

    monkeypatch.setattr(judge, "evaluate", evaluate)
    monkeypatch.setattr(runner, "RETRY_PAUSES", (0, 0, 0))
    db = StubDB()
    asyncio.run(runner.Runner(db, votes=votes)._one(
        1, "x", config, 1, judge.get_provider("fake"), asyncio.Semaphore(2), mode, previous))
    assert db.evals[0]["model_calls"] == count
    return db.evals[0], count


def full_result(answer="Да", *, status="ok"):
    return {**copy.deepcopy(OK), **{k: v for k, v in result(status=status).items() if k != "answers"},
            "answers": {**copy.deepcopy(OK["answers"]), "01": {
                "answer": answer, "explanation": answer, "evidence": [1]}}}


def test_runner_three_votes_selects_majority(monkeypatch):
    ev, calls = run_one(monkeypatch, [full_result("Нет"), full_result(), full_result()])
    assert ev["status"] == "ok" and ev["total"] == 100 and calls == 3
    assert ev["answers"]["01"]["votes"] == ["Нет", "Да", "Да"]
    assert ev["answers"]["01"]["agreement"] == "2/3"
    assert ev["input_tokens"] == 30 and ev["output_tokens"] == 60
    assert ev["cost_usd"] == pytest.approx(0.03) and ev["latency_s"] == 1


def test_runner_partial_failure_does_not_retry(monkeypatch):
    ev, calls = run_one(monkeypatch, [full_result(status="error"), full_result(), full_result()])
    assert ev["status"] == "ok" and calls == 3
    assert ev["answers"]["01"]["agreement"] == "2/2"
    assert len(json.loads(ev["raw"])) == 3


@pytest.mark.parametrize("recover", [False, True])
def test_runner_retries_only_all_failed_batches_and_counts_every_call(monkeypatch, recover):
    responses = [full_result(status="error")] * 3
    if recover:
        responses += [full_result(), full_result("Нет"), full_result()]
    ev, calls = run_one(monkeypatch, responses)
    assert calls == (6 if recover else 12)
    assert ev["status"] == ("ok" if recover else "error")
    assert ev["input_tokens"] == calls * 10 and ev["output_tokens"] == calls * 20
    assert ev["cost_usd"] == pytest.approx(calls * 0.01)
    assert ev["latency_s"] == calls // 3 and len(json.loads(ev["raw"])) == calls


def test_runner_single_vote_keeps_original_response_and_one_call(monkeypatch):
    response = full_result()
    ev, calls = run_one(monkeypatch, [response], votes=1)
    assert calls == 1 and ev["status"] == "ok"
    assert ev["raw"] == response["raw"]
    for cid, answer in ev["answers"].items():
        assert "votes" not in answer and "agreement" not in answer
        assert answer["answer"] == response["answers"][cid]["answer"]


def test_voted_incremental_filters_new_answers_and_preserves_reused_votes(monkeypatch):
    old, _ = run_one(monkeypatch, [full_result(), full_result("Нет"), full_result()])
    old["id"] = 42
    config = copy.deepcopy(CFG)
    config["criteria"][4]["yes"] += " Новое определение."
    changed = full_result("Нет")
    changed["answers"]["05"]["answer"] = "Нет"
    ev, calls = run_one(monkeypatch, [changed], config=config, previous=old, mode="incremental")
    assert calls == 3 and ev["evaluated"] == ["05"] and len(ev["reused"]) == 7
    assert ev["answers"]["01"]["answer"] == "Да"  # новые полные ответы вне only отброшены
    assert ev["answers"]["05"]["votes"] == ["Нет"] * 3
    for cid in ev["reused"]:
        assert ev["answers"][cid] == {**old["answers"][cid], "reused_from": 42}
    ev["id"] = 43
    reused, calls = run_one(monkeypatch, [changed], config=config, previous=ev, mode="incremental")
    assert calls == 0 and reused["input_tokens"] == reused["cost_usd"] == 0
    assert reused["answers"]["05"]["agreement"] == "3/3"


def test_runner_default_votes_from_environment(monkeypatch):
    monkeypatch.delenv("CQA_VOTES", raising=False)
    assert runner.Runner(StubDB()).votes == 3
    monkeypatch.setenv("CQA_VOTES", "5")
    assert runner.Runner(StubDB()).votes == 5
    assert runner.Runner(StubDB(), votes=1).votes == 1


def test_results_record_does_not_export_voting_metadata():
    ev = merge_results([full_result(), full_result("Нет"), full_result()], CFG)
    exported = record("x", ev, None, CFG, n_turns=1)
    plain = copy.deepcopy(ev)
    for answer in plain["answers"].values():
        answer.pop("votes")
        answer.pop("agreement")
    assert exported == record("x", plain, None, CFG, n_turns=1)
    assert set(exported) == {"conversation_id", "status", "criteria", "score"}
    assert all(set(c) == {"criterion_id", "value", "rationale", "evidence_turns"}
               for c in exported["criteria"])


@pytest.mark.parametrize("votes", [None, 1, 5])
def test_api_voting_fields_round_trip_and_run_override(tmp_path, monkeypatch, votes):
    monkeypatch.setenv("CQA_VOTES", "3")
    monkeypatch.setattr(api, "read_env", lambda *args: None)
    app = api.create_app(tmp_path / "vote.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    app.state.db.save_dataset("TEST", "fake", [RECORD])
    count = 0

    async def evaluate(*args, precedents):
        assert precedents == []
        nonlocal count
        count += 1
        return full_result("Нет" if count == 1 else "Да")

    monkeypatch.setattr(judge, "evaluate", evaluate)
    body = {"dataset_id": "TEST", "provider": "fake", "mode": "full"}
    if votes is not None:
        body["votes"] = votes
    expected_calls = votes or 3
    with TestClient(app) as client:
        plan = client.post("/api/runs/plan", json=body).json()
        assert plan["model_calls"] == expected_calls
        response = client.post("/api/runs", json=body)
        assert response.status_code == 200
        rid = response.json()["run_id"]
        for _ in range(200):
            run = client.get(f"/api/runs/{rid}").json()
            if run["status"] != "running":
                break
            time.sleep(0.005)
        assert run["status"] == "done" and count == expected_calls
        ev_id = run["items"][0]["evaluation_id"]
        ev = client.get(f"/api/evaluations/{ev_id}").json()
        assert ev["model_calls"] == expected_calls
        original = ev["criteria"][0]["original"]
        stored = app.state.db.evaluation(ev_id)["answers"]["01"]
        conv = client.get("/api/conversations/x").json()
        assert conv["evaluations"][0]["answers"]["01"] == stored
        if expected_calls > 1:
            assert original["votes"] == ["Нет"] + ["Да"] * (expected_calls - 1)
            assert original["agreement"] == f"{expected_calls - 1}/{expected_calls}"
            assert original["votes"] == stored["votes"]
        else:
            assert "votes" not in original and "agreement" not in original
    app.state.db.close()


@pytest.mark.parametrize("votes", [0, 6, -1, 1.5, "3", True])
def test_api_rejects_invalid_votes(tmp_path, votes):
    app = api.create_app(tmp_path / "vote.db", allow_fake=True)
    with TestClient(app) as client:
        assert client.post("/api/runs", json={"dataset_id": "TEST", "votes": votes}).status_code == 422
        assert not app.state.db.list_runs()
    app.state.db.close()
