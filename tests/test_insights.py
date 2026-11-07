"""Разбор с фейковым провайдером: строгий контракт, хранение и изоляция оценки."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cqa import api, insights, judge
from cqa.checklist import allowed_answers, load_default
from cqa.db import DB
from cqa.judge.providers import ProviderError, Reply

RECORD = {"conversation_id": "insight-1", "channel": "agent chat", "language": "RU", "transcript": [
    {"speaker": "client", "timestamp": "00:00", "text": "Не пришёл перевод. Почта client@example.com, ИИН 900101300007"},
    {"speaker": "bot", "timestamp": "00:01", "text": "Соединяю"},
    {"speaker": "system", "timestamp": "00:02", "text": "Оператор подключён"},
    {"speaker": "operator", "timestamp": "00:03", "text": "Я уточню и вернусь с ответом"},
    {"speaker": "client", "timestamp": "00:05", "text": "Жду"},
]}


def seed(db, cid=RECORD["conversation_id"], **overrides):
    config = load_default()
    answers = {c["id"]: {"answer": "Нет" if c["type"] == "flag" else allowed_answers(c)[0],
                          "explanation": "Пояснение [4].", "evidence": [4]} for c in config["criteria"]}
    for key, answer in {"01": "Нет", "04": "15", "06": "7", "08": "Да", **overrides}.items():
        answers[key]["answer"] = answer
    answers["01"]["explanation"] += " client@example.com 900101300007"
    return db.insert_evaluation({"conversation_id": cid, "checklist_id": 1, "provider": "fake", "model": "fake",
                                 "status": "ok", "answers": answers, "raw": "original raw", "total": 82.3,
                                 "gross": True, "cost_usd": .003})


class RecordingFake:
    pid = "fake"
    model = "fake-insights"

    def __init__(self):
        self.calls = []
        self.active = self.max_active = 0
        self.change = lambda data: data
        self.delay = 0
        self.failure = None

    async def complete(self, system, user, schema, **kwargs):
        self.calls.append((system, user, schema))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.failure:
                raise self.failure
            ids = schema["$defs"]["Coaching"]["properties"]["criterion_id"].get("enum", [])
            turn = schema["$defs"]["Coaching"]["properties"]["turn"].get("enum", [4])[0]
            data = {"summary": "Клиент уточнил статус перевода. Оператор обещал проверить, ответ ещё не получен.",
                    "topic": "Переводы", "reason": "Не пришёл перевод",
                    "sentiment": {"start": "negative", "end": "neutral"},
                    "flags": [{"code": "follow_up_promised", "turns": [4], "reason": "Оператор обещал вернуться"}],
                    "coaching": [{"criterion_id": cid, "turn": turn, "tip": "Уточните следующий шаг.",
                                  "rewrite": "Здравствуйте! Я уточню статус перевода."} for cid in ids]}
            data = self.change(data)
            return Reply(text=data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                         model=self.model, input_tokens=1000, output_tokens=300, cache_read=100, cache_write=50)
        finally:
            self.active -= 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    provider = RecordingFake()
    monkeypatch.setattr(judge, "get_provider", lambda pid: provider)
    monkeypatch.setattr(api, "read_env", lambda key, default=None: default)
    app = api.create_app(tmp_path / "i.db", web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    db = app.state.db
    db.save_dataset("TEST", "test", [RECORD])
    ev_id = seed(db)
    db.set_setting("default_provider", "fake")
    with TestClient(app) as client:
        yield client, db, provider, ev_id
    db.close()


def wait(client, cid=RECORD["conversation_id"]):
    for _ in range(200):
        row = client.get(f"/api/insights/{cid}").json()
        if row and row["data"]["status"] not in ("pending", "running"):
            return row
        time.sleep(.01)
    raise AssertionError("Разбор не завершён")


def protected_files():
    roots = [Path("results"), Path("cqa/judge"), Path("cqa/checklists")]
    files = [p for root in roots for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [Path("cqa") / name for name in ("scoring.py", "results.py", "vote.py", "precedents.py", "incremental.py")]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def test_run_persists_one_call_and_does_not_change_evaluations(setup, monkeypatch):
    client, db, provider, ev_id = setup
    # Тестовая цена как у Haiku, без сети и без изменения поставщиков судьи.
    monkeypatch.setitem(insights.providers.PROVIDERS["fake"], "price_in", .1)
    monkeypatch.setitem(insights.providers.PROVIDERS["fake"], "price_out", .5)
    tables = ("evaluations", "runs", "run_items", "appeals", "conversations", "checklists", "settings")
    before = {t: db.q(f"SELECT * FROM {t}") for t in tables}
    ev_before = client.get(f"/api/evaluations/{ev_id}").content
    files = protected_files()
    r = client.post("/api/insights/run", json={"conversation_ids": [RECORD["conversation_id"]]})
    assert r.status_code == 202, r.text
    row = wait(client)
    assert r.json()["insight_ids"] == [row["id"]]
    assert row["evaluation_id"] == ev_id and row["data"]["status"] == "ok"
    assert row["model"] == "fake-insights" and row["provider"] == "fake"
    assert row["input_tokens"] == 1150 and row["output_tokens"] == 300
    assert row["cost_usd"] == .000257 and row["created_at"]
    assert len(provider.calls) == 1
    assert {t: db.q(f"SELECT * FROM {t}") for t in tables} == before
    assert client.get(f"/api/evaluations/{ev_id}").content == ev_before
    assert protected_files() == files
    assert client.get("/api/insights?dataset_id=TEST").json() == [row]
    assert client.get("/api/insights?dataset_id=OTHER").json() == []
    stored = db.one("SELECT data FROM insights WHERE id=?", (row["id"],))
    assert json.loads(stored["data"]) == row["data"]


def test_prompt_masking_schema_and_original_turn_numbering(setup):
    client, db, provider, ev_id = setup
    client.post("/api/insights/run", json={"dataset_id": "TEST"})
    row = wait(client)
    system, user, schema = provider.calls[0]
    assert "client@example.com" not in user and "900101300007" not in user
    assert "[e-mail скрыт]" in user and "[ИИН скрыт]" in user
    assert "[2] 00:01 bot:" in user and "[3] 00:02 system:" in user and "[4] 00:03 operator:" in user
    assert '"answer": "Нет"' in user and '"evaluation_id": ' + str(ev_id) in user
    def check_supported(node):
        if isinstance(node, dict):
            assert not ({"minimum", "maximum", "minLength", "maxLength", "maxItems"} & set(node))
            if "minItems" in node:
                assert node["minItems"] in (0, 1)
            for child in node.values():
                check_supported(child)
        elif isinstance(node, list):
            for child in node:
                check_supported(child)
    check_supported(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "topic", "reason", "sentiment", "flags", "coaching"}
    assert schema["$defs"]["Flag"]["properties"]["code"]["enum"] == list(insights.FLAGS)
    assert schema["$defs"]["Flag"]["properties"]["turns"]["items"]["enum"] == [1, 2, 3, 4, 5]
    assert schema["$defs"]["Coaching"]["properties"]["turn"]["enum"] == [4]
    assert schema["$defs"]["Coaching"]["properties"]["criterion_id"]["enum"] == ["01", "04", "06", "08"]
    assert {c["criterion_id"] for c in row["data"]["coaching"]} == {"01", "04", "06", "08"}


@pytest.mark.parametrize("change", [
    lambda d: "not json",
    lambda d: {**d, "extra": True},
    lambda d: {k: v for k, v in d.items() if k != "summary"},
    lambda d: {**d, "summary": ""},
    lambda d: {**d, "sentiment": {"start": "angry", "end": "neutral"}},
    lambda d: {**d, "flags": [{**d["flags"][0], "code": "invented"}]},
    lambda d: {**d, "flags": d["flags"] * 2},
    lambda d: {**d, "flags": [{**d["flags"][0], "turns": [0]}]},
    lambda d: {**d, "flags": [{**d["flags"][0], "turns": [6]}]},
    lambda d: {**d, "flags": [{**d["flags"][0], "turns": [True]}]},
    lambda d: {**d, "flags": [{**d["flags"][0], "turns": ["4"]}]},
    lambda d: {**d, "flags": [{**d["flags"][0], "turns": [4, 4]}]},
    lambda d: {**d, "coaching": []},
    lambda d: {**d, "coaching": d["coaching"] + [d["coaching"][0]]},
    lambda d: {**d, "coaching": [{**c, "turn": 2} for c in d["coaching"]]},
    lambda d: {**d, "coaching": [{**c, "criterion_id": "02"} for c in d["coaching"]]},
])
def test_invalid_response_keeps_usage_and_never_repairs_meaning(setup, change, monkeypatch):
    client, db, provider, _ = setup
    provider.change = change
    monkeypatch.setitem(insights.providers.PROVIDERS["fake"], "price_in", .1)
    before = db.q("SELECT * FROM evaluations")
    client.post("/api/insights/run", json={"dataset_id": "TEST"})
    row = wait(client)
    assert row["data"]["status"] == "error" and "summary" not in row["data"]
    assert row["input_tokens"] == 1150 and row["cost_usd"] > 0
    assert len(provider.calls) == 1 and db.q("SELECT * FROM evaluations") == before


def test_provider_error_is_saved_and_can_be_retried(setup):
    client, db, provider, _ = setup
    provider.failure = ProviderError("sensitive transport details")
    client.post("/api/insights/run", json={"dataset_id": "TEST"})
    failed = wait(client)
    assert failed["data"]["status"] == "error" and failed["cost_usd"] == 0
    assert "sensitive" not in json.dumps(failed)
    provider.failure = None
    client.post("/api/insights/run", json={"dataset_id": "TEST"})
    success = wait(client)
    assert success["data"]["status"] == "ok" and success["id"] > failed["id"]
    assert len(db.q("SELECT * FROM insights")) == 2 and len(provider.calls) == 2
    assert client.get("/api/insights").json() == [success]


def test_latest_success_is_pinned_and_appeals_do_not_change_input(setup):
    client, db, provider, old_id = setup
    new_id = seed(db, **{"01": "Да", "04": "20", "06": "N/A", "08": "Нет"})
    db.insert_evaluation({"conversation_id": RECORD["conversation_id"], "checklist_id": 1, "status": "error"})
    ap = db.create_appeal(new_id, "01", "test")
    db.decide_appeal(ap, "changed", "Нет", "test")
    client.post("/api/insights/run", json={"dataset_id": "TEST"})
    row = wait(client)
    assert row["evaluation_id"] == new_id and new_id != old_id
    assert row["data"]["coaching"] == []


@pytest.mark.parametrize("body", [{}, {"conversation_ids": []}, {"dataset_id": "missing"},
                                  {"conversation_ids": ["missing"]}, {"conversation_ids": [" "]},
                                  {"conversation_ids": [RECORD["conversation_id"]], "dataset_id": "OTHER"}])
def test_invalid_selection_is_atomic(setup, body):
    client, db, provider, _ = setup
    r = client.post("/api/insights/run", json=body)
    assert r.status_code == 422 and r.json()["errors"]
    assert db.q("SELECT * FROM insights") == [] and provider.calls == []


def test_requires_evaluation_and_fake_is_not_public(setup, tmp_path):
    client, db, provider, _ = setup
    record = {**RECORD, "conversation_id": "unrated"}
    db.save_dataset("EMPTY", "test", [record])
    assert client.get("/api/insights/unrated").json() is None
    assert client.get("/api/insights/missing").status_code == 404
    assert client.post("/api/insights/run", json={"conversation_ids": [RECORD["conversation_id"], "unrated"]}).status_code == 422
    assert db.q("SELECT * FROM insights") == []
    other = api.create_app(db.path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=False)
    with TestClient(other) as c:
        assert c.post("/api/insights/run", json={"dataset_id": "TEST"}).status_code == 422
    other.state.db.close()
    assert provider.calls == []


def test_overlapping_batches_share_limit_and_deduplicate_pending(tmp_path):
    async def scenario():
        db = DB(tmp_path / "batch.db")
        db.seed_default_checklist()
        records = [{**RECORD, "conversation_id": str(n)} for n in range(5)]
        db.save_dataset("TEST", "test", records)
        provider = RecordingFake()
        provider.delay = .03
        runner = insights.InsightsRunner(db, concurrency=2)
        snapshots = [(db.record(r["conversation_id"]), db.evaluation(seed(db, r["conversation_id"])), load_default()) for r in records]
        first = runner.start(snapshots[:3], provider)
        second = runner.start(snapshots[2:], provider)
        assert first[-1] == second[0]
        await runner.wait()
        assert len(provider.calls) == 5 and provider.max_active == 2
        assert all(i["data"]["status"] == "ok" for i in db.list_insights())
        db.close()
    asyncio.run(scenario())


def test_no_operator_and_na_need_no_coaching(setup):
    client, db, provider, ev_id = setup
    rec = db.record(RECORD["conversation_id"])
    rec["transcript"] = rec["transcript"][:3]
    ev = db.evaluation(ev_id)
    _, schema, ids, turns = insights.request(rec, ev, load_default())
    assert ids == turns == [] and schema["properties"]["coaching"]["minItems"] == 0
    assert insights.coaching_ids(load_default(), {"04": {"answer": "N/A"}, "08": {"answer": "Нет"}}) == []


def test_existing_database_migration_and_interrupted_work(tmp_path):
    path = tmp_path / "old.db"
    db = DB(path)
    db.seed_default_checklist()
    db.save_dataset("TEST", "test", [RECORD])
    ev_id = seed(db)
    db.ex("DROP TABLE insights")
    before = db.q("SELECT * FROM evaluations")
    db.close()
    db = DB(path)
    assert db.q("SELECT * FROM insights") == [] and db.q("SELECT * FROM evaluations") == before
    iid = db.insert_insight(RECORD["conversation_id"], ev_id, "fake", "fake")
    db.close()
    app = api.create_app(path, web_dir=tmp_path, data_dir=tmp_path, allow_fake=True)
    with TestClient(app) as c:
        row = c.get(f"/api/insights/{RECORD['conversation_id']}").json()
        assert row["id"] == iid and row["data"]["status"] == "error"
        assert app.state.db.q("SELECT * FROM evaluations") == before
    app.state.db.close()


def test_cancellation_finishes_pending_rows(tmp_path):
    async def scenario():
        db = DB(tmp_path / "cancel.db")
        db.seed_default_checklist()
        db.save_dataset("TEST", "test", [RECORD])
        ev_id = seed(db)
        runner = insights.InsightsRunner(db, concurrency=1)
        provider = RecordingFake()
        provider.delay = 10
        runner.start([(db.record(RECORD["conversation_id"]), db.evaluation(ev_id), load_default())], provider)
        await asyncio.sleep(0)
        for task in runner.tasks:
            task.cancel()
        await runner.wait()
        assert db.latest_insight(RECORD["conversation_id"])["data"]["status"] == "error"
        db.close()
    asyncio.run(scenario())


def test_api_pending_deduplication_and_snapshot(setup):
    client, db, provider, ev_id = setup
    provider.delay = .1
    body = {"conversation_ids": [RECORD["conversation_id"], RECORD["conversation_id"]]}
    first = client.post("/api/insights/run", json=body).json()
    second = client.post("/api/insights/run", json=body).json()
    assert first == second and len(first["insight_ids"]) == 1
    seed(db, **{"01": "Да"})  # новая оценка во время вызова не меняет его вход
    row = wait(client)
    assert row["evaluation_id"] == ev_id and len(provider.calls) == 1


def test_unavailable_provider_does_not_create_work(setup, monkeypatch):
    client, db, provider, _ = setup
    monkeypatch.setattr(judge, "available", lambda pid: False)
    r = client.post("/api/insights/run", json={"dataset_id": "TEST"})
    assert r.status_code == 422 and r.json()["errors"]
    assert db.list_insights() == [] and provider.calls == []


def test_web_insight_rendering_and_flag_filter():
    """Проверка методов реальных компонентов без браузера и сторонних JS-зависимостей."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Для проверки веб-компонентов нужен node")
    script = r'''
import assert from 'node:assert/strict';
const registry = {};
globalThis.HTMLElement = class { getAttribute() { return null; } };
globalThis.customElements = { define(name, cls) { registry[name] = cls; } };
globalThis.location = { search: '' };
await import('./web/js/components/card.js');
await import('./web/js/components/conversation-list.js');
const card = new registry['cqa-card']();
card.s = { evals: [], ev: null, insight: null };
assert.match(card.insightHtml(), /Разобрать диалог/);
assert.match(card.insightHtml(), /disabled/);
const row = { evaluation_id: 7, cost_usd: .001, model: 'fake', input_tokens: 100, output_tokens: 20,
  data: { status: 'ok', summary: '<img onerror=alert(1)>', topic: 'Переводы', reason: 'Задержка',
    sentiment: { start: 'negative', end: 'neutral' },
    flags: [{ code: 'unresolved', turns: [1, 4], reason: 'Ответ не получен' }],
    coaching: [{ criterion_id: '01', turn: 4, tip: '<совет>', rewrite: '<переписано>' }] } };
card.s = { evals: [{ evaluation_id: 7, status: 'ok' }], ev: { id: 7 }, insight: row, insightOpen: false };
let html = card.insightHtml();
assert.ok(!html.includes('<img'));
assert.match(html, /&lt;img/);
assert.match(html, /data-ref="1"/);
assert.match(html, /data-ref="4"/);
assert.match(html, /Негативное → Нейтральное/);
assert.match(html, /aria-expanded="false"/);
assert.match(html, /id="insight-detail" inert/);
assert.match(card.coachingHtml('01'), /Как можно было:.*&lt;переписано&gt;/);
assert.match(card.coachingHtml('01'), /data-ref="4"/);
assert.equal(card.coachingHtml('02'), '');
card.s.ev.id = 6;
assert.equal(card.coachingHtml('01'), '');
assert.match(card.insightHtml(), /По оценке №7/);
assert.ok(!card.insightHtml().includes('>Обновить<'));
card.s.evals.unshift({ evaluation_id: 8, status: 'ok' });
assert.match(card.insightHtml(), />Обновить</);
const list = new registry['cqa-conversation-list']();
list.s = { rows: [{ id: 'a', language: 'RU' }, { id: 'b', language: 'KZ' }],
  lang: 'all', channel: 'all', q: '', flag: 'unresolved', sort: { key: 'id', dir: 1 },
  insights: new Map([['a', row.data]]), selected: new Set(), runItems: new Map() };
assert.deepEqual(list.filtered().map(r => r.id), ['a']);
list.s.flag = 'complaint';
assert.equal(list.filtered().length, 0);
list.s.flag = 'all';
assert.equal(list.filtered().length, 2);
assert.match(list.rowHtml(list.s.rows[0]), /cqa-insight-topic">Переводы/);
assert.ok(!list.rowHtml(list.s.rows[1]).includes('cqa-insight-topic'));
const toolbar = { innerHTML: '' };
list.part = () => toolbar;
Object.assign(list.s, { datasets: [], checklists: [], meta: { providers: [] } });
list.renderToolbar();
assert.match(toolbar.innerHTML, /class="cqa-select cqa-select--sm" data-f="flag"/);
assert.ok(!toolbar.innerHTML.includes('Разобрать все'));
list.s.insights.clear();
list.renderToolbar();
assert.ok(!toolbar.innerHTML.includes('data-f="flag"'));
'''
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
