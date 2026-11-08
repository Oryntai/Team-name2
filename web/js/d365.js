// Макет Dynamics 365 Customer Service: экраны продукта — те же веб-компоненты, что в основном интерфейсе (index.html),
// внутри оболочки CRM. Компоненты не меняются. Карточка на форме записи берёт id через заглушку Xrm — так же, как
// HTML-веб-ресурс на форме в настоящей CRM. Маршруты — те же #/…, поэтому ссылки внутри компонентов работают здесь.
import { getApi } from './api.js';
import { esc, fmtScore, fmtDateTime, getRole, setRole, icon, CHANNEL_LABEL } from './util.js';
import './dropdown.js';
import './components/conversation-list.js';
import './components/card.js';
import './components/appeals.js';
import './components/analytics.js';
import './components/checklist-editor.js';
import './components/journal.js';
import './components/ask.js';

const API = '/api';
const api = getApi(API);

const AREAS = [
  { key: 'list', href: '#/', label: 'Разговоры', icon: 'chat', title: 'Разговоры', sub: 'Все диалоги с оценкой качества' },
  { key: 'appeals', href: '#/appeals', label: 'Апелляции', icon: 'gavel', title: 'Апелляции', sub: 'Оспоренные оценки и решения проверяющих' },
  { key: 'analytics', href: '#/analytics', label: 'Аналитика', icon: 'scale', title: 'Аналитика качества', sub: 'Операторы, критерии, языки' },
  { key: 'checklist', href: '#/checklist', label: 'Чек-листы', icon: 'checklist', title: 'Чек-листы', sub: 'Критерии, веса, шкалы и версии' },
  { key: 'journal', href: '#/journal', label: 'Журнал оценок', icon: 'journal', title: 'Журнал оценок', sub: 'Прогоны модели: время и стоимость' },
];
const ROLE_NAME = { operator: 'Оператор', reviewer: 'Проверяющий' };

// Заглушка клиентского API Dynamics 365 (Xrm): веб-ресурс на форме берёт id открытой записи так же, как в CRM.
const record = { id: null };
window.Xrm = {
  Page: { data: { entity: {
    getId: () => (record.id ? `{${record.id}}` : ''),
    getEntityName: () => 'cqa_conversation',
  } } },
  Utility: { getGlobalContext: () => ({ userSettings: { userName: ROLE_NAME[getRole()] || 'Пользователь' } }) },
};

const part = (name) => document.querySelector(`[data-part="${name}"]`);
const WAFFLE = '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">'
  + [1, 6.5, 12].flatMap((y) => [1, 6.5, 12].map((x) => `<rect x="${x}" y="${y}" width="3" height="3"/>`)).join('') + '</svg>';

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const i = raw.indexOf('?');
  const path = i >= 0 ? raw.slice(0, i) : raw;
  const params = new URLSearchParams(i >= 0 ? raw.slice(i + 1) : '');
  const parts = path.split('/').filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  return { parts, params };
}

function renderChrome() {
  part('top').innerHTML = `
    <span class="d365-waffle" aria-hidden="true">${WAFFLE}</span>
    <span class="d365-product">Dynamics 365</span><span class="d365-divider"></span>
    <span class="d365-app">Customer Service · Контроль качества</span>
    <div class="d365-top-right">
      <span class="d365-mock" title="Оболочка CRM нарисована; экраны внутри — настоящий продукт и настоящий сервер оценки">Макет CRM</span>
      <div class="d365-roles" role="group" aria-label="Роль пользователя">
        ${Object.entries(ROLE_NAME).map(([r, name]) => `<button type="button" data-role="${r}">${name}</button>`).join('')}
      </div>
      <span class="d365-avatar" data-part="avatar" aria-hidden="true"></span>
    </div>`;
  part('top').addEventListener('click', (e) => {
    const b = e.target.closest('[data-role]');
    if (b) setRole(b.dataset.role);
  });
  part('sitemap').innerHTML = `
    <div class="d365-group">Контроль качества</div>
    ${AREAS.map((a) => `<a href="${a.href}" data-nav="${a.key}">${icon(a.icon)}<span>${esc(a.label)}</span>${a.key === 'appeals' ? '<span class="d365-badge" data-part="badge" hidden></span>' : ''}</a>`).join('')}
    <div class="d365-group">Рабочее место оператора</div>
    <a href="guard.html" target="_blank" rel="noopener">${icon('shield')}<span>Щит до отправки</span></a>
    <div class="d365-area">Область<b>Контроль качества</b></div>`;
  syncRole();
}

