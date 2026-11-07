import { CqaElement } from '../base.js';
import { esc, icon, toast, fmtInt, fmtPts, CHANNEL_LABEL, checklistLabel } from '../util.js';
import '../dropdown.js';
import { INSIGHT_FLAGS } from '../insights.js';
import './ask.js';

const pct = (v) => v == null ? '—' : `${fmtPts(v * 100)}%`;
const card = (d) => `#/c/${encodeURIComponent(d.id)}?e=${d.evaluation_id}`;
const link = (d) => `<a class="cqa-link" href="${esc(card(d))}">${esc(d.id)}</a>`;
const table = (heads, rows) => `<div class="cqa-table-wrap"><table class="cqa-table"><thead><tr>${heads.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows || `<tr><td colspan="${heads.length}" class="cqa-muted">Нет данных</td></tr>`}</tbody></table></div>`;

class Analytics extends CqaElement {
  async init() {
    this.folds = new Map([['analytics-queue', true], ['analytics-accuracy', true]]);
    this.onCleanup(() => clearTimeout(this.insightsTimer));
    this.s = { dataset: this.getAttribute('dataset-id') || '', checklist: this.getAttribute('checklist-id') || '', datasets: [], checklists: [] };
    this.listen(this, 'change', e => {
      const field = e.target.dataset.filter;
      if (!field) return;
      this.s[field] = e.target.value;
      this.load();
    });
    this.listen(this, 'click', e => {
      const row = e.target.closest('[data-criterion-row]');
      const toggle = e.target.closest('[data-fold]') || row?.querySelector('[data-fold]');
      if (toggle) this.toggleFold(toggle);
      if (e.target.closest('[data-insights-run]')) this.startInsights();
      if (e.target.closest('[data-insights-retry]')) this.refreshInsights(this.seq);
      if (e.target.closest('[data-retry]')) this.load();
    });
    this.listen(window, 'cqa-appeals-changed', () => this.load());
    this.listen(window, 'cqa-data-changed', () => this.load());
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner"></span>Загружаю аналитику…</div>';
    if (this.api.mock) {
      this.innerHTML = '<h1>Аналитика</h1><p class="cqa-muted">Откройте приложение без ?mock=1, чтобы посмотреть аналитику по базе.</p>';
      return;
    }
    try {
      [this.s.datasets, this.s.checklists] = await Promise.all([this.api.datasets(), this.api.checklists()]);
      if (!this.alive) return;
      this.renderShell();
      await this.load();
    } catch (e) { this.error(e); }
  }

  params() { return { dataset_id: this.s.dataset, checklist_id: this.s.checklist }; }

  async load() {
    const seq = this.seq = (this.seq || 0) + 1;
    clearTimeout(this.insightsTimer);
    this.insightsBatch = null;
    this.insightsStarting = false;
    this.part('report').innerHTML = '<div class="cqa-loading"><span class="cqa-spinner"></span>Загружаю…</div>';
    try {
      const data = await this.api.analytics(this.params());
      if (!this.alive || seq !== this.seq) return;
      this.data = data;
      this.s.checklist = String(data.checklist_id);
      const checklist = this.querySelector('[data-filter="checklist"]');
      checklist.value = this.s.checklist;
      checklist.dispatchEvent(new Event('change'));
      const ask = this.querySelector('cqa-ask');
      ask.setAttribute('dataset-id', this.s.dataset);
      ask.setAttribute('checklist-id', this.s.checklist);
      this.renderReport();
      await this.refreshInsights(seq);
    } catch (e) { if (seq === this.seq) this.error(e); }
  }

  error(e) {
    if (!this.alive) return;
    const area = this.part('report') || this;
    area.innerHTML = `<div class="cqa-error-box">${icon('alert')}${esc(e.message)} <button class="cqa-btn" data-retry>Повторить</button></div>`;
    toast('Не удалось загрузить аналитику', 'error');
  }

  renderShell() {
    this.innerHTML = `<div class="cqa-page-head"><h1>Аналитика</h1><div class="cqa-page-actions" data-part="export"></div></div>
      <cqa-ask compact api-base="${esc(this.getAttribute('api-base') || '/api')}" dataset-id="${esc(this.s.dataset)}" checklist-id="${esc(this.s.checklist)}" ${this.hasAttribute('focus-ask') ? 'autofocus' : ''}></cqa-ask>
      <div class="cqa-toolbar"><div class="cqa-toolbar-group">
        <select class="cqa-select cqa-select--sm" data-filter="dataset" aria-label="Набор">
          <option value="">Все наборы</option>${this.s.datasets.map(d => `<option value="${esc(d.id)}" ${d.id === this.s.dataset ? 'selected' : ''}>${esc(d.id)}</option>`).join('')}
        </select>
        <select class="cqa-select cqa-select--sm" data-filter="checklist" aria-label="Чек-лист">
          <option value="" ${!this.s.checklist ? 'selected' : ''}>Действующий чек-лист</option>
          ${[...this.s.checklists].reverse().map(c => `<option value="${c.id}" ${String(c.id) === this.s.checklist ? 'selected' : ''}>${esc(checklistLabel(c))}</option>`).join('')}
        </select>
      </div></div><div data-part="report" aria-live="polite"><div class="cqa-loading"><span class="cqa-spinner"></span>Загружаю…</div></div>`;
  }

