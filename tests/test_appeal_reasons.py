"""Причины апелляций, совместимость старой базы и отображение серверных баллов."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from cqa import api as api_module
from cqa.api import create_app
from cqa.db import DB
from test_api import run_and_wait, upload


def test_migrate_old_appeals_preserves_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE appeals(id INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id INTEGER, "
                     "criterion_id TEXT, comment TEXT, created_at TEXT, status TEXT, new_answer TEXT, "
                     "reason TEXT, decided_at TEXT)")
        conn.execute("INSERT INTO appeals VALUES(1, 7, '05', 'Комментарий', '2026-11-01', "
                     "'changed', 'Нет', 'Причина', '2026-11-02')")
        before = conn.execute("SELECT * FROM appeals").fetchone()
    for _ in range(2):
        db = DB(path)
        try:
            row = db.appeal(1)
            assert row["reason_code"] == "other"
            assert tuple(v for k, v in row.items() if k != "reason_code") == before
            assert [r["name"] for r in db.q("PRAGMA table_info(appeals)")].count("reason_code") == 1
        finally:
            db.close()


@pytest.fixture
def evaluated(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "read_env", lambda key, default=None: None)
    app = create_app(tmp_path / "appeals.db", web_dir=tmp_path / "web", allow_fake=True)
    with TestClient(app) as client:
        upload(client)
        run = run_and_wait(client, {"dataset_id": "TEST", "provider": "fake"})
        ev_id = run["items"][0]["evaluation_id"]
        yield client, app.state.db, ev_id


@pytest.mark.parametrize("reason_code", ["wrong_interpretation", "wrong_evidence", "wrong_na", "other", None])
def test_reason_saved_listed_and_returned_in_card(evaluated, reason_code):
    client, db, ev_id = evaluated
    before = db.q("SELECT * FROM evaluations ORDER BY id")
    body = {"evaluation_id": ev_id, "criterion_id": "05", "comment": "Проверьте реплику"}
    if reason_code is not None:
        body["reason_code"] = reason_code
    response = client.post("/api/appeals", json=body)
    assert response.status_code == 201, response.text
    appeal = response.json()
    expected = reason_code or "other"
    assert appeal["reason_code"] == expected
    assert db.appeal(appeal["id"])["reason_code"] == expected
    assert client.get("/api/appeals?status=open").json()[0]["reason_code"] == expected
    detail = client.get(f"/api/evaluations/{ev_id}").json()
    assert next(c for c in detail["criteria"] if c["id"] == "05")["appeal"]["reason_code"] == expected
    assert db.q("SELECT * FROM evaluations ORDER BY id") == before


@pytest.mark.parametrize("reason_code", ["unknown", "", "Wrong_na", None, 1, ["other"]])
def test_invalid_reason_is_422_without_writes(evaluated, reason_code):
    client, db, ev_id = evaluated
    response = client.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": "05",
                                               "comment": "Комментарий", "reason_code": reason_code})
    assert response.status_code == 422
    assert db.appeals_for(ev_id) == []


def test_decision_totals_survive_reload_and_later_decisions(evaluated):
    client, db, ev_id = evaluated
    before = db.q("SELECT * FROM evaluations ORDER BY id")

    def appeal(criterion):
        r = client.post("/api/appeals", json={"evaluation_id": ev_id, "criterion_id": criterion,
                                            "comment": "Проверьте", "reason_code": "wrong_evidence"})
        assert r.status_code == 201
        return r.json()["id"]

    def decide(aid, decision, answer=None):
        r = client.post(f"/api/appeals/{aid}/decision", json={"decision": decision, "new_answer": answer,
                                                           "reason": "Проверено по репликам"})
        assert r.status_code == 200, r.text
        return r.json()["appeal"]

    first = appeal("05")
    changed = decide(first, "change", "Нет")
    assert (changed["total_before_decision"], changed["total_after_decision"]) == (100, 85)
    second = appeal("05")
    confirmed = decide(second, "confirm")
    assert (confirmed["total_before_decision"], confirmed["total_after_decision"]) == (85, 85)
    third = appeal("01")
    decide(third, "change", "Нет")
    items = {a["id"]: a for a in client.get("/api/appeals?status=decided").json()}
    assert (items[first]["total_before_decision"], items[first]["total_after_decision"]) == (100, 85)
    assert (items[second]["total_before_decision"], items[second]["total_after_decision"]) == (85, 85)
    assert db.q("SELECT * FROM evaluations ORDER BY id") == before


def test_web_reasons_decisions_and_score_display(evaluated):
    node = shutil.which("node")
    if not node:
        pytest.skip("Для проверки компонентов нужен node")
    client, _, ev_id = evaluated
    ev = client.get(f"/api/evaluations/{ev_id}").json()
    script = r'''
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const registry = {};
globalThis.HTMLElement = class {};
globalThis.customElements = { define: (name, cls) => { registry[name] = cls; } };
globalThis.localStorage = { getItem: () => 'reviewer' };
globalThis.window = { __cqaRole: 'reviewer' };
await import('./web/js/components/card.js');
await import('./web/js/components/appeals.js');
const { fmtScore, fmtPts } = await import('./web/js/util.js');
const ev = JSON.parse(readFileSync(0, 'utf8'));
const card = new registry['cqa-card']();
card.s = { ev, appealFor: '05' };
const crit = ev.criteria.find(c => c.id === '05');
let html = card.appealHtml({ ...crit, appeal: null }, crit);
for (const code of ['wrong_interpretation', 'wrong_evidence', 'wrong_na', 'other']) {
  assert(html.includes(`data-appeal-reason="${code}"`));
}
// Несогласованные контрольные числа доказывают: UI выводит значения API, не рассчитывает итог.
ev.final.total = 12.34;
crit.final_answer = 'N/A';
crit.score_final = { applicable: false, weight_eff: 0, points_eff: 0 };
ev.criteria[0].score_final = { applicable: true, weight_eff: 17.89, points_eff: 11.23 };
html = card.scoreDialogHtml();
assert(html.includes(fmtScore(12.34)));
assert(html.includes(fmtPts(17.89)));
assert(html.includes(fmtPts(11.23)));
assert(html.includes('исключён'));
assert(html.includes('без баллов'));
assert(html.includes('N/A исключены, веса применимых пересчитаны до 100'));
ev.final.total = null;
assert(card.scoreDialogHtml().includes('нет применимых критериев с весом'));
const appeals = new registry['cqa-appeals']();
appeals.s = { result: null, list: [], decision: 'change' };
const appeal = { status: 'open', current_answer: 'Нет', reason_code: 'wrong_evidence' };
html = appeals.decisionHtml({ appeal }, { ...crit, allowed: ['Да', 'Нет'], original: { answer: 'Да' } });
assert(html.includes('name="new_answer" value="Да"'));
assert(!html.includes('name="new_answer" value="Нет"'));
html = appeals.decisionHtml({ appeal: { ...appeal, status: 'confirmed', total_before_decision: 85, total_after_decision: 85 } }, crit);
assert(html.includes(`Итог: ${fmtScore(85)} → ${fmtScore(85)}`));
assert(!appeals.fragmentHtml({ transcript: [] }, []).includes('undefined'));
// Выбор сегмента и отправка формы должны передавать именно выбранный код.
const buttons = ['other', 'wrong_na'].map(code => ({ dataset: { appealReason: code }, setAttribute(k, v) { this[k] = v; } }));
const form = { elements: { reason_code: { value: 'other' } }, querySelectorAll: () => buttons };
buttons[1].closest = () => form;
card.onClick({ target: { closest: () => buttons[1] } });
assert.equal(form.elements.reason_code.value, 'wrong_na');
assert.equal(buttons[1]['aria-pressed'], 'true');
assert.equal(buttons[0]['aria-pressed'], 'false');
globalThis.FormData = class { get(name) { return { comment: 'Проверьте N/A', reason_code: form.elements.reason_code.value }[name]; } };
globalThis.document = { querySelector: () => ({ append() {} }), createElement: () => ({}) };
globalThis.CustomEvent = class {};
window.dispatchEvent = () => {};
globalThis.setTimeout = () => {}; // таймер уведомления не нужен в проверке отправки
let sent;
Object.defineProperty(card, 'api', { value: { createAppeal: async body => { sent = body; } } });
card.reloadEval = async () => {};
await card.onSubmit({ preventDefault() {}, target: { dataset: { form: 'appeal', crit: '05' }, querySelector: () => ({}) } });
assert.deepEqual(sent, { evaluation_id: ev.id, criterion_id: '05', comment: 'Проверьте N/A', reason_code: 'wrong_na' });
// Пустой фильтр не оставляет справа апелляцию из другого фильтра.
appeals.s = { list: [{ id: 1, status: 'open' }], sel: 1, detail: { appeal: { id: 1 } } };
appeals.render = () => {};
appeals.onClick({ target: { closest: () => ({ dataset: { tab: 'decided' } }) } });
assert.equal(appeals.s.tab, 'decided');
assert.equal(appeals.s.sel, null);
assert.equal(appeals.s.detail, null);
'''
    result = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(ev),
                            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