function syncRole() {
  const r = getRole();
  document.querySelectorAll('[data-role]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.role === r)));
  const av = part('avatar');
  if (av) { av.textContent = (ROLE_NAME[r] || '?')[0]; av.title = ROLE_NAME[r] || ''; }
}

async function refreshBadge() {
  const el = part('badge');
  if (!el) return;
  try {
    const n = ((await api.appeals('open')) || []).filter((a) => a.status === 'open').length;
    el.textContent = String(n);
    el.hidden = !n;
    el.title = `Открытых апелляций: ${n}`;
  } catch { el.hidden = true; }
}

function markNav(key) {
  document.querySelectorAll('[data-nav]').forEach((a) => {
    if (a.dataset.nav === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
}

function setCommands(items) {
  const bar = part('cmd');
  bar.innerHTML = items.map((c, i) => (c.href
    ? `<a class="d365-cmd" href="${esc(c.href)}"${c.blank ? ' target="_blank" rel="noopener"' : ''}>${icon(c.icon)}${esc(c.label)}</a>`
    : `<button type="button" class="d365-cmd" data-cmd="${i}">${icon(c.icon)}${esc(c.label)}</button>`)).join('');
  bar.onclick = (e) => {
    const b = e.target.closest('[data-cmd]');
    if (b) items[Number(b.dataset.cmd)].run();
  };
}

function mount(tag, attrs = {}) {
  const el = document.createElement(tag);
  el.setAttribute('api-base', API);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== '') el.setAttribute(k, v);
  return el;
}

const field = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value ?? '—')}</dd></div>`;

function route() {
  const { parts, params } = parseHash();
  const [head, id] = parts;
  record.id = null;
  if (head === 'c' && id) { record.id = id; markNav('list'); renderRecord(id, params.get('e')); return; }
  const key = { appeals: 'appeals', analytics: 'analytics', ask: 'analytics', checklist: 'checklist', journal: 'journal' }[head] || 'list';
  const area = AREAS.find((a) => a.key === key);
  markNav(key);
  let el;
  if (key === 'appeals') el = mount('cqa-appeals', { 'appeal-id': params.get('id') });
  else if (key === 'analytics') {
    el = mount('cqa-analytics', { 'dataset-id': params.get('dataset_id') || params.get('dataset'),
      'checklist-id': params.get('checklist_id') || params.get('checklist') });
    if (head === 'ask') el.setAttribute('focus-ask', '');
  } else if (key === 'checklist') el = mount('cqa-checklist-editor', { 'checklist-id': params.get('id') });
  else if (key === 'journal') el = mount('cqa-journal');
  else el = mount('cqa-conversation-list', { dataset: params.get('dataset') });
  setCommands([
    { label: 'Обновить', icon: 'refresh', run: route },
    { label: 'Открыть в продукте', icon: 'arrow', href: `index.html${location.hash || '#/'}`, blank: true },
  ]);
  // Заголовок раздела рисует сам компонент — оболочка его не дублирует.
  part('page').replaceChildren(el);
  document.title = `${area.title} — Контроль качества · Dynamics 365 (макет)`;
  window.scrollTo(0, 0);
}

async function renderRecord(id, evId) {
  setCommands([
    { label: 'Назад', icon: 'back', run: () => { if (history.length > 1) history.back(); else location.hash = '#/'; } },
    { label: 'Обновить', icon: 'refresh', run: route },
    { label: 'Открыть в продукте', icon: 'arrow', href: `index.html#/c/${encodeURIComponent(id)}`, blank: true },
  ]);
  const page = part('page');
  page.innerHTML = `
    <div class="d365-formhead">
      <div class="d365-entity"><span class="d365-entity-icon">${icon('chat')}</span>
        <div><h1>${esc(id)}</h1><div class="d365-sub">Разговор · форма «Контроль качества»</div></div></div>
      <dl class="d365-headfields" data-part="head">${field('Канал', '…')}${field('Язык', '…')}${field('Итог', '…')}</dl>
    </div>
    <div class="d365-tabs" role="tablist" aria-label="Вкладки формы">
      <button type="button" role="tab" aria-selected="true" data-tab="quality">Оценка качества</button>
      <button type="button" role="tab" aria-selected="false" data-tab="details">Сведения</button>
    </div>
    <section class="d365-tabpanel" data-panel="quality">
      <div class="d365-section-title">${icon('file')}<span>HTML-веб-ресурс <code>cqa_/card.html</code> · компонент <code>&lt;cqa-card&gt;</code></span></div>
      <div data-part="slot"></div>
    </section>
    <section class="d365-tabpanel" data-panel="details" hidden>
      <div class="d365-surface">
        <dl class="d365-fields" data-part="fields"></dl>
        <p class="d365-note">Вкладка «Оценка качества» — HTML-веб-ресурс на форме сущности «Разговор». Компонент
          <code>&lt;cqa-card&gt;</code> получает id записи так же, как в Dynamics 365:
          <code>Xrm.Page.data.entity.getId()</code> → <code>${esc(window.Xrm.Page.data.entity.getId())}</code>, и сам обращается
          к серверу оценки (<code>${esc(API)}</code>). Разделы слева — те же компоненты продукта:
          <code>&lt;cqa-conversation-list&gt;</code>, <code>&lt;cqa-appeals&gt;</code>, <code>&lt;cqa-analytics&gt;</code>,
          <code>&lt;cqa-checklist-editor&gt;</code>, <code>&lt;cqa-journal&gt;</code>. Для встраивания их код не меняется.</p>
      </div>
    </section>`;
  page.querySelector('.d365-tabs').addEventListener('click', (e) => {
    const t = e.target.closest('[data-tab]');
    if (!t) return;
    page.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b === t)));
    page.querySelectorAll('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== t.dataset.tab; });
  });
  // Как веб-ресурс в CRM: id открытой записи — из контекста формы, а не из адреса страницы.
  const card = mount('cqa-card', { 'conversation-id': window.Xrm.Page.data.entity.getId().replace(/[{}]/g, ''), 'evaluation-id': evId });
  card.setAttribute('embedded', '');
  part('slot').append(card);
  document.title = `${id} — Разговор · Dynamics 365 (макет)`;
  window.scrollTo(0, 0);

  try {
    const c = ((await api.conversations()) || []).find((x) => x.id === id);
    if (!c || record.id !== id) return;
    const ev = c.latest || {};
    const ok = ev.status === 'ok';
    const total = ok ? (ev.final_total ?? ev.total) : null;
    const appeals = c.appeals || {};
    part('head').innerHTML = field('Канал', CHANNEL_LABEL(c.channel)) + field('Язык', c.language)
      + field('Итог', total == null ? '—' : `${fmtScore(total)} из 100`)
      + field('Грубая ошибка', ok ? ((ev.final_gross ?? ev.gross) ? 'Да' : 'Нет') : '—')
      + field('Апелляции', `${appeals.open || 0} откр. · ${appeals.decided || 0} реш.`);
    part('fields').innerHTML = field('Набор', c.dataset_id) + field('Длительность', c.duration) + field('Реплик', c.n_replies)
      + field('Чек-лист оценки', ev.checklist_label) + field('Модель', ev.model) + field('Оценено', ev.created_at ? fmtDateTime(ev.created_at) : null);
  } catch { /* шапка формы — справочная; карточка работает и без неё */ }
}

renderChrome();
window.addEventListener('hashchange', route);
window.addEventListener('cqa-role', syncRole);
window.addEventListener('cqa-appeals-changed', refreshBadge);
window.addEventListener('cqa-data-changed', refreshBadge);
route();
refreshBadge();
