// <cqa-app> — оболочка приложения: шапка, навигация, роль, маршруты по #.
// Экраны — самостоятельные веб-компоненты; каждый можно вставить отдельно (например, в форму Dynamics 365).
import { CqaElement } from './base.js';
import { isMock } from './api.js';
import { icon, getRole, setRole } from './util.js';
import './dropdown.js';
import './components/conversation-list.js';
import './components/card.js';
import './components/appeals.js';
import './components/analytics.js';
import './components/checklist-editor.js';
import './components/journal.js';

const NAV = [
  { key: 'list', href: '#/', label: 'Диалоги', icon: 'chat' },
  { key: 'appeals', href: '#/appeals', label: 'Апелляции', icon: 'gavel' },
  { key: 'analytics', href: '#/analytics', label: 'Аналитика', icon: 'scale' },
  { key: 'checklist', href: '#/checklist', label: 'Чек-лист', icon: 'checklist' },
  { key: 'journal', href: '#/journal', label: 'Журнал', icon: 'journal' },
];

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const i = raw.indexOf('?');
  const path = i >= 0 ? raw.slice(0, i) : raw;
  const params = new URLSearchParams(i >= 0 ? raw.slice(i + 1) : '');
  const parts = path.split('/').filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  return { parts, params };
}

class CqaApp extends CqaElement {
  init() {
    this.base = this.getAttribute('api-base') || '/api';
    document.documentElement.dataset.cqaRole = getRole();
    this.innerHTML = `
      <header class="cqa-header"><div class="cqa-header-in">
        <a class="cqa-brand" href="#/" aria-label="CQA — на главную">
          <span class="cqa-brand-mark">${icon('logo')}</span>
          <span class="cqa-brand-text"><span class="cqa-brand-title">CQA</span><span class="cqa-brand-sub">Контроль качества диалогов</span></span>
        </a>
        <nav class="cqa-nav" aria-label="Разделы">
          ${NAV.map((n) => `<a href="${n.href}" data-nav="${n.key}">${icon(n.icon)}<span class="cqa-nav-text">${n.label}</span>${n.key === 'appeals' ? '<span class="cqa-nav-count cqa-hidden" data-part="appeals-count"></span>' : ''}</a>`).join('')}
        </nav>
        <div class="cqa-header-right">
          ${isMock() ? '<span class="cqa-demo-tag" title="Данные выдуманы, сервер не используется">Демо-режим</span>' : ''}
          <div class="cqa-role"><span>Роль:</span>
            <div class="cqa-seg" role="group" aria-label="Роль пользователя">
              <button type="button" data-role="operator" title="Оператор: видит оценку и подаёт апелляцию">${icon('user')}Оператор</button>
              <button type="button" data-role="reviewer" title="Проверяющий: рассматривает апелляции и принимает решение">${icon('shield')}Проверяющий</button>
            </div>
          </div>
        </div>
      </div></header>
      <main class="cqa-main" data-part="main"></main>`;
    this.syncRole();
    this.addEventListener('click', (e) => {
      const b = e.target.closest('[data-role]');
      if (b) { setRole(b.dataset.role); }
    });
    this.listen(window, 'cqa-role', () => this.syncRole());
    this.listen(window, 'hashchange', () => this.route());
    this.listen(window, 'cqa-appeals-changed', () => this.refreshBadge());
    this.listen(window, 'cqa-data-changed', () => this.refreshBadge());
    this.route();
    this.refreshBadge();
  }

  syncRole() {
    const r = getRole();
    this.querySelectorAll('[data-role]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.role === r)));
  }

  async refreshBadge() {
    const el = this.part('appeals-count');
    if (!el) return;
    try {
      const list = await this.api.appeals('open');
      const n = (list || []).filter((a) => a.status === 'open').length;
      el.textContent = String(n);
      el.classList.toggle('cqa-hidden', !n);
      el.title = `Открытых апелляций: ${n}`;
    } catch { el.classList.add('cqa-hidden'); }
  }

  route() {
    const { parts, params } = parseHash();
    const [head, id] = parts;
    let el, nav = 'list';
    if (head === 'c' && id) {
      el = document.createElement('cqa-card');
      el.setAttribute('conversation-id', id);
      if (params.get('e')) el.setAttribute('evaluation-id', params.get('e'));
    } else if (head === 'appeals') {
      el = document.createElement('cqa-appeals');
      if (params.get('id')) el.setAttribute('appeal-id', params.get('id'));
      nav = 'appeals';
    } else if (head === 'analytics') {
      el = document.createElement('cqa-analytics');
      if (params.get('dataset_id')) el.setAttribute('dataset-id', params.get('dataset_id'));
      if (params.get('checklist_id')) el.setAttribute('checklist-id', params.get('checklist_id'));
      nav = 'analytics';
    } else if (head === 'checklist') {
      el = document.createElement('cqa-checklist-editor');
      if (params.get('id')) el.setAttribute('checklist-id', params.get('id'));
      nav = 'checklist';
    } else if (head === 'journal') {
      el = document.createElement('cqa-journal');
      nav = 'journal';
    } else {
      el = document.createElement('cqa-conversation-list');
      if (params.get('dataset')) el.setAttribute('dataset', params.get('dataset'));
    }
    el.setAttribute('api-base', this.base);
    this.querySelectorAll('[data-nav]').forEach((a) => {
      if (a.dataset.nav === nav) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    const main = this.part('main');
    main.replaceChildren(el);
    window.scrollTo(0, 0);
    document.title = `${{ list: 'Диалоги', appeals: 'Апелляции', analytics: 'Аналитика', checklist: 'Чек-лист', journal: 'Журнал' }[nav]}${head === 'c' && id ? ` · ${id}` : ''} — CQA`;
  }
}

customElements.define('cqa-app', CqaApp);
