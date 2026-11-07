// <cqa-checklist-editor [checklist-id="…"]> — версии чек-листа, редактор критериев, живая проверка,
// сохранение новой версии и применение её к набору диалогов (новый прогон модели).
import { CqaElement } from '../base.js';
import {
  esc, icon, fmtDateTime, fmtInt, fmtPts, fmtMoney, fmtClock, toast, debounce, clone, emit, pollRun, isRunFinished,
  providerLabel, plural, TYPE_LABEL, secondsBetween,
} from '../util.js';

const FIELDS = ['section', 'title', 'type', 'weight', 'na', 'yes', 'no', 'na_when', 'steps', 'guidance'];
const FIELD_LABEL = { section: 'секция', title: 'название', type: 'тип', weight: 'вес', na: 'N/A', yes: '«Да»', no: '«Нет»', na_when: 'условие N/A', steps: 'шкала', guidance: 'пояснение' };

const normSteps = (st) => [...(st || [])].map((s) => ({ value: Number(s.value), text: String(s.text || '').trim() })).sort((a, b) => b.value - a.value);
function normField(c, f) {
  const v = c?.[f];
  if (f === 'steps') return c?.type === 'scale' ? normSteps(v) : [];
  if (f === 'weight') return Number(v || 0);
  if (f === 'na') return !!v;
  if (f === 'na_when') return c?.na ? String(v || '').trim() : '';
  return String(v ?? '').trim();
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function cleanConfig(cfg) {
  const out = clone(cfg);
  out.rounding = Number.isFinite(+out.rounding) ? Math.round(+out.rounding) : 2;
  out.rules = (out.rules || []).map((r) => String(r || '').trim()).filter(Boolean);
  out.criteria = (out.criteria || []).map((c) => {
    const x = { ...c, weight: Number(c.weight || 0), na: !!c.na };
    if (x.type === 'flag') { x.weight = 0; x.na = false; }
    if (!x.na) x.na_when = '';
    x.steps = x.type === 'scale' ? normSteps(x.steps) : [];
    for (const k of ['section', 'title', 'yes', 'no', 'na_when', 'guidance']) x[k] = String(x[k] ?? '').trim();
    return x;
  });
  return out;
}
function allowedFor(c) {
  const vals = c.type === 'scale' ? normSteps(c.steps).map((s) => String(s.value)) : ['Да', 'Нет'];
  if (c.na && c.type !== 'flag') vals.push('N/A');
  return vals;
}

class ChecklistEditor extends CqaElement {
  init() {
    this.s = {
      list: [], selId: this.getAttribute('checklist-id'), loaded: null, parent: null, draft: null, open: new Set(),
      validation: null, validating: false, note: '', meta: null, datasets: [], applyDataset: null, applyProvider: null, run: null, justSaved: null,
      assists: new Map(),
      full: false, plan: null, planError: null, diff: null, trial: {},
    };
    this.validateSoon = debounce(() => this.validate(), 350);
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Загружаю чек-листы…</div>';
    this.addEventListener('click', (e) => this.onClick(e));
    this.addEventListener('input', (e) => this.onInput(e));
    this.addEventListener('change', (e) => this.onInput(e, true));
    this.load();
  }

  async load() {
    try {
      const [list, meta, datasets] = await Promise.all([this.api.checklists(), this.api.meta(), this.api.datasets()]);
      this.s.list = (list || []).sort((a, b) => b.id - a.id);
      this.s.meta = meta; this.s.datasets = datasets || [];
      this.s.applyDataset = this.s.datasets[0]?.id ?? null;
      const avail = (meta?.providers || []).filter((p) => p.available !== false);
      this.s.applyProvider = avail.find((p) => p.id === meta?.default_provider)?.id || avail[0]?.id || null;
      const id = this.s.list.some((c) => String(c.id) === String(this.s.selId)) ? this.s.selId : this.s.list[0]?.id;
      if (id == null) { this.renderEmpty(); return; }
      await this.open(id);
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось загрузить чек-листы: ${esc(e.message)}</div></div>`;
    }
  }

  renderEmpty() {
    this.innerHTML = `<div class="cqa-panel"><div class="cqa-empty"><div class="cqa-empty-icon">${icon('checklist')}</div><h3>Чек-листов нет</h3><p>Сервер создаёт начальный чек-лист из PDF организаторов при первом запуске.</p></div></div>`;
  }

  async open(id) {
    const loaded = await this.api.checklist(id);
    let parent = null;
    if (loaded.parent_id != null) { try { parent = await this.api.checklist(loaded.parent_id); } catch { parent = null; } }
    if (!this.alive) return;
    Object.assign(this.s, { selId: loaded.id, loaded, parent, draft: clone(loaded.config), validation: { ok: true, errors: [] }, note: '' });
    this.s.open = new Set();
    this.s.assists = new Map();
    this.trySeq = (this.trySeq || 0) + 1;
    this.s.trial = {};
    if (parent) for (const c of loaded.config.criteria || []) {
      const p = (parent.config.criteria || []).find((x) => x.id === c.id);
      if (!p || FIELDS.some((f) => !same(normField(c, f), normField(p, f)))) this.s.open.add(c.id);
    }
    this.render();
    this.loadPlan();
    if (history.replaceState) history.replaceState(null, '', `#/checklist?id=${encodeURIComponent(loaded.id)}`);
  }

  /* ---------- состояние ---------- */
  isDirty() { return !!this.s.loaded && !same(cleanConfig(this.s.draft), cleanConfig(this.s.loaded.config)); }
  refConfig() { return this.isDirty() ? this.s.loaded.config : (this.s.parent?.config || null); }
  refLabel() { return this.isDirty() ? `несохранённые изменения относительно v${this.s.loaded.version}` : (this.s.parent ? `отличия от родительской версии v${this.s.parent.version}` : ''); }
  refCrit(id) { return (this.refConfig()?.criteria || []).find((x) => x.id === id) || null; }
  critDiff(c) {
    const ref = this.refConfig();
    if (!ref) return { isNew: false, fields: [] };
    const r = this.refCrit(c.id);
    if (!r) return { isNew: true, fields: [] };
    return { isNew: false, fields: FIELDS.filter((f) => !same(normField(c, f), normField(r, f))) };
  }

  /* ---------- отрисовка ---------- */
  render() {
    this.innerHTML = `
      <div class="cqa-page-head">
        <div><div class="cqa-eyebrow">Контур 1 · Настройка</div><h1>Чек-лист</h1>
          <div class="cqa-page-sub">Критерии, формулировки, веса, типы ответа, N/A и шкалы. Каждое сохранение — новая версия; из неё модель получает и инструкции, и строгую схему ответа.</div></div>
      </div>
      <div class="cqa-editor-grid">
        <aside class="cqa-panel cqa-versions" data-part="versions">${this.versionsHtml()}</aside>
        <div class="cqa-editor" data-part="editor">${this.editorHtml()}</div>
      </div>`;
  }

  versionsHtml() {
    return `<div class="cqa-eyebrow" style="padding:8px 10px 4px">Версии</div>` + this.s.list.map((c) => `
      <button type="button" class="cqa-version" data-version="${c.id}" aria-selected="${String(c.id) === String(this.s.selId)}">
        <div class="cqa-version-top"><span class="cqa-version-v">v${esc(c.version)}</span><span class="cqa-faint" style="font-size:11.5px">${esc(fmtDateTime(c.created_at))}</span></div>
        <div class="cqa-version-name">${esc(c.name)}</div>
        <div class="cqa-version-note">${esc(c.note || (c.parent_id == null ? 'начальная версия' : ''))}${c.n_criteria != null ? ` · ${fmtInt(c.n_criteria)} ${plural(c.n_criteria, 'критерий', 'критерия', 'критериев')}` : ''}</div>
      </button>`).join('');
  }

  editorHtml() {
    const d = this.s.draft;
    const ref = this.refConfig();
    const headChanged = (f) => (ref && !same(String(d[f] ?? '').trim(), String(ref[f] ?? '').trim()) ? 'is-changed' : '');
    const deleted = ref ? (ref.criteria || []).filter((r) => !(d.criteria || []).some((c) => c.id === r.id)) : [];
    return `
      <div class="cqa-panel cqa-editor-head">
        <label class="cqa-field ${headChanged('name')}" data-wrap="name"><span class="cqa-label">Название чек-листа</span><input class="cqa-input" data-path="name" value="${esc(d.name)}"></label>
        <label class="cqa-field ${headChanged('description')}" data-wrap="description"><span class="cqa-label">Описание</span><input class="cqa-input" data-path="description" value="${esc(d.description || '')}"></label>
        <label class="cqa-field ${ref && +d.rounding !== +ref.rounding ? 'is-changed' : ''}" data-wrap="rounding"><span class="cqa-label">Округление итога</span>
          <select class="cqa-select" data-path="rounding">${[0, 1, 2, 3, 4].map((k) => `<option value="${k}" ${+d.rounding === k ? 'selected' : ''}>${k} ${plural(k, 'знак', 'знака', 'знаков')}</option>`).join('')}</select></label>
      </div>
      <div class="cqa-panel cqa-editor-summary" data-part="summary">${this.summaryHtml()}</div>
      ${ref ? `<div class="cqa-note ${this.isDirty() ? 'cqa-note--warn' : 'cqa-note--mint'}" data-part="diffnote">${icon('eye')} Подсвечены ${esc(this.refLabel())}.${deleted.length ? ` Удалены: ${deleted.map((r) => `<b>${esc(r.id)}</b> ${esc(r.title)}`).join(', ')}.` : ''}</div>` : ''}
      <div data-part="errors">${this.errorsHtml()}</div>
      <div data-part="criteria">${(d.criteria || []).map((c, i) => this.critHtml(c, i)).join('')}</div>
      <div class="cqa-row"><button type="button" class="cqa-btn" data-act="add-crit">${icon('plus')}Добавить критерий</button>
        <span class="cqa-faint" style="font-size:12.5px">Критерий без N/A и с весом &gt; 0 всегда входит в итог; «Грубая ошибка» отмечается отдельно и баллов не даёт.</span></div>
      ${this.rulesHtml()}
      ${this.schemaHtml()}
      <div data-part="apply">${this.applyHtml()}</div>
      <div class="cqa-savebar" data-part="savebar">${this.savebarHtml()}</div>`;
  }

  summaryHtml() {
    const cs = this.s.draft.criteria || [];
    const weighted = cs.filter((c) => c.type !== 'flag');
    const sum = weighted.reduce((a, c) => a + (+c.weight || 0), 0);
    const secs = new Set(cs.map((c) => (c.section || '').trim() || 'Без секции'));
    const v = this.s.validation;
    let status;
    if (this.s.validating) status = '<span class="cqa-validation"><span class="cqa-spinner"></span>Проверяю…</span>';
    else if (v && !v.ok) status = `<span class="cqa-validation cqa-validation--err">${icon('alert')}Ошибок: ${v.errors.length}</span>`;
    else if (v?.unchecked) status = `<span class="cqa-validation" style="color:#8a5a00">${icon('info')}Сервер проверки недоступен</span>`;
    else status = `<span class="cqa-validation cqa-validation--ok">${icon('check')}Чек-лист корректен</span>`;
    return `<span>Критериев: <b>${cs.length}</b></span><span>Секций: <b>${secs.size}</b></span>
      <span>Сумма весов: <b>${fmtPts(sum)}</b> <span class="cqa-faint">(итог всё равно приводится к 100)</span></span>
      <span>С N/A: <b>${cs.filter((c) => c.na && c.type !== 'flag').length}</b></span>
      <span>Грубые ошибки: <b>${cs.filter((c) => c.type === 'flag').length}</b></span>
      <span class="cqa-spacer"></span>${status}`;
  }

  errorsHtml() {
    const v = this.s.validation;
    if (!v || v.ok || !v.errors?.length) return '';
    return `<ul class="cqa-errors">${v.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`;
  }

  critHtml(c, i) {
    const open = this.s.open.has(c.id);
    const diff = this.critDiff(c);
    const ch = (f) => (diff.fields.includes(f) ? 'is-changed' : '');
    const cs = this.s.draft.criteria;
    const tags = this.tagsHtml(c, diff);
    const sections = [...new Set(cs.map((x) => x.section).filter(Boolean))];
    const body = !open ? '' : `<div class="cqa-ec-body">
      <label class="cqa-field cqa-span-4 ${ch('title')}"><span class="cqa-label">Название критерия</span><input class="cqa-input" data-path="c.${i}.title" value="${esc(c.title)}"></label>
      <label class="cqa-field cqa-span-2 ${ch('section')}"><span class="cqa-label">Секция</span><input class="cqa-input" data-path="c.${i}.section" value="${esc(c.section || '')}" list="cqa-sections-${i}"><datalist id="cqa-sections-${i}">${sections.map((s) => `<option value="${esc(s)}">`).join('')}</datalist></label>
      <div class="cqa-field cqa-span-3 ${ch('type')}"><span class="cqa-label">Тип ответа</span>
        <div class="cqa-segctl" role="group">${['bool', 'scale', 'flag'].map((t) => `<button type="button" data-type="${t}" data-i="${i}" aria-pressed="${c.type === t}">${TYPE_LABEL[t]}</button>`).join('')}</div></div>
      <label class="cqa-field ${ch('weight')}"><span class="cqa-label">Вес</span><input class="cqa-input" type="number" min="0" step="0.5" data-path="c.${i}.weight" value="${esc(c.type === 'flag' ? 0 : c.weight)}" ${c.type === 'flag' ? 'disabled title="Грубая ошибка не даёт баллов"' : ''}></label>
      <div class="cqa-field cqa-span-2 ${ch('na')}"><span class="cqa-label">N/A</span>
        <label class="cqa-switch" style="height:36px"><input type="checkbox" data-path="c.${i}.na" ${c.na && c.type !== 'flag' ? 'checked' : ''} ${c.type === 'flag' ? 'disabled' : ''}><span class="cqa-switch-track"></span>${c.type === 'flag' ? 'не бывает' : 'разрешён'}</label></div>
      <label class="cqa-field cqa-span-6 ${ch('yes')}"><span class="cqa-label">${c.type === 'scale' ? 'Как выставлять шкалу' : c.type === 'flag' ? '«Да» (нарушение) — когда' : '«Да» — когда'}</span><textarea class="cqa-textarea" rows="2" data-path="c.${i}.yes">${esc(c.yes || '')}</textarea></label>
      ${c.type !== 'scale' ? `<label class="cqa-field cqa-span-6 ${ch('no')}"><span class="cqa-label">«Нет» — когда <small>необязательно</small></span><textarea class="cqa-textarea" rows="2" data-path="c.${i}.no">${esc(c.no || '')}</textarea></label>` : ''}
      ${c.na && c.type !== 'flag' ? `<label class="cqa-field cqa-span-6 ${ch('na_when')}"><span class="cqa-label">N/A — когда <small>обязательно, если N/A разрешён</small></span><textarea class="cqa-textarea" rows="2" data-path="c.${i}.na_when">${esc(c.na_when || '')}</textarea></label>` : ''}
      ${c.type === 'scale' ? `<div class="cqa-field cqa-span-6 ${ch('steps')}"><span class="cqa-label">Ступени шкалы <small>верхняя ступень даёт полный вес; баллы = вес × ступень / верхняя</small></span>
        <div class="cqa-steps">${(c.steps || []).map((s, k) => `<div class="cqa-step-row">
          <input class="cqa-input cqa-num" type="number" min="0" step="1" data-path="c.${i}.steps.${k}.value" value="${esc(s.value)}" aria-label="Значение ступени">
          <input class="cqa-input" data-path="c.${i}.steps.${k}.text" value="${esc(s.text || '')}" placeholder="Когда ставится эта ступень" aria-label="Описание ступени">
          <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--icon cqa-btn--sm" data-act="del-step" data-i="${i}" data-k="${k}" title="Удалить ступень">${icon('x')}</button></div>`).join('')}
          <div><button type="button" class="cqa-btn cqa-btn--xs" data-act="add-step" data-i="${i}">${icon('plus')}Ступень</button></div></div></div>` : ''}
      <label class="cqa-field cqa-span-6 ${ch('guidance')}"><span class="cqa-label">Пояснение для модели <small>необязательно: трактовки, примеры</small></span><textarea class="cqa-textarea" rows="2" data-path="c.${i}.guidance">${esc(c.guidance || '')}</textarea></label>
      <div class="cqa-assist cqa-span-6">${this.assistHtml(c, i)}</div>
    </div>`;
    return `<div class="cqa-ec ${open ? 'is-open' : ''} ${diff.isNew ? 'is-new' : diff.fields.length ? 'is-changed' : ''}" data-crit="${esc(c.id)}" style="margin-bottom:8px">
      <div class="cqa-ec-head" data-act="toggle" data-id="${esc(c.id)}">
        <span class="cqa-ec-caret">${icon('right')}</span>
        <span class="cqa-crit-id">${esc(c.id)}</span>
        <span class="cqa-ec-title">${esc(c.title || 'Без названия')}<small>${esc(c.section || '')}</small></span>
        <span class="cqa-ec-tags">${tags}</span>
        <span class="cqa-ec-tools">
          <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--icon cqa-btn--sm" data-act="up" data-i="${i}" title="Выше" ${i === 0 ? 'disabled' : ''}>${icon('up')}</button>
          <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--icon cqa-btn--sm" data-act="down" data-i="${i}" title="Ниже" ${i === cs.length - 1 ? 'disabled' : ''}>${icon('down')}</button>
          <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--icon cqa-btn--sm" data-act="del" data-i="${i}" title="Удалить критерий" style="color:var(--cqa-no)">${icon('trash')}</button>
        </span>
      </div>${body}</div>`;
  }

  assistHtml(c, i) {
    const a = this.s.assists.get(c.id);
    const r = a?.result;
    return `<button type="button" class="cqa-btn cqa-btn--sm" data-act="assist" data-i="${i}" ${a?.loading ? 'disabled' : ''}>${a?.loading ? '<span class="cqa-spinner"></span>Готовлю подсказку…' : 'Подсказка ИИ'}</button>
      ${a ? `<div class="cqa-assist-result" role="region" aria-label="Подсказка ИИ для критерия ${esc(c.id)}" aria-live="polite" aria-busy="${!!a.loading}">
        ${a.loading ? '<p class="cqa-muted">Анализирую формулировку и другие критерии…</p>' : ''}
        ${a.error ? `<div class="cqa-error-box">${esc(a.error)}</div>` : ''}
        ${r ? `<div class="cqa-label">Предложенное пояснение</div><p class="cqa-assist-text">${esc(r.guidance)}</p>
          ${r.warnings?.length ? `<div class="cqa-label">Проблемы формулировки</div><ul class="cqa-errors">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : '<p class="cqa-muted">Проблем формулировки не найдено.</p>'}
          ${r.na_when_suggestion ? `<div class="cqa-label">Предложенное условие N/A</div><p class="cqa-assist-text">${esc(r.na_when_suggestion)}</p>` : ''}
          <p class="cqa-faint">Проверьте предложение перед вставкой. Изменения сохраняются кнопкой «Сохранить как новую версию».</p>` : ''}
        <div class="cqa-row">
          ${r ? `<button type="button" class="cqa-btn cqa-btn--sm" data-act="assist-guidance" data-i="${i}">Вставить в пояснение</button>` : ''}
          ${r?.na_when_suggestion ? `<button type="button" class="cqa-btn cqa-btn--sm" data-act="assist-na" data-i="${i}" ${!c.na || c.type === 'flag' ? 'disabled title="Сначала разрешите N/A у критерия"' : ''}>Заменить условие N/A</button>` : ''}
          <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-act="assist-close" data-i="${i}">Закрыть</button>
        </div>
      </div>` : ''}`;
  }

  refreshAssist(c) {
    const i = this.s.draft.criteria.indexOf(c);
    const box = this.querySelector(`.cqa-ec[data-crit="${CSS.escape(c.id)}"] .cqa-assist`);
    if (box && i >= 0) box.innerHTML = this.assistHtml(c, i);
  }

  async requestAssist(i) {
    const c = this.s.draft.criteria[i];
    if (!c || this.s.assists.get(c.id)?.loading) return;
    const a = { loading: true, result: null, error: null };
    const pending = this.s.assists;
    pending.set(c.id, a);
    this.refreshAssist(c);
    try {
      a.result = await this.api.assistChecklist({ config: clone(this.s.draft), criterion_id: c.id });
    } catch (e) { a.error = e.message; }
    a.loading = false;
    // Ответ закрытого блока или другой версии не возвращаем в редактор.
    if (this.alive && this.s.assists === pending && pending.get(c.id) === a && this.s.draft.criteria.includes(c)) this.refreshAssist(c);
  }

  rulesHtml() {
    const rules = this.s.draft.rules || [];
    const ref = this.refConfig();
    return `<div class="cqa-panel cqa-panel-pad">
      <div class="cqa-panel-title">${icon('list')}Общие правила для модели <span class="cqa-muted">памятка к чек-листу (стр. 3 PDF)</span></div>
      <div class="cqa-rules">${rules.map((r, k) => `<div class="cqa-rule-row ${ref && !(ref.rules || []).map((x) => x.trim()).includes(String(r).trim()) ? 'is-changed' : ''}">
        <span class="cqa-rule-n">${k + 1}</span><textarea class="cqa-textarea" rows="2" data-path="r.${k}">${esc(r)}</textarea>
        <button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--icon cqa-btn--sm" data-act="del-rule" data-k="${k}" title="Удалить правило">${icon('x')}</button></div>`).join('')}</div>
      <div style="margin-top:10px"><button type="button" class="cqa-btn cqa-btn--xs" data-act="add-rule">${icon('plus')}Правило</button></div>
    </div>`;
  }

  schemaHtml() {
    const cs = cleanConfig(this.s.draft).criteria;
    return `<details class="cqa-panel cqa-panel-pad cqa-details" data-part="schema">
      <summary>${icon('right')}Схема ответа модели — строится из чек-листа автоматически</summary>
      <p class="cqa-muted" style="margin:10px 0">По каждому критерию модель обязана вернуть обоснование, номера реплик-доказательств и один из допустимых ответов — другой ответ схема не пропустит. Баллы и итог считает код по формуле чек-листа.</p>
      <table class="cqa-table"><thead><tr><th>№</th><th>Критерий</th><th>Допустимые ответы</th><th class="cqa-col-right">Вес</th></tr></thead>
      <tbody>${cs.map((c) => `<tr><td class="cqa-cell-id">${esc(c.id)}</td><td>${esc(c.title)}</td><td>${allowedFor(c).map((a) => `<span class="cqa-badge cqa-badge--ghost cqa-mono" style="margin:1px">${esc(a)}</span>`).join(' ')}</td><td class="cqa-col-right cqa-num">${c.type === 'flag' ? 'без баллов' : fmtPts(c.weight)}</td></tr>`).join('')}</tbody></table>
    </details>`;
  }

  savebarHtml() {
    const dirty = this.isDirty();
    const v = this.s.validation;
    const bad = v && !v.ok;
    return `<span class="cqa-validation ${dirty ? '' : 'cqa-faint'}" style="font-weight:700">${dirty ? `${icon('edit')}Есть несохранённые изменения` : `${icon('check')}Версия v${esc(this.s.loaded.version)} · изменений нет`}</span>
      <input class="cqa-input cqa-input--sm" data-part="note" placeholder="Что изменили — видно в истории версий" value="${esc(this.s.note)}" ${dirty ? '' : 'disabled'}>
      <span class="cqa-spacer"></span>
      <button type="button" class="cqa-btn cqa-btn--sm" data-act="reset" ${dirty ? '' : 'disabled'}>Сбросить</button>
      <button type="button" class="cqa-btn cqa-btn--primary" data-act="save" ${dirty && !bad ? '' : 'disabled'} title="${bad ? 'Исправьте ошибки чек-листа' : ''}">${icon('check')}Сохранить как новую версию</button>`;
  }

  applyHtml() {
    const { datasets, meta, run, loaded, justSaved } = this.s;
    const providers = meta?.providers || [];
    const fin = run && isRunFinished(run);
    let progress = '';
    if (run) {
      const total = run.total || 0, done = run.done || 0, failed = run.failed || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      progress = `<div class="cqa-stack" style="gap:8px">
        <div class="cqa-run-stats">
          <span>${fin ? `${icon('check')} Готово` : '<span class="cqa-spinner"></span> Модель оценивает диалоги…'}</span>
          <span>Оценено <b>${fmtInt(done)}</b> из <b>${fmtInt(total)}</b></span>${failed ? `<span style="color:var(--cqa-no)">Ошибок <b>${fmtInt(failed)}</b></span>` : ''}
          <span>${icon('coins')} <b>${fmtMoney(run.cost_usd || 0)}</b></span>
          <span>Вызовов модели: <b>${fmtInt(run.model_calls || 0)}</b>; критериев оценено: <b>${fmtInt(run.evaluated || 0)}</b>, перенесено: <b>${fmtInt(run.reused || 0)}</b></span>
          <span>${icon('clock')} <b>${fmtClock(secondsBetween(run.created_at, fin ? run.finished_at : null))}</b></span>
        </div>
        <div class="cqa-progress ${fin ? '' : 'is-running'}"><div class="cqa-progress-fill" style="width:${pct}%"></div></div>
        ${fin ? `<div><a class="cqa-btn cqa-btn--primary cqa-btn--sm" href="#/?dataset=${encodeURIComponent(run.dataset_id || this.s.applyDataset || '')}">${icon('list')}Открыть результаты в списке диалогов</a></div>` : ''}
      </div>`;
    }
    return `<div class="cqa-apply" ${justSaved ? 'style="border-color:var(--cqa-green-500);box-shadow:var(--cqa-focus)"' : ''}>
      <div class="cqa-panel-title" style="margin:0">${icon('play')}Применить версию v${esc(loaded.version)} к набору диалогов
        ${justSaved ? '<span class="cqa-badge cqa-badge--yes">версия сохранена</span>' : ''}</div>
      <label class="cqa-row"><input type="checkbox" data-f="apply-full" ${this.s.full ? 'checked' : ''} ${run && !fin ? 'disabled' : ''}>Переоценить все критерии заново</label>
      ${this.planHtml()}
      <div class="cqa-row" style="align-items:flex-end">
        <label class="cqa-field"><span class="cqa-label">Набор</span><select class="cqa-select cqa-select--sm" data-f="apply-dataset" ${run && !fin ? 'disabled' : ''}>
          ${datasets.map((d) => `<option value="${esc(d.id)}" ${d.id === this.s.applyDataset ? 'selected' : ''}>${esc(d.id)} · ${fmtInt(d.total)} ${plural(d.total, 'диалог', 'диалога', 'диалогов')}</option>`).join('') || '<option value="">нет наборов — импортируйте в «Диалогах»</option>'}</select></label>
        <label class="cqa-field"><span class="cqa-label">Модель</span><select class="cqa-select cqa-select--sm" data-f="apply-provider" ${run && !fin ? 'disabled' : ''}>
          ${providers.map((p) => `<option value="${esc(p.id)}" ${p.id === this.s.applyProvider ? 'selected' : ''} ${p.available === false ? 'disabled' : ''}>${esc(p.label || p.id)}</option>`).join('')}</select></label>
        <button type="button" class="cqa-btn cqa-btn--primary" data-act="apply" ${!datasets.length || !this.s.plan || this.isDirty() || (run && !fin) ? 'disabled' : ''} title="${this.isDirty() ? 'Сначала сохраните изменения' : ''}">${icon('play')}Применить к набору</button>
        <button type="button" class="cqa-btn" data-act="try" ${!datasets.length || !this.s.applyProvider || this.s.trial.busy ? 'disabled' : ''} title="Проверить текущий черновик без сохранения на диалогах из загруженных наборов">${this.s.trial.busy ? '<span class="cqa-spinner"></span>Проверяю…' : `${icon('eye')}Проверить на 3 диалогах`}</button>
        ${this.isDirty() ? '<span class="cqa-faint" style="font-size:12.5px">для применения к набору сохраните изменения как новую версию</span>' : ''}
      </div>
      <div aria-live="polite">${this.tryHtml()}</div>
      ${progress}
      ${fin ? this.diffHtml() : ''}
    </div>`;
  }

  runBody() {
    return { dataset_id: this.s.applyDataset, checklist_id: +this.s.loaded.id, provider: this.s.applyProvider, mode: this.s.full ? 'full' : 'incremental' };
  }

  tryKey() { return JSON.stringify([cleanConfig(this.s.draft), this.s.applyProvider]); }

  async tryChecklist() {
    if (this.s.trial.busy) return;
    const seq = this.trySeq = (this.trySeq || 0) + 1;
    this.s.trial = { busy: true, key: this.tryKey() };
    this.refreshChrome();
    try {
      const result = await this.api.tryChecklist({ config: cleanConfig(this.s.draft), provider: this.s.applyProvider });
      if (!this.alive || seq !== this.trySeq) return;
      this.s.trial.result = result;
    } catch (e) {
      if (!this.alive || seq !== this.trySeq) return;
      this.s.trial.error = e.message;
    }
    if (!this.alive || seq !== this.trySeq) return;
    this.s.trial.busy = false;
    this.refreshChrome();
  }

  tryHtml() {
    const t = this.s.trial;
    if (t.busy) return '<p class="cqa-faint"><span class="cqa-spinner"></span>Оцениваю до 3 диалогов параллельно. Черновик и результаты не сохраняются.</p>';
    if (t.key && t.key !== this.tryKey()) return '<p class="cqa-faint">Чек-лист или модель изменились после проверки. Проверьте текущую правку ещё раз.</p>';
    if (t.error) return `<div class="cqa-error-box">Проверка не удалась: ${esc(t.error)}</div>`;
    const r = t.result;
    if (!r) return '';
    const value = (v) => v == null ? '—' : esc(v);
    return `<div class="cqa-panel cqa-panel-pad"><h3>Песочница: результаты проверки</h3>
      <p class="cqa-faint">${esc(r.model)} · ${fmtMoney(r.cost_usd)} · ${esc(r.latency_s)} с · без сохранения</p>
      ${r.items.map((item) => `<h4><a href="#/c/${encodeURIComponent(item.id)}">${esc(item.id)}</a> · ${esc(item.language)} · ${esc(item.channel)}</h4>
        ${item.error ? `<div class="cqa-error-box">Не удалось проверить: ${esc(item.error)}</div>` : ''}
        <div class="cqa-table-wrap"><table class="cqa-table"><thead><tr><th>Критерий</th><th>Было → стало</th><th>Обоснование</th><th>Реплики</th></tr></thead>
        <tbody>${item.criteria.map((c) => `<tr><td>${esc(c.id)} · ${esc(c.title)}</td><td>${value(c.previous_answer)} → <b>${value(c.answer)}</b></td><td>${esc(c.explanation)}</td><td>${c.evidence.map((n) => `#${esc(n)}`).join(', ') || '—'}</td></tr>`).join('')}</tbody></table></div>`).join('')}
    </div>`;
  }

  async loadPlan() {
    const seq = this.planSeq = (this.planSeq || 0) + 1;
    this.s.plan = null; this.s.planError = null;
    const refresh = () => { const box = this.part('apply'); if (box) box.innerHTML = this.applyHtml(); };
    refresh();
    if (!this.s.applyDataset || !this.s.applyProvider) return;
    try {
      const plan = await this.api.planRun(this.runBody());
      if (!this.alive || seq !== this.planSeq) return;
      this.s.plan = plan;
    } catch (e) {
      if (!this.alive || seq !== this.planSeq) return;
      this.s.planError = e.message;
    }
    refresh();
  }

  planHtml() {
    if (this.isDirty()) return '<p class="cqa-faint">Сохраните изменения — покажем план применения новой версии.</p>';
    if (this.s.planError) return `<div class="cqa-error-box">План недоступен: ${esc(this.s.planError)}</div>`;
    const p = this.s.plan;
    if (!p) return '<p class="cqa-faint">Загружаю план применения…</p>';
    const lines = [];
    if (p.mode === 'full') lines.push(`Все критерии оценим заново в ${fmtInt(p.total)} диалогах.`);
    else {
      for (const c of p.criteria) {
        if (c.definition_changed) lines.push(`Изменено определение критерия ${esc(c.id)} или общие инструкции — переоценим его в ${fmtInt(c.definition_changed)} диалогах.`);
        if (c.weight_changed) lines.push(`Критерий ${esc(c.id)}: изменён только вес — пересчёт в ${fmtInt(c.weight_changed)} диалогах без модели.`);
      }
      if (p.without_previous) lines.push(`Без прошлой успешной оценки этой моделью: ${fmtInt(p.without_previous)} диалогов — оценим полностью.`);
      if (!p.model_calls) lines.push('Определения не менялись — пересчитаем итоги без вызова модели.');
      else if (!lines.length) lines.push(`Оценим изменённые критерии в ${fmtInt(p.model_calls)} диалогах.`);
    }
    return `<div class="cqa-note cqa-note--mint"><div>${lines.map((l) => `<div>${l}</div>`).join('')}<div>План: вызовов модели ${fmtInt(p.model_calls)} (без повторов при ошибках), критериев оценим ${fmtInt(p.evaluated)}, перенесём ${fmtInt(p.reused)}.</div></div></div>`;
  }

  diffHtml() {
    if (this.s.diffError) return `<div class="cqa-error-box">Не удалось загрузить изменения: ${esc(this.s.diffError)}</div>`;
    const d = this.s.diff;
    if (!d) return '<p class="cqa-faint">Загружаю изменения…</p>';
    const value = (v) => v == null ? '—' : esc(v);
    const gross = (v) => v == null ? '—' : v ? 'да' : 'нет';
    return `<div class="cqa-panel cqa-panel-pad"><h3>Что изменилось</h3><p class="cqa-faint">Сравнение исходных ответов. Апелляции и решения проверяющих остаются у своих оценок.</p>
      <p>${d.criteria.filter((c) => c.changed).map((c) => `${esc(c.id)} · ${esc(c.title)}: изменилось ответов ${fmtInt(c.changed)}`).join('; ') || 'Ответы критериев не изменились.'}</p>
      <div class="cqa-table-wrap"><table class="cqa-table"><thead><tr><th>Диалог</th><th>Оценка</th><th>Итог было → стало</th><th>Грубая ошибка</th><th>Ответы было → стало</th></tr></thead>
      <tbody>${d.items.map((i) => `<tr><td><a href="#/c/${encodeURIComponent(i.conversation_id)}${i.current ? `?e=${encodeURIComponent(i.current.id)}` : ''}">${esc(i.conversation_id)}</a></td><td>${value(i.previous?.id)} → ${value(i.current?.id)}</td><td>${value(i.previous?.total)} → ${value(i.current?.total)}</td><td>${gross(i.previous?.gross)} → ${gross(i.current?.gross)}</td><td>${i.error ? esc(i.error) : i.changes.map((c) => `${esc(c.id)}: ${value(c.before)} → ${value(c.after)}`).join('; ') || 'без изменений'}</td></tr>`).join('')}</tbody></table></div></div>`;
  }

  refreshChrome() {
    const sum = this.part('summary'); if (sum) sum.innerHTML = this.summaryHtml();
    const err = this.part('errors'); if (err) err.innerHTML = this.errorsHtml();
    const bar = this.part('savebar');
    if (bar) {
      const focused = document.activeElement?.dataset?.part === 'note';
      bar.innerHTML = this.savebarHtml();
      if (focused) { const n = this.part('note'); n?.focus(); n?.setSelectionRange(n.value.length, n.value.length); }
    }
    const ap = this.part('apply'); if (ap) ap.innerHTML = this.applyHtml();
  }

  /** Полная перерисовка редактора с сохранением прокрутки страницы. */
  rerenderEditor() {
    const y = window.scrollY;
    const ed = this.part('editor');
    if (ed) ed.innerHTML = this.editorHtml();
    window.scrollTo(0, y);
  }

  /** Подсветка изменённых полей без перерисовки (чтобы не терять фокус при вводе). */
  refreshDiffMarks(i) {
    const c = this.s.draft.criteria[i];
    const card = c && this.querySelector(`.cqa-ec[data-crit="${CSS.escape(c.id)}"]`);
    if (!card) return;
    const diff = this.critDiff(c);
    card.classList.toggle('is-changed', !diff.isNew && diff.fields.length > 0);
    card.querySelectorAll('[data-path]').forEach((inp) => {
      const f = inp.dataset.path.split('.')[2];
      const wrap = inp.closest('.cqa-field');
      if (wrap && FIELDS.includes(f)) wrap.classList.toggle('is-changed', diff.fields.includes(f));
    });
    const title = card.querySelector('.cqa-ec-title');
    if (title) title.innerHTML = `${esc(c.title || 'Без названия')}<small>${esc(c.section || '')}</small>`;
    const tags = card.querySelector('.cqa-ec-tags');
    if (tags) tags.innerHTML = this.tagsHtml(c, diff);
  }

  tagsHtml(c, diff = this.critDiff(c)) {
    return [
      `<span class="cqa-badge cqa-badge--ghost">${esc(TYPE_LABEL[c.type] || c.type)}${c.type === 'scale' ? ` ${normSteps(c.steps).map((s) => s.value).reverse().join('/')}` : ''}</span>`,
      c.type === 'flag' ? '<span class="cqa-badge cqa-badge--no">без баллов</span>' : `<span class="cqa-badge cqa-badge--mint">вес ${fmtPts(c.weight)}</span>`,
      c.na && c.type !== 'flag' ? '<span class="cqa-badge cqa-badge--na">N/A</span>' : '',
      diff.isNew ? '<span class="cqa-badge cqa-badge--yes">новый</span>' : '',
      diff.fields.length ? `<span class="cqa-badge cqa-badge--warn" title="${esc(diff.fields.map((f) => FIELD_LABEL[f]).join(', '))}">изменён: ${esc(diff.fields.map((f) => FIELD_LABEL[f]).join(', '))}</span>` : '',
    ].join('');
  }

  /* ---------- проверка и сохранение ---------- */
  async validate() {
    this.s.validating = true;
    const sum = this.part('summary'); if (sum) sum.innerHTML = this.summaryHtml();
    try {
      const res = await this.api.validateChecklist(cleanConfig(this.s.draft));
      this.s.validation = { ok: !!res?.ok, errors: res?.errors || [] };
    } catch (e) {
      this.s.validation = e.status === 422 && e.body?.errors ? { ok: false, errors: e.body.errors } : { ok: true, errors: [], unchecked: true };
    }
    this.s.validating = false;
    if (this.alive) this.refreshChrome();
  }

  async save() {
    const btn = this.querySelector('[data-act="save"]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="cqa-spinner"></span>Сохраняю…'; }
    const note = (this.part('note')?.value || this.s.note || '').trim() || 'Изменения в интерфейсе';
    try {
      const res = await this.api.saveChecklist({ config: cleanConfig(this.s.draft), note, parent_id: this.s.loaded.id });
      const id = res?.id;
      toast(`Сохранена версия v${res?.version ?? '?'} — теперь её можно применить к набору`);
      this.s.list = ((await this.api.checklists()) || []).sort((a, b) => b.id - a.id);
      this.s.run = null;
      await this.open(id);
      this.s.justSaved = id;
      const ap = this.part('apply'); if (ap) { ap.innerHTML = this.applyHtml(); ap.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      emit('cqa-data-changed', { checklist: id });
    } catch (e) {
      const errs = e.body?.errors || e.body?.detail?.errors;
      if (errs) { this.s.validation = { ok: false, errors: errs }; this.refreshChrome(); }
      toast(`Не сохранено: ${e.message}`, 'error', 7000);
      if (btn) { btn.disabled = false; btn.innerHTML = `${icon('check')}Сохранить как новую версию`; }
    }
  }

  async apply() {
    const dataset_id = this.s.applyDataset;
    if (!dataset_id) { toast('Выберите набор', 'warn'); return; }
    try {
      this.s.diff = null; this.s.diffError = null;
      const res = await this.api.startRun(this.runBody());
      const runId = res?.run_id ?? res?.id;
      this.s.run = { id: runId, status: 'running', total: this.s.datasets.find((d) => d.id === dataset_id)?.total || 0, done: 0, failed: 0, cost_usd: 0, created_at: new Date().toISOString(), dataset_id };
      const ap = this.part('apply'); if (ap) ap.innerHTML = this.applyHtml();
      this.stopPoll?.();
      this.stopPoll = pollRun(this.api, runId, (run, err) => {
        if (!this.alive || err || !run) return;
        this.s.run = { ...run, dataset_id };
        const box = this.part('apply'); if (box) box.innerHTML = this.applyHtml();
        if (isRunFinished(run)) {
          this.api.runDiff(run.id).then((diff) => {
            if (!this.alive || this.s.run?.id !== run.id) return;
            this.s.diff = diff;
            const box = this.part('apply'); if (box) box.innerHTML = this.applyHtml();
          }).catch((e) => {
            if (!this.alive || this.s.run?.id !== run.id) return;
            this.s.diffError = e.message;
            const box = this.part('apply'); if (box) box.innerHTML = this.applyHtml();
          });
          this.loadPlan();
          toast(`Чек-лист v${this.s.loaded.version} применён: ${fmtInt(run.done)} ${plural(run.done, 'оценка', 'оценки', 'оценок')} · ${fmtMoney(run.cost_usd || 0)}`, 'info', 6000);
          emit('cqa-data-changed', { run: run.id });
        }
      });
      this.onCleanup(() => this.stopPoll?.());
    } catch (e) { toast(`Не удалось запустить: ${e.message}`, 'error', 7000); }
  }

  /* ---------- события ---------- */
  onClick(e) {
    const t = e.target;
    const ver = t.closest('[data-version]');
    if (ver) {
      if (this.isDirty() && !window.confirm('Есть несохранённые изменения. Открыть другую версию и отбросить их?')) return;
      this.s.justSaved = null; this.s.run = null;
      this.open(ver.dataset.version).catch((er) => toast(er.message, 'error'));
      return;
    }
    const typeBtn = t.closest('[data-type]');
    if (typeBtn) { this.setType(+typeBtn.dataset.i, typeBtn.dataset.type); return; }
    const actEl = t.closest('[data-act]');
    if (!actEl || actEl.disabled) return;
    const act = actEl.dataset.act;
    const cs = this.s.draft.criteria;
    const i = actEl.dataset.i != null ? +actEl.dataset.i : null;
    if (act === 'assist') { this.requestAssist(i); return; }
    if (act === 'assist-close') { this.s.assists.delete(cs[i].id); this.refreshAssist(cs[i]); return; }
    if (act === 'assist-guidance' || act === 'assist-na') {
      const r = this.s.assists.get(cs[i].id)?.result;
      if (!r) return;
      if (act === 'assist-guidance') cs[i].guidance = r.guidance;
      else if (cs[i].na && cs[i].type !== 'flag' && r.na_when_suggestion) cs[i].na_when = r.na_when_suggestion;
      this.changed(true);
      return;
    }
    if (act === 'toggle') {
      if (t.closest('.cqa-ec-tools')) return;
      const id = actEl.dataset.id;
      if (this.s.open.has(id)) this.s.open.delete(id); else this.s.open.add(id);
      this.rerenderEditor();
      return;
    }
    if (act === 'up' && i > 0) { [cs[i - 1], cs[i]] = [cs[i], cs[i - 1]]; this.changed(true); }
    else if (act === 'down' && i < cs.length - 1) { [cs[i + 1], cs[i]] = [cs[i], cs[i + 1]]; this.changed(true); }
    else if (act === 'del') { this.s.assists.delete(cs[i].id); cs.splice(i, 1); this.changed(true); }
    else if (act === 'add-crit') this.addCriterion();
    else if (act === 'add-step') { (cs[i].steps ||= []).push({ value: 0, text: '' }); this.changed(true); }
    else if (act === 'del-step') { cs[i].steps.splice(+actEl.dataset.k, 1); this.changed(true); }
    else if (act === 'add-rule') { (this.s.draft.rules ||= []).push(''); this.changed(true); setTimeout(() => { const r = this.querySelectorAll('[data-path^="r."]'); r[r.length - 1]?.focus(); }, 0); }
    else if (act === 'del-rule') { this.s.draft.rules.splice(+actEl.dataset.k, 1); this.changed(true); }
    else if (act === 'reset') { this.s.assists = new Map(); this.s.draft = clone(this.s.loaded.config); this.s.note = ''; this.s.validation = { ok: true, errors: [] }; this.rerenderEditor(); }
    else if (act === 'save') this.save();
    else if (act === 'apply') this.apply();
    else if (act === 'try') this.tryChecklist();
  }

  addCriterion() {
    const cs = this.s.draft.criteria;
    const nums = cs.map((c) => parseInt(c.id, 10)).filter((n) => !Number.isNaN(n));
    let id = String((nums.length ? Math.max(...nums) : 0) + 1).padStart(2, '0');
    while (cs.some((c) => c.id === id)) id = `${id}a`;
    const lastWeighted = [...cs].reverse().find((c) => c.type !== 'flag');
    const c = { id, section: lastWeighted?.section || 'Новая секция', title: 'Новый критерий', type: 'bool', weight: 10, na: false, yes: '', no: '', na_when: '', steps: [], guidance: '' };
    const flagIdx = cs.findIndex((x) => x.type === 'flag');
    if (flagIdx >= 0) cs.splice(flagIdx, 0, c); else cs.push(c);
    this.s.open.add(id);
    this.changed(true);
    setTimeout(() => { const el = this.querySelector(`.cqa-ec[data-crit="${CSS.escape(id)}"]`); el?.scrollIntoView({ behavior: 'smooth', block: 'center' }); el?.querySelector('[data-path$=".title"]')?.select(); }, 30);
  }

  setType(i, type) {
    const c = this.s.draft.criteria[i];
    if (!c || c.type === type) return;
    c.type = type;
    if (type === 'flag') { c.weight = 0; c.na = false; }
    else if (!(+c.weight > 0)) c.weight = 10;
    if (type === 'scale' && !(c.steps || []).length) c.steps = [{ value: 10, text: '' }, { value: 5, text: '' }, { value: 0, text: '' }];
    this.changed(true);
  }

  onInput(e, isChange = false) {
    const t = e.target;
    if (t.dataset.part === 'note') { this.s.note = t.value; return; }
    if (t.dataset.f?.startsWith('apply-')) {
      if (!isChange) return;
      if (t.dataset.f === 'apply-dataset') this.s.applyDataset = t.value;
      if (t.dataset.f === 'apply-provider') this.s.applyProvider = t.value;
      if (t.dataset.f === 'apply-full') this.s.full = t.checked;
      this.loadPlan(); return;
    }
    const path = t.dataset.path;
    if (!path) return;
    const discrete = t.type === 'checkbox' || t.tagName === 'SELECT';
    if (isChange !== discrete) return; // текст — по input, переключатели и списки — по change
    const d = this.s.draft;
    const p = path.split('.');
    let structural = false;
    if (p[0] === 'c') {
      const c = d.criteria[+p[1]];
      if (!c) return;
      if (p[2] === 'steps') {
        const s = c.steps[+p[3]];
        s[p[4]] = p[4] === 'value' ? (t.value === '' ? '' : Number(t.value)) : t.value;
      } else if (p[2] === 'na') { c.na = t.checked; structural = true; }
      else if (p[2] === 'weight') c.weight = t.value === '' ? '' : Number(t.value);
      else c[p[2]] = t.value;
      if (!structural) { this.refreshDiffMarks(+p[1]); if (p[2] === 'weight') this.refreshTags(+p[1]); }
    } else if (p[0] === 'r') {
      d.rules[+p[1]] = t.value;
    } else if (p[0] === 'rounding') {
      d.rounding = Number(t.value);
    } else {
      d[p[0]] = t.value;
      const wrap = this.querySelector(`[data-wrap="${p[0]}"]`);
      const ref = this.refConfig();
      if (wrap && ref) wrap.classList.toggle('is-changed', String(d[p[0]] ?? '').trim() !== String(ref[p[0]] ?? '').trim());
    }
    this.changed(structural);
  }

  refreshTags(i) {
    const c = this.s.draft.criteria[i];
    const tags = c && this.querySelector(`.cqa-ec[data-crit="${CSS.escape(c.id)}"] .cqa-ec-tags`);
    if (tags) tags.innerHTML = this.tagsHtml(c);
  }

  changed(structural) {
    this.s.justSaved = null;
    if (structural) this.rerenderEditor(); else this.refreshChrome();
    this.validateSoon();
  }
}

customElements.define('cqa-checklist-editor', ChecklistEditor);
