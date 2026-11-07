// Node's built-in test runner; no browser, packages or model calls required.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

class Element {
  constructor() { this.events = new Map(); this.attrs = new Map(); this.dataset = {}; this.innerHTML = ''; this.style = {}; this.children = []; }
  addEventListener(type, fn) { this.events.set(type, fn); }
  removeEventListener(type) { this.events.delete(type); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  append(...elements) { this.children.push(...elements); }
  remove() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const registry = new Map();
globalThis.HTMLElement = Element;
globalThis.customElements = { define: (name, cls) => registry.set(name, cls) };
globalThis.MutationObserver = class { observe() {} };
globalThis.document = Object.assign(new Element(), { documentElement: new Element(), body: new Element(), createElement: () => new Element() });
globalThis.window = { scrollY: 0, scrollTo() {}, confirm: () => false, dispatchEvent() {} };
globalThis.history = { replaceState() {} };
globalThis.CSS = { escape: (value) => value };
globalThis.matchMedia = () => ({ matches: true });
await import('../../web/js/components/checklist-editor.js');
await import('../../web/js/components/journal.js');
const { clone, fmtMoney, fmtSeconds } = await import('../../web/js/util.js');
const config = JSON.parse(await readFile(new URL('../../cqa/checklists/default.json', import.meta.url)));

function editor() {
  const el = new (registry.get('cqa-checklist-editor'))();
  Object.defineProperty(el, 'api', { value: {} });
  el._connected = true;
  el.s = { loaded: { id: 1, version: 1, config: clone(config) }, draft: clone(config), parent: null,
    list: [{ id: 1, version: 1, name: config.name, note: '<заметка>', created_at: '2026-11-01' }], selId: 1,
    open: new Set(), folds: new Set(), assists: new Map(), trial: {}, approved: {}, approvedLimit: 12,
    datasets: [{ id: 'DEV', total: 65 }], meta: { providers: [{ id: 'fake', available: true }] },
    applyDataset: 'DEV', applyProvider: 'fake', plan: { total: 65, model_calls: 0, criteria: [] },
    validation: { ok: true, errors: [] }, note: '' };
  el.validateSoon = () => {};
  return el;
}
function action(html, name) {
  const tag = html.match(new RegExp(`<button[^>]*data-act="${name}"[^>]*>`));
  assert.ok(tag, `Missing action ${name}`);
  return tag[0];
}
function disclosure(id, act = 'toggle') {
  const button = new Element();
  const classes = new Set();
  const body = new Element();
  body.classList = { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } };
  body.inert = true;
  const card = { classList: body.classList };
  button.nextElementSibling = body;
  button.dataset = { act, id, fold: id };
  button.setAttribute('aria-expanded', 'false');
  button.closest = (sel) => sel === '[data-act]' ? button : sel === '.cqa-ec' ? card : null;
  return button;
}

test('header actions gate saving and applying the draft, with compact type labels and draft dots', () => {
  const el = editor();
  assert.match(action(el.savebarHtml(), 'save'), / disabled/);
  assert.doesNotMatch(action(el.savebarHtml(), 'apply'), / disabled/);
  const c = el.s.draft.criteria[0];
  assert.doesNotMatch(el.tagsHtml(c), /cqa-draft-dot is-visible/);
  c.title = '<Новый критерий>';
  assert.match(el.tagsHtml(c), /cqa-draft-dot is-visible/);
  assert.doesNotMatch(action(el.savebarHtml(), 'save'), / disabled/);
  assert.match(action(el.savebarHtml(), 'apply'), / disabled/);
  assert.match(el.critHtml(c, 0), /&lt;Новый критерий&gt;/);
  el.s.validation = { ok: false };
  assert.match(action(el.savebarHtml(), 'save'), / disabled/);
  const scale = el.s.draft.criteria.find((c) => c.type === 'scale');
  assert.match(el.tagsHtml(scale), /шкала 0–20/);
  const flag = el.s.draft.criteria.find((c) => c.type === 'flag');
  assert.match(el.tagsHtml(flag), /грубая ошибка/);
  assert.match(el.versionsHtml(), /data-f="version"/);
  assert.match(el.versionsHtml(), /&lt;заметка&gt;/);
});

test('opening criteria keeps exactly one editable body active and closes it on a second click', () => {
  const el = editor();
  const a = disclosure('01'), b = disclosure('02');
  el.querySelectorAll = () => [a, b];
  el.onClick({ target: a });
  assert.equal(a.getAttribute('aria-expanded'), 'true');
  assert.equal(a.nextElementSibling.inert, false);
  el.onClick({ target: b });
  assert.equal(a.getAttribute('aria-expanded'), 'false');
  assert.equal(a.nextElementSibling.inert, true);
  assert.equal(b.nextElementSibling.inert, false);
  assert.deepEqual([...el.s.open], ['02']);
  el.onClick({ target: b });
  assert.equal(b.nextElementSibling.inert, true);
  assert.equal(el.s.open.size, 0);
});

test('rules and guidance start collapsed, retain their state, and AI insertion reveals its destination', () => {
  const el = editor(), c = el.s.draft.criteria[0];
  assert.match(el.rulesHtml(), /data-fold="rules" aria-expanded="false"/);
  assert.match(el.critHtml(c, 0), /data-fold="guidance-01" aria-expanded="false"/);
  const rules = disclosure('rules', 'fold');
  el.onClick({ target: rules });
  assert.match(el.rulesHtml(), /data-fold="rules" aria-expanded="true"/);
  assert.match(el.rulesHtml(), /data-path="r.0"/);
  el.s.open.add(c.id);
  el.s.assists.set(c.id, { result: { guidance: 'Пояснение ИИ', na_when_suggestion: 'Условие ИИ' } });
  const insert = disclosure(c.id, 'assist-guidance'); insert.dataset.i = '0';
  el.onClick({ target: insert });
  assert.equal(c.guidance, 'Пояснение ИИ');
  assert.match(el.critHtml(c, 0), /data-fold="guidance-01" aria-expanded="true"/);
  assert.equal(el.s.folds.has('rules'), true);
});

test('adding, reordering, deleting, and changing types keep criteria editable', () => {
  const el = editor();
  el.s.open.add('01');
  el.addCriterion();
  const id = [...el.s.open][0];
  assert.equal(el.s.open.size, 1);
  assert.notEqual(id, '01');
  let i = el.s.draft.criteria.findIndex((c) => c.id === id);
  el.setType(i, 'scale');
  assert.ok(el.s.draft.criteria[i].steps.length);
  el.setType(i, 'flag');
  assert.equal(el.s.draft.criteria[i].weight, 0);
  assert.equal(el.s.draft.criteria[i].na, false);
  const up = disclosure(id, 'up'); up.dataset.i = String(i);
  el.onClick({ target: up });
  assert.equal(el.s.draft.criteria[i - 1].id, id);
  const del = disclosure(id, 'del'); del.dataset.i = String(i - 1);
  el.onClick({ target: del });
  assert.equal(el.s.draft.criteria.some((c) => c.id === id), false);
});

test('version loads collapse criteria even if multiple criteria differ from the parent', async () => {
  const el = editor();
  const loaded = { ...el.s.loaded, id: 2, version: 2, parent_id: 1, config: clone(config) };
  loaded.config.criteria[0].title += ' изменено'; loaded.config.criteria[1].title += ' изменено';
  el.api.checklist = async (id) => id === 2 ? loaded : el.s.loaded;
  el.loadPlan = () => {};
  await el.open(2);
  assert.equal(el.s.open.size, 0);
  assert.match(el.innerHTML, /data-part="versions"/);
  assert.match(el.innerHTML, /data-fold="rules" aria-expanded="false"/);
  assert.doesNotMatch(el.tagsHtml(el.s.draft.criteria[0]), /cqa-draft-dot is-visible/);
});

test('trial and approved checks submit the actual unsaved draft and keep returned evidence and cost', async () => {
  const el = editor();
  el.s.draft.criteria[0].yes = 'Новое определение';
  el.api.tryChecklist = async (body) => {
    assert.equal(body.config.criteria[0].yes, 'Новое определение');
    assert.equal(body.provider, 'fake');
    return { items: [{ id: 'test', criteria: [{ id: '01', title: 'Тест', previous_answer: 'Нет', answer: 'Да', explanation: 'Причина', evidence: [2] }] }], cost_usd: 0.25, latency_s: 1, model: 'fake' };
  };
  await el.tryChecklist();
  assert.match(el.tryHtml(), /Нет → <b>Да<\/b>/);
  assert.match(el.tryHtml(), /#2/);
  assert.match(el.tryHtml(), /\$0,250/);
  el.api.checkApproved = async (body) => {
    assert.equal(body.config.criteria[0].yes, 'Новое определение');
    assert.equal(body.limit, 12);
    return { model: 'fake', criteria: [], cost_usd: 0.125, latency_s: 1, message: 'Сверено' };
  };
  await el.checkApproved();
  assert.match(el.approvedHtml(), /Сверено/);
  assert.match(el.approvedHtml(), /\$0,125/);
  el.s.draft.criteria[0].yes += ' ещё';
  assert.match(el.tryHtml(), /изменились/);
  assert.match(el.approvedHtml(), /изменились/);
});

test('saving preserves the API payload, creates a version, and enables apply after the plan loads', async () => {
  const el = editor(), original = el.s.loaded;
  el.s.draft.criteria[0].weight += 1;
  el.s.note = 'Правка веса';
  const expected = clone(el.s.draft);
  let saved;
  el.api.saveChecklist = async (body) => { saved = body; return { id: 2, version: 2 }; };
  el.api.checklists = async () => [{ id: 2, version: 2, name: config.name }, ...el.s.list];
  el.api.checklist = async (id) => id === 2 ? { id: 2, version: 2, parent_id: 1, config: expected } : original;
  el.api.planRun = async (body) => {
    assert.deepEqual(body, { dataset_id: 'DEV', checklist_id: 2, provider: 'fake', mode: 'incremental' });
    return { total: 65, model_calls: 0, criteria: [] };
  };
  await el.save();
  assert.deepEqual(saved, { config: expected, note: 'Правка веса', parent_id: 1 });
  assert.equal(el.s.loaded.id, 2);
  assert.equal(el.isDirty(), false);
  assert.equal(el.s.justSaved, 2);
  assert.doesNotMatch(action(el.savebarHtml(), 'apply'), / disabled/);
  assert.doesNotMatch(el.tagsHtml(el.s.draft.criteria[0]), /cqa-draft-dot is-visible/);
});

test('applying uses selected dataset/model and retains progress and the result diff', async () => {
  const el = editor();
  let request;
  el.api.startRun = async (body) => { request = body; return { run_id: 7 }; };
  el.api.run = async () => ({ id: 7, status: 'done', done: 65, total: 65, cost_usd: 0, checklist_id: 1 });
  el.api.runDiff = async () => ({ criteria: [], items: [] });
  el.api.planRun = async () => ({ total: 65, model_calls: 0, criteria: [] });
  await el.apply();
  // Poll and diff resolve asynchronously using fake API responses only.
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(request, { dataset_id: 'DEV', checklist_id: 1, provider: 'fake', mode: 'incremental' });
  assert.equal(el.s.run.id, 7);
  assert.equal(el.s.run.status, 'done');
  assert.match(el.applyHtml(), /Оценено <b>65<\/b>/);
  assert.match(el.applyHtml(), /Что изменилось/);
  assert.match(el.applyHtml(), /Открыть результаты/);
  el.stopPoll?.();
});

test('loading a plan updates the disabled main action', async () => {
  const el = editor();
  el.s.plan = null;
  assert.match(action(el.savebarHtml(), 'apply'), / disabled/);
  el.api.planRun = async () => ({ total: 65, model_calls: 0, criteria: [] });
  await el.loadPlan();
  assert.doesNotMatch(action(el.savebarHtml(), 'apply'), / disabled/);
});

test('journal combines costs and weighted times and keeps disclosures open across refreshes', () => {
  const el = new (registry.get('cqa-journal'))();
  el.s = { runs: [{ id: 1, done: 2, cost_usd: 1, avg_latency_s: 2, status: 'done' },
    { id: 2, done: 1, cost_usd: 2, avg_latency_s: 8, status: 'done' }],
    asks: [{ id: 1, question: '<Вопрос>', status: 'error', cost_usd: 4, error: 'Ошибка' }],
    calls: [{ conversation_id: 'a/b', criteria: ['01'], cost_usd: 8, status: 'ok' }], meta: null, folds: new Set() };
  el.render();
  assert.match(el.innerHTML, new RegExp(`Потрачено всего <b>\\${fmtMoney(15)}</b>`));
  assert.ok(el.innerHTML.includes(`Одна оценка <b>${fmtMoney(1)}</b>`));
  assert.ok(el.innerHTML.includes(`Время <b>${fmtSeconds(4)}</b>`));
  assert.match(el.innerHTML, /data-log-fold="asks" aria-expanded="false"/);
  assert.match(el.innerHTML, /data-log-fold="calls" aria-expanded="false"/);
  assert.match(el.innerHTML, /&lt;Вопрос&gt;/);
  assert.match(el.innerHTML, /#\/c\/a%2Fb/);
  el.s.folds.add('asks'); el.s.folds.add('ask-1'); el.s.folds.add('calls');
  el.render();
  assert.match(el.innerHTML, /data-log-fold="asks" aria-expanded="true"/);
  assert.match(el.innerHTML, /data-log-fold="ask-1" aria-expanded="true"/);
  assert.match(el.innerHTML, /data-log-fold="calls" aria-expanded="true"/);
});

test('empty journal retains both collapsed sections with zero totals', () => {
  const el = new (registry.get('cqa-journal'))();
  el.s = { runs: [], asks: [], calls: [], meta: null, folds: new Set() };
  el.render();
  assert.match(el.innerHTML, /Вопросы ИИ · 0/);
  assert.match(el.innerHTML, /Сверки с утверждёнными · 0/);
  assert.match(el.innerHTML, /Одна оценка <b>—<\/b>/);
});