  setFold(button, open) {
    const panel = this.querySelector(`#${button.dataset.fold}`);
    button.setAttribute('aria-expanded', String(open));
    panel.classList.toggle('is-open', open);
    panel.firstElementChild.inert = !open;
    panel.setAttribute('aria-hidden', String(!open));
    this.folds.set(button.dataset.fold, open);
  }

  toggleFold(button) {
    const open = button.getAttribute('aria-expanded') !== 'true';
    if (open && button.classList.contains('cqa-analytics-criterion')) {
      this.querySelectorAll('.cqa-analytics-criterion[aria-expanded="true"]').forEach(b => this.setFold(b, false));
    }
    this.setFold(button, open);
  }

  fold(id, title, note, content) {
    const open = this.folds.get(id) === true;
    return `<section class="cqa-analytics-section"><h2><button type="button" class="cqa-analytics-toggle" data-fold="${id}" aria-controls="${id}" aria-expanded="${open}">
      <span>${title}</span><span class="cqa-muted">${note}</span>${icon('down')}</button></h2>
      <div class="cqa-analytics-fold${open ? ' is-open' : ''}" id="${id}" aria-hidden="${!open}"><div ${open ? '' : 'inert'}><div class="cqa-analytics-content">${content}</div></div></div></section>`;
  }

  async refreshInsights(seq) {
    clearTimeout(this.insightsTimer);
    try {
      const rows = await this.api.insights({ dataset_id: this.s.dataset });
      if (!this.alive || seq !== this.seq) return;
      this.insightRows = rows;
      const busy = rows.some(r => ['pending', 'running'].includes(r.data?.status));
      this.renderInsightsAction();
      if (busy) {
        this.insightsWereBusy = true;
        this.insightsTimer = setTimeout(() => this.refreshInsights(seq), 2000);
      } else if (this.insightsWereBusy) {
        this.insightsWereBusy = false;
        await this.load();
      }
    } catch (e) {
      if (!this.alive || seq !== this.seq) return;
      this.part('insights-action').innerHTML = `<span class="cqa-muted">Прогресс недоступен</span> <button class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-insights-retry>Повторить</button>`;
    }
  }

  renderInsightsAction() {
    const area = this.part('insights-action');
    const rows = this.insightRows || [];
    const busy = rows.filter(r => ['pending', 'running'].includes(r.data?.status)).length;
    const missing = Math.max(0, this.data.summary.evaluated - rows.filter(r => r.data?.status === 'ok').length);
    if (this.insightsStarting) {
      area.innerHTML = '<span class="cqa-spinner"></span> Запускаю разбор…';
    } else if (busy) {
      const batch = this.insightsBatch ? rows.filter(r => this.insightsBatch.includes(r.id)) : rows;
      const done = batch.filter(r => r.data?.status === 'ok').length;
      const failed = batch.filter(r => r.data?.status === 'error').length;
      area.innerHTML = `<span class="cqa-spinner"></span> Разобрано ${done} из ${this.insightsBatch?.length || batch.length}${failed ? ` · ошибок ${failed}` : ''}`;
    } else if (missing && this.s.dataset) {
      area.innerHTML = `<button class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-insights-run>${icon('spark')}Разобрать диалоги (${missing})</button>`;
    } else if (missing) {
      area.innerHTML = '<span class="cqa-faint">Выберите набор для разбора</span>';
    } else {
      area.innerHTML = '';
    }
  }

  async startInsights() {
    if (this.insightsStarting || !this.s.dataset) return;
    const seq = this.seq;
    this.insightsStarting = true;
    this.renderInsightsAction();
    try {
      const result = await this.api.startInsights({ dataset_id: this.s.dataset });
      if (!this.alive || seq !== this.seq) return;
      this.insightsBatch = result.insight_ids;
      this.insightsWereBusy = true;
      this.insightsStarting = false;
      await this.refreshInsights(seq);
    } catch (e) {
      if (!this.alive || seq !== this.seq) return;
      this.insightsStarting = false;
      this.renderInsightsAction();
      toast(e.message, 'error');
    }
  }

