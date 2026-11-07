import assert from 'node:assert/strict';
import { test } from 'node:test';

// Minimal browser surface for importing the real web components without dependencies.
const registry = new Map();
class Element {
  attributes = new Map();
  dataset = {};
  innerHTML = '';
  classList = {
    values: new Set(),
    contains(value) { return this.values.has(value); },
    toggle(value, on) { on ? this.values.add(value) : this.values.delete(value); },
  };
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  hasAttribute(name) { return this.attributes.has(name); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener() {}
  removeEventListener() {}
  querySelectorAll() { return []; }
}
globalThis.HTMLElement = Element;
globalThis.customElements = { define: (name, cls) => registry.set(name, cls) };
globalThis.document = {
  documentElement: new Element(), addEventListener() {}, querySelectorAll: () => [],
  createElement: name => new (registry.get(name) || Element)(),
};
globalThis.MutationObserver = class { observe() {} };
globalThis.location = { search: '', hash: '' };
globalThis.window = { scrollTo() {} };
await import('../../web/js/app.js');

function analytics(api = {}) {
  const el = new (registry.get('cqa-analytics'))();
  el._connected = true;
  el.s = { dataset: 'DEV', checklist: '1', datasets: [], checklists: [] };
  el.folds = new Map([['analytics-queue', true], ['analytics-accuracy', true]]);
  el.seq = 1;
  el.data = { summary: { evaluated: 3 } };
  const parts = Object.fromEntries(['report', 'export', 'insights-action'].map(p => [p, new Element()]));
  el.part = p => parts[p];
  Object.defineProperty(el, 'api', { value: api });
  return el;
}
const insight = (id, status) => ({ id, conversation_id: `c${id}`, data: { status } });

test('only queue and accuracy are open initially; closing persists', () => {
  const el = analytics();
  for (const name of ['queue', 'accuracy', 'slices', 'appeals', 'topics', 'histogram']) {
    const open = ['queue', 'accuracy'].includes(name);
    const html = el.fold(`analytics-${name}`, name, 'итог', 'подробности');
    assert.ok(html.includes(`aria-expanded="${open}"`));
    assert.ok(html.includes(`aria-hidden="${!open}"`));
  }
  el.folds.set('analytics-queue', false);
  assert.match(el.fold('analytics-queue', '', '', ''), /aria-expanded="false"/);
});

test('criterion accordion closes previous row and makes hidden content inert', () => {
  const el = analytics();
  const buttons = [0, 1].map(i => {
    const b = new Element();
    b.dataset.fold = `analytics-criterion-${i}`;
    b.classList.values.add('cqa-analytics-criterion');
    b.setAttribute('aria-expanded', 'false');
    return b;
  });
  const panels = buttons.map(() => Object.assign(new Element(), { firstElementChild: { inert: true } }));
  el.querySelector = selector => panels[buttons.findIndex(b => `#${b.dataset.fold}` === selector)];
  el.querySelectorAll = () => buttons.filter(b => b.getAttribute('aria-expanded') === 'true');
  el.toggleFold(buttons[0]);
  assert.equal(panels[0].firstElementChild.inert, false);
  el.toggleFold(buttons[1]);
  assert.equal(buttons[0].getAttribute('aria-expanded'), 'false');
  assert.equal(panels[0].firstElementChild.inert, true);
  assert.equal(buttons[1].getAttribute('aria-expanded'), 'true');
  el.toggleFold(buttons[1]);
  assert.equal(panels[1].firstElementChild.inert, true);
});

test('missing insights count only ok, errors remain retryable', () => {
  const el = analytics();
  el.insightRows = [insight(1, 'ok'), insight(2, 'error')];
  el.renderInsightsAction();
  assert.match(el.part('insights-action').innerHTML, /Разобрать диалоги \(2\)/);
  el.insightRows = [insight(1, 'ok'), insight(2, 'ok'), insight(3, 'ok')];
  el.renderInsightsAction();
  assert.equal(el.part('insights-action').innerHTML, '');
  el.s.dataset = '';
  el.insightRows = [];
  el.renderInsightsAction();
  assert.match(el.part('insights-action').innerHTML, /Выберите набор/);
});

test('run uses dataset API, rejects duplicate clicks, polls every 2s, refreshes once', async () => {
  const oldTimeout = globalThis.setTimeout;
  const oldClear = globalThis.clearTimeout;
  const timers = new Map();
  let nextTimer = 0, rows = [insight(11, 'ok'), insight(12, 'running'), insight(13, 'pending')];
  let starts = 0, loads = 0, resolveStart;
  globalThis.setTimeout = (fn, ms) => { assert.equal(ms, 2000); timers.set(++nextTimer, fn); return nextTimer; };
  globalThis.clearTimeout = id => timers.delete(id);
  try {
    const el = analytics({
      startInsights: body => { assert.deepEqual(body, { dataset_id: 'DEV' }); starts++; return new Promise(r => { resolveStart = r; }); },
      insights: async params => { assert.deepEqual(params, { dataset_id: 'DEV' }); return rows; },
    });
    el.load = async () => { loads++; };
    const start = el.startInsights();
    await el.startInsights();
    assert.equal(starts, 1);
    resolveStart({ insight_ids: [11, 12, 13] });
    await start;
    assert.match(el.part('insights-action').innerHTML, /Разобрано 1 из 3/);
    assert.equal(timers.size, 1);
    rows = [insight(11, 'ok'), insight(12, 'ok'), insight(13, 'error')];
    await [...timers.values()][0]();
    assert.equal(timers.size, 0);
    assert.equal(loads, 1);
  } finally { globalThis.setTimeout = oldTimeout; globalThis.clearTimeout = oldClear; }
});

test('stale and detached polling responses cannot change UI or schedule another poll', async () => {
  for (const detached of [false, true]) {
    let resolve;
    const el = analytics({ insights: () => new Promise(r => { resolve = r; }) });
    const request = el.refreshInsights(1);
    if (detached) el.disconnectedCallback(); else el.seq++;
    resolve([insight(1, 'running')]);
    await request;
    assert.equal(el.part('insights-action').innerHTML, '');
    assert.equal(el.insightsTimer, undefined);
  }
});

test('polling failure exposes retry and preserves report', async () => {
  const el = analytics({ insights: async () => { throw new Error('offline'); } });
  el.part('report').innerHTML = 'report';
  await el.refreshInsights(1);
  assert.match(el.part('insights-action').innerHTML, /data-insights-retry/);
  assert.equal(el.part('report').innerHTML, 'report');
});

test('compact Ask keeps original submit logic and uses current outer filters', async () => {
  const ask = new (registry.get('cqa-ask'))();
  ask._connected = true;
  ask.setAttribute('compact', '');
  ask.setAttribute('autofocus', '');
  ask.setAttribute('dataset-id', 'DEV');
  ask.setAttribute('checklist-id', '7');
  const question = { value: 'Вопрос', focus() { this.focused = true; } };
  const result = new Element();
  ask.part = p => ({ question, result })[p];
  ask.querySelector = () => new Element();
  let body;
  Object.defineProperty(ask, 'api', { value: { ask: async b => { body = b; return { answer: 'Ответ', refs: [], cost_usd: 0, model: 'fake' }; } } });
  ask.init();
  assert.equal(question.focused, true);
  assert.match(ask.innerHTML, /Спросить ИИ о диалогах…/);
  assert.ok(!ask.innerHTML.includes('<h1>'));
  assert.ok(!ask.innerHTML.includes('data-part="filters"'));
  ask.setAttribute('dataset-id', 'OTHER');
  await ask.submit();
  assert.deepEqual(body, { question: 'Вопрос', dataset_id: 'OTHER', checklist_id: 7 });
  assert.match(result.innerHTML, /Ответ/);
});

test('legacy ask URL selects Analytics, forwards filters and requests focus', () => {
  const app = new (registry.get('cqa-app'))();
  app.base = '/api';
  let screen;
  app.part = () => ({ replaceChildren: el => { screen = el; } });
  location.hash = '#/ask?dataset=DEV&checklist=7';
  app.route();
  assert.ok(screen instanceof registry.get('cqa-analytics'));
  assert.equal(screen.getAttribute('dataset-id'), 'DEV');
  assert.equal(screen.getAttribute('checklist-id'), '7');
  assert.equal(screen.hasAttribute('focus-ask'), true);
});
