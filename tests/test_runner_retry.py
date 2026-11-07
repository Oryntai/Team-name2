"""Повторные попытки оценки: сбой модели (например, лимит запросов 429) не валит диалог, если следующая попытка удалась."""
import asyncio

from cqa import checklist as ck
from cqa import runner as rn
from cqa.judge import get_provider

CFG = ck.load_default()
ALL_YES = {"01": "Да", "02": "Да", "03": "Да", "04": "20", "05": "Да", "06": "10", "07": "Да", "08": "Нет"}
OK = {"status": "ok", "error": None, "cost_usd": 0.001, "provider": "fake", "model": "fake",
      "answers": {k: {"answer": v, "explanation": "", "evidence": [1]} for k, v in ALL_YES.items()}}
FAIL = {"status": "error", "error": "Лимит запросов Claude (429), повторите позже.", "answers": {}, "cost_usd": 0.0}


class StubDB:
    def __init__(self):
        self.items, self.evals, self.bumps = [], [], []

    def record(self, cid):
        return {"conversation_id": cid, "channel": "chat", "language": "RU",
                "transcript": [{"timestamp": "00:01", "speaker": "operator", "text": "Здравствуйте"}]}

    def set_item(self, run_id, cid, status, ev_id=None, error=None):
        self.items.append((cid, status, error))

    def bump_run(self, run_id, ok, cost):
        self.bumps.append(ok)

    def insert_evaluation(self, ev):
        self.evals.append(ev)
        return len(self.evals)


def run_one(monkeypatch, answers):
    calls = []

    async def fake_evaluate(rec, config, provider):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(rn.judge, "evaluate", fake_evaluate)
    monkeypatch.setattr(rn, "RETRY_PAUSES", (0, 0, 0))
    db = StubDB()
    asyncio.run(rn.Runner(db, votes=1)._one(1, "x", CFG, 1, get_provider("fake"), asyncio.Semaphore(1)))
    return db, calls


def test_rate_limit_then_success_gives_ok_evaluation(monkeypatch):
    db, calls = run_one(monkeypatch, [FAIL, OK])
    assert len(calls) == 2
    assert db.evals[0]["status"] == "ok" and db.evals[0]["total"] == 100.0
    assert db.items[-1][1] == "ok" and db.bumps == [True]


def test_all_attempts_fail_saves_error(monkeypatch):
    db, calls = run_one(monkeypatch, [FAIL])
    assert len(calls) == len(rn.RETRY_PAUSES) + 1
    assert db.evals[0]["status"] == "error" and "429" in db.evals[0]["error"]
    assert db.items[-1][1] == "error" and db.bumps == [False]