  renderReport() {
    const d = this.data, s = d.summary;
    this.part('export').innerHTML = `<a class="cqa-btn" href="${esc(this.api.analyticsExportUrl(this.params()))}" download>${icon('download')}Скачать CSV</a>`;
    const summary = `<div class="cqa-page-sub cqa-analytics-summary">
      <span>Оценено <b>${fmtInt(s.evaluated)}</b></span><span>Средний итог <b>${fmtPts(s.average_total)}</b></span>
      <span>Грубые <b>${pct(s.gross_rate)}</b></span><span>Разногласия <b>${pct(s.disagreement_rate)}</b></span>
      <span data-part="insights-action" role="status"></span>
      <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm cqa-analytics-method" data-fold="analytics-method" aria-controls="analytics-method" aria-expanded="${this.folds.get('analytics-method') === true}">Как считаем</button></div>`;
    const methodOpen = this.folds.get('analytics-method') === true;
    const notes = `<div class="cqa-analytics-fold${methodOpen ? ' is-open' : ''}" id="analytics-method" aria-hidden="${!methodOpen}"><div ${methodOpen ? '' : 'inert'}><div class="cqa-analytics-content cqa-muted">
      <p>Последняя успешная оценка по чек-листу; итоги и нарушения — с решениями проверяющего. Итог: ${s.scored} диалогов, грубые: ${s.gross_n}; нет данных — «—».</p>
      <p>Нарушение: «Нет», неполная ступень или флаг «Да»; доля — без N/A. Средний балл — после перераспределения N/A, без N/A и флагов.</p>
      <p>Разногласия — исходные голоса (${s.agreement_n} из ${s.decisions} решений). Апелляции — вся история набора и версии; случайная проверка — 10% остальных, округление вверх, seed ${d.sample.seed}.</p>
      <p>CSV: финальные ответы, баллы, исходные веса, итог и флаг; исходные доказательства, пояснения и голоса; отдельно — ответ модели и причина проверяющего.</p>
      </div></div></div>`;
    const criteria = `<section class="cqa-analytics-section"><h2>Критерии</h2>${table(
      ['Критерий', 'Нарушения', 'N/A', 'Разногласия', 'Ср. балл'],
      d.criteria.map((c, i) => `<tr data-criterion-row><td><button type="button" class="cqa-analytics-criterion" data-fold="analytics-criterion-${i}" aria-controls="analytics-criterion-${i}" aria-expanded="${this.folds.get(`analytics-criterion-${i}`) === true}">${esc(c.id)} · ${esc(c.title)}${icon('down')}</button></td>
        <td><div class="cqa-analytics-rate"><div class="cqa-bar${c.violation_rate == null ? ' cqa-bar--na' : ''}" aria-hidden="true"><div class="cqa-bar-fill" style="width:${Math.max(0, Math.min(100, (c.violation_rate || 0) * 100))}%"></div></div><span>${pct(c.violation_rate)}</span> <span class="cqa-faint">${c.violations}/${c.applicable_n}</span></div></td><td>${pct(c.na_rate)}</td>
        <td>${pct(c.disagreement_rate)} <span class="cqa-faint">n=${c.agreement_n}</span></td><td>${fmtPts(c.average_points)}</td></tr>
        <tr class="cqa-analytics-detail-row"><td colspan="5"><div class="cqa-analytics-fold${this.folds.get(`analytics-criterion-${i}`) === true ? ' is-open' : ''}" id="analytics-criterion-${i}" aria-hidden="${this.folds.get(`analytics-criterion-${i}`) !== true}"><div ${this.folds.get(`analytics-criterion-${i}`) === true ? '' : 'inert'}><div class="cqa-analytics-content">
        ${c.violating_dialogues.length ? `<div class="cqa-analytics-links">${c.violating_dialogues.map(v => `<span>${link(v)} <span class="cqa-muted">${esc(v.language)} · ${esc(CHANNEL_LABEL(v.channel))} · ${fmtPts(v.total)}</span></span>`).join('')}</div>` : '<span class="cqa-muted">Нарушений нет</span>'}
        </div></div></div></td></tr>`).join(''))}</section>`;
    const compared = d.criteria.reduce((n, c) => n + c.accuracy.n, 0);
    const matched = d.criteria.reduce((n, c) => n + c.accuracy.matches, 0);
    const accuracy = this.fold('analytics-accuracy', 'Совпадение с утверждёнными оценками', `${pct(compared ? matched / compared : null)} · ${fmtInt(compared)} решений`,
      table(['Критерий', 'Совпадение', 'n'], d.criteria.map(c => `<tr><td>${esc(c.id)} · ${esc(c.title)}</td><td>${pct(c.accuracy.rate)}</td><td>${c.accuracy.n}</td></tr>`).join('')) +
      '<p class="cqa-muted">Сравниваем исходный ответ модели, включая N/A. Последнее решение проверяющего перекрывает разметку DEV. Учитываем только совпадающие определения критериев. DEV — разметка моделями-судьями, не эталон организаторов.</p>');
    const titles = Object.fromEntries(d.criteria.map(c => [c.id, c.title]));
    const reasons = r => r.code === 'gross' ? 'Грубое нарушение' : r.code === 'low_score' ? 'Итог ниже 60' : r.code === 'sample' ? 'Случайная проверка · 10%' :
      `Оценки разошлись: ${r.criteria.map(id => `${id} · ${titles[id]}`).join(', ')}`;
    const queue = this.fold('analytics-queue', 'Очередь проверки', `${d.review_queue.length} диалогов`,
      table(['Диалог', 'Итог', 'Причина'], d.review_queue.map(v => `<tr><td>${link(v)}</td><td>${fmtPts(v.total)}</td><td>${esc(v.reasons.map(reasons).join(' · '))}</td></tr>`).join('')));
    const appeals = this.fold('analytics-appeals', 'Апелляции', `${s.appeals.submitted} подано · ${s.appeals.decided} решено · ${s.appeals.changed} изменено`,
      table(['Критерий', 'Подано', 'Решено', 'Оценка изменена'], [...d.criteria].sort((a, b) => b.appeals.submitted - a.appeals.submitted).map(c => `<tr><td>${esc(c.id)} · ${esc(c.title)}</td><td>${c.appeals.submitted}</td><td>${c.appeals.decided}</td><td>${c.appeals.changed}</td></tr>`).join('')));
    const slices = this.fold('analytics-slices', 'Языки и каналы', `${d.slices.language.length} языков · ${d.slices.channel.length} каналов`,
      ['language', 'channel'].map(field => table([field === 'language' ? 'Язык' : 'Канал', 'Диалоги', 'Средний итог', 'Грубые нарушения', 'Оценки разошлись'],
        d.slices[field].map(g => `<tr><td>${esc(field === 'channel' ? CHANNEL_LABEL(g.value) : g.value)}</td><td>${g.evaluated}</td><td>${fmtPts(g.average_total)}</td><td>${pct(g.gross_rate)}</td><td>${pct(g.disagreement_rate)}</td></tr>`).join(''))).join(''));
    const insights = d.insights?.covered ? this.fold('analytics-topics', 'Темы обращений', `${d.insights.covered} диалогов с разбором`,
      table(['Тема', 'Диалоги', 'Средний итог', 'Частое нарушение'], d.insights.topics.map(t => `<tr><td>${esc(t.topic)}</td><td>${t.count}</td><td>${fmtPts(t.average_total)}</td><td>${t.top_violation ? `${esc(t.top_violation.id)} · ${esc(t.top_violation.title)} (${t.top_violation.count})` : '—'}</td></tr>`).join('')) +
      `<div class="cqa-analytics-links">${d.insights.flags.map(f => `<span>${esc(INSIGHT_FLAGS[f.code] || f.code)} <b>${f.count}</b></span>`).join('') || '<span class="cqa-muted">Флагов нет</span>'}</div>`) : '';
    this.part('report').innerHTML = summary + notes + (s.evaluated ? '' : '<p class="cqa-muted">Успешных оценок по этому чек-листу пока нет.</p>') +
      criteria + this.fold('analytics-histogram', 'Распределение итогов', `${s.scored} диалогов · из 100 баллов`, this.histogram(d.histogram)) + slices + accuracy + queue + appeals + insights;
  }

  histogram(bins) {
    const max = Math.max(1, ...bins.map(b => b.count));
    return `<svg class="cqa-analytics-chart" viewBox="0 0 700 180" role="img" aria-label="Распределение итогов по десятибалльным интервалам">
      ${bins.map((b, i) => {
        const h = 120 * b.count / max, x = i * 70 + 6;
        const label = `${b.from}–${b.to}`;
        return `<g><title>${b.from} ≤ итог ${i === 9 ? '≤' : '&lt;'} ${b.to}: ${b.count} диалогов</title>
          <rect x="${x}" y="${145 - h}" width="55" height="${h}" rx="2"/>
          <text x="${x + 27}" y="${137 - h}" text-anchor="middle">${b.count}</text>
          <text x="${x + 27}" y="168" text-anchor="middle">${label}</text></g>`;
      }).join('')}</svg>`;
  }
}

customElements.define('cqa-analytics', Analytics);
