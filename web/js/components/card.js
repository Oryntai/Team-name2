// <cqa-card conversation-id="…" [evaluation-id="…"] [embedded]> — карточка диалога: транскрипт + результат оценки,
// переход от обоснования к реплике (и к секунде записи звонка), апелляция оператора.
import { CqaElement } from '../base.js';
import { APPEAL_REASONS, appealReasonLabel } from '../appeal-reasons.js';
import { INSIGHT_FLAGS, SENTIMENT, insightBusy } from '../insights.js';
import {
  esc, icon, fmtScore, fmtPts, fmtDateTime, fmtMoney, fmtSeconds, fmtInt, fmtTs, fmtClock, answerTone, answerLabel,
  evidenceLabel, scoreTone, pctTone, linkifyRefs, isCall, CHANNEL_LABEL, ROLE_LABEL, toast, getRole, providerLabel,
  checklistLabel, navigate, emit, pollRun, isRunFinished, plural, PROVIDER_DESC } from '../util.js';

const TONE_COLOR = { yes: 'var(--cqa-yes)', ok: 'var(--cqa-yes)', mid: '#d9a03a', no: 'var(--cqa-no)', na: 'var(--cqa-na)', gross: 'var(--cqa-gross)' };
const checklistCache = new Map();

class Card extends CqaElement {
  init() {
    this.convId = this.getAttribute('conversation-id');
    this.evalId = this.getAttribute('evaluation-id');
    this.s = { insight: null, insightError: null, insightOpen: false, insightStarting: false, conv: null, ev: null, cfg: null, meta: null, checklists: [], evals: [], appealFor: null, active: null, rerunOpen: false, run: null, pop: null, thinkingTab: 0,
      translation: null, translationUsage: null, translationBusy: false, showTranslation: false,
      blind: false, calAnswers: {}, comparison: null, calBusy: false, doubts: new Map() };
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Открываю диалог…</div>';
    this.addEventListener('click', (e) => this.onClick(e));
    this.addEventListener('submit', (e) => this.onSubmit(e));
    this.addEventListener('change', (e) => this.onChange(e));
    this.addEventListener('mouseover', (e) => this.onHover(e, true));
    this.addEventListener('mouseout', (e) => this.onHover(e, false));
    // Esc закрывает окно критерия; Enter/пробел на строке критерия — открывает
    this.listen(document, 'keydown', (e) => {
      if (e.key === 'Escape' && this.s.pop) this.closePop();
      const row = e.target.closest?.('.cqa-crit');
      if (row && (e.key === 'Enter' || e.key === ' ') && this.contains(row)) { e.preventDefault(); this.openFromRow(row.dataset.crit); }
    });
    this.listen(window, 'cqa-role', () => this.renderResults());
    this.onCleanup(() => clearTimeout(this.insightTimer));
    this.load();
  }

  async load() {
    try {
      const [conv, meta, checklists] = await Promise.all([this.api.conversation(this.convId), this.api.meta(), this.api.checklists()]);
      this.s.conv = conv; this.s.meta = meta; this.s.checklists = checklists || [];
      if (this.canTranslate() && !this.api.mock) {
        try {
          const saved = await this.api.translation(this.convId);
          this.s.translationUsage = saved;
          this.s.translation = saved.translation;
          this.s.showTranslation = !!saved.translation;
        } catch (e) { if (this.alive) toast(e.message, 'error'); }
      }
      this.s.evals = [...(conv.evaluations || [])].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || (b.evaluation_id - a.evaluation_id));
      const id = this.evalId || this.s.evals[0]?.evaluation_id;
      if (id != null) await this.loadEval(id);
      await this.loadInsight();
      if (!this.alive) return;
      this.render();
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-card-head"><a class="cqa-back" href="#/">${icon('back')}Диалоги</a></div>
        <div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось открыть диалог «${esc(this.convId)}»: ${esc(e.message)}</div></div>`;
    }
  }

  async loadInsight() {
    clearTimeout(this.insightTimer);
    if (this.api.mock) return;
    try {
      const row = await this.api.insight(this.convId);
      if (!this.alive) return;
      this.s.insight = row;
      this.s.insightError = null;
      if (insightBusy(row)) this.insightTimer = setTimeout(() => this.pollInsight(), 1000);
    } catch (e) {
      this.s.insightError = e.message;
    }
  }

  async pollInsight() {
    if (!this.alive) return;
    await this.loadInsight();
    if (!this.alive) return;
    this.renderResults();
    // Сетевой сбой не обрывает наблюдение за уже оплаченным запросом.
    if (this.s.insightError && insightBusy(this.s.insight)) this.insightTimer = setTimeout(() => this.pollInsight(), 2000);
  }

  async startInsight() {
    if (this.s.insightStarting || insightBusy(this.s.insight)) return;
    this.s.insightStarting = true;
    this.renderResults();
    try {
      await this.api.startInsights({ conversation_ids: [this.convId] });
      await this.loadInsight();
      if (this.s.insightError) this.insightTimer = setTimeout(() => this.pollInsight(), 1000);
    } catch (e) { toast(e.message, 'error'); }
    finally { this.s.insightStarting = false; if (this.alive) this.renderResults(); }
  }

  insightHtml() {
    if (this.api.mock) return '';
    const row = this.s.insight, d = row?.data;
    const busy = this.s.insightStarting || insightBusy(row);
    const hasEvaluation = this.s.evals.some((e) => e.status === 'ok');
    const button = `<button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-act="insight-start" ${busy || !hasEvaluation ? 'disabled' : ''}>${icon('spark')}Разобрать диалог</button>`;
    if (!row || d?.status !== 'ok') return `<section class="cqa-insight" aria-label="Разбор" aria-live="polite">
      <div class="cqa-row"><h2>Разбор</h2>${busy ? '<span class="cqa-status"><span class="cqa-spinner"></span>Разбираю…</span>' : button}
      ${row && !busy ? `<span class="cqa-faint">${fmtMoney(row.cost_usd)}</span>` : ''}</div>
      ${this.s.insightError || d?.error ? `<p class="cqa-muted">${esc(this.s.insightError || d.error)}</p>` : ''}
      ${busy ? '' : `<span class="cqa-faint">${hasEvaluation ? 'Один вызов · ≈ 0,1 цента' : 'Сначала оцените диалог'}</span>`}</section>`;
    const stale = row.evaluation_id !== this.s.ev?.id;
    const outdated = row.evaluation_id !== this.s.evals.find((e) => e.status === 'ok')?.evaluation_id;
    const flags = d.flags.map((f) => `<span class="cqa-insight-flag" title="${esc(f.reason)}"><span>${esc(INSIGHT_FLAGS[f.code])}</span>
      ${f.turns.map((n) => `<a class="cqa-ref" href="#" data-ref="${n}" aria-label="${esc(INSIGHT_FLAGS[f.code])}, реплика ${n}">#${n}</a>`).join(' ')}</span>`).join('');
    return `<section class="cqa-insight ${this.s.insightOpen ? 'is-expanded' : ''}" aria-label="Разбор">
      <div class="cqa-row"><h2>Разбор</h2><span class="cqa-faint">${fmtMoney(row.cost_usd)}</span>
      ${stale ? `<span class="cqa-faint">По оценке №${row.evaluation_id}</span>` : ''}
      ${outdated ? '<button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--xs" data-act="insight-start">Обновить</button>' : ''}</div>
      <button type="button" class="cqa-insight-summary" data-act="insight-toggle" aria-expanded="${this.s.insightOpen}" aria-controls="insight-detail">
        <span>${esc(d.summary)}</span>${icon('down')}</button>
      <div class="cqa-row cqa-insight-meta"><b>${esc(d.topic)}</b><span class="cqa-muted" title="Настроение клиента в начале → в конце">${esc(SENTIMENT[d.sentiment.start])} → ${esc(SENTIMENT[d.sentiment.end])}</span></div>
      ${flags ? `<div class="cqa-row cqa-insight-flags">${flags}</div>` : ''}
      <div class="cqa-insight-detail" id="insight-detail" ${this.s.insightOpen ? '' : 'inert'}><div>
        <p>${esc(d.reason)}</p>
        ${d.flags.map((f) => `<p><b>${esc(INSIGHT_FLAGS[f.code])}:</b> ${esc(f.reason)}</p>`).join('')}
        <p class="cqa-faint">${esc(row.model)} · ${fmtInt(row.input_tokens)} / ${fmtInt(row.output_tokens)} токенов · ${esc(fmtDateTime(row.created_at))}</p>
      </div></div></section>`;
  }

  coachingHtml(cid) {
    const row = this.s.insight;
    if (row?.data?.status !== 'ok' || row.evaluation_id !== this.s.ev?.id) return '';
    const c = row.data.coaching.find((x) => x.criterion_id === cid);
    if (!c) return '';
    return `<div class="cqa-coaching"><p><b>Как можно было:</b> ${esc(c.rewrite)} <a href="#" class="cqa-ref" data-ref="${c.turn}">#${c.turn}</a></p><p class="cqa-muted">${esc(c.tip)}</p></div>`;
  }

  async loadEval(id) {
    const ev = await this.api.evaluation(id);
    this.s.ev = ev;
    this.s.cfg = null;
    const cid = ev?.checklist?.id;
    if (cid != null) {
      try {
        if (!checklistCache.has(cid)) checklistCache.set(cid, this.api.checklist(cid));
        this.s.cfg = (await checklistCache.get(cid))?.config || null;
      } catch { checklistCache.delete(cid); }
    }
  }

  async reloadEval() {
    if (!this.s.ev) return;
    const keep = this.part('results')?.scrollTop || 0;
    await this.loadEval(this.s.ev.id);
    if (!this.alive) return;
    this.renderResults();
    const r = this.part('results'); if (r) r.scrollTop = keep;
  }

  /* ---------- отрисовка ---------- */
  render() {
    const { conv } = this.s;
    const call = isCall(conv.channel);
    const n = conv.transcript?.length || 0;
    const evOpts = !this.s.blind && this.s.evals.length > 1
      ? `<label class="cqa-inline-field"><span class="cqa-label">Оценка</span><select class="cqa-select cqa-select--sm" data-f="eval">${this.s.evals.map((e) => `<option value="${e.evaluation_id}" ${e.evaluation_id === this.s.ev?.id ? 'selected' : ''}>${esc(shortLabel(e.checklist_label))} · ${esc(providerLabel(this.s.meta, e.provider, e.model))} · ${esc(fmtDateTime(e.created_at))}${e.final_total != null || e.total != null ? ` · ${fmtScore(e.final_total ?? e.total)}` : ''}</option>`).join('')}</select></label>`
      : '';
    this.innerHTML = `
      <div class="cqa-card-head">
        ${this.hasAttribute('embedded') ? '' : `<a class="cqa-back" href="#/${conv.dataset_id ? `?dataset=${encodeURIComponent(conv.dataset_id)}` : ''}">${icon('back')}Диалоги</a>`}
        <div class="cqa-card-title">
          <h1>${esc(conv.id)}</h1>
          <div class="cqa-card-facts">
            <span class="cqa-channel">${icon(call ? 'phone' : 'chat')}${CHANNEL_LABEL(conv.channel)}</span>
            <span class="cqa-lang">${esc((conv.language || '—').toUpperCase())}</span>
            <span>${fmtInt(n)} ${plural(n, 'реплика', 'реплики', 'реплик')}</span>
            ${conv.dataset_id ? `<span>· набор ${esc(conv.dataset_id)}</span>` : ''}
          </div>
        </div>
        <div class="cqa-card-actions">
          ${evOpts}
          ${this.s.blind ? '' : `<span title="${this.calibrationReady() ? 'Ответы ИИ будут скрыты до сравнения' : 'Нужна успешная оценка ИИ по выбранному чек-листу'}"><button type="button" class="cqa-btn" data-act="cal-start" ${this.calibrationReady() ? '' : 'disabled'}>${icon('edit')}Оценить вслепую</button></span>
          <button type="button" class="cqa-btn" data-act="rerun-toggle" aria-expanded="${this.s.rerunOpen}">${icon('refresh')}Переоценить</button>`}
        </div>
      </div>
      <div data-part="rerun">${this.s.rerunOpen ? this.rerunHtml() : ''}</div>
      <div class="cqa-card-grid">
        <section class="cqa-panel cqa-transcript-panel cqa-col-scroll" aria-label="Транскрипт">
          ${this.transcriptHeadHtml()}
          ${call ? this.playerHtml() : ''}
          <div class="cqa-transcript" data-part="transcript">${this.transcriptHtml()}</div>
        </section>
        <section class="cqa-results cqa-col-scroll" data-part="results" aria-label="Результат оценки">${this.resultsHtml()}</section>
      </div>`;
    this.bindAudio();
    this.renderMarkers();
    this.applyHighlight();
  }

  renderResults() {
    const el = this.part('results');
    if (!el) return;
    el.innerHTML = this.resultsHtml();
    this.renderMarkers();
    if (this.s.pop) this.renderPop(); else this.applyHighlight();
  }

  transcriptHeadHtml() {
    const pii = (this.s.conv.transcript || []).reduce((a, m) => a + (m.pii?.length || 0), 0);
    return `<div class="cqa-transcript-head">
      <h2>Транскрипт</h2>
      <span class="cqa-spacer"></span>
      <span data-part="translation-tools">${this.translationToolsHtml()}</span>
      ${pii ? `<span class="cqa-badge cqa-badge--warn" title="Эти фрагменты заменены метками до отправки модели">${icon('lock')}скрыто от модели: ${pii}</span>` : ''}
      <span class="cqa-badge cqa-badge--ghost" title="Реплики бота и системные события модель видит как контекст, но оценивает только оператора">оценивается только оператор</span>
    </div>`;
  }

  canTranslate() { return (this.s.conv?.language || '').trim().toLowerCase() !== 'ru'; }

  translationToolsHtml() {
    if (!this.canTranslate()) return '';
    const cost = this.s.translationUsage?.model_calls
      ? `<span class="cqa-muted cqa-translation-cost" title="Стоимость всех попыток перевода">Перевод · ${esc(fmtMoney(this.s.translationUsage.cost_usd))}</span>` : '';
    if (this.s.translation) {
      return `<span class="cqa-row"><span class="cqa-segctl cqa-translation-switch" role="group" aria-label="Перевод диалога">
        <button type="button" data-act="translation-original" aria-pressed="${!this.s.showTranslation}">Оригинал</button>
        <button type="button" data-act="translation-show" aria-pressed="${this.s.showTranslation}">С переводом</button>
      </span>${cost}</span>`;
    }
    return `<span class="cqa-row"><button type="button" class="cqa-btn cqa-btn--sm cqa-btn--ghost" data-act="translate"
      ${this.s.translationBusy || this.api.mock ? 'disabled' : ''} title="${this.api.mock ? 'Перевод доступен на сервере' : 'Claude Haiku 5.5 · ПДн скрыты · только для чтения'}">
      ${this.s.translationBusy ? '<span class="cqa-spinner"></span>Перевожу…' : icon('spark') + 'Перевести на русский'}</button>${cost}</span>`;
  }

  translationHtml(n) {
    const t = this.s.translation?.data.find((item) => item.turn === n);
    return `<div class="cqa-translation ${this.s.showTranslation && t ? 'is-visible' : ''}" data-translation="${n}"
      aria-hidden="${!this.s.showTranslation || !t}"><div lang="ru">${esc(t?.text || '')}</div></div>`;
  }

  updateTranslation() {
    const focused = this.part('translation-tools').contains(document.activeElement) ? document.activeElement.dataset.act : null;
    this.part('translation-tools').innerHTML = this.translationToolsHtml();
    if (focused) this.part('translation-tools').querySelector(`[data-act="${focused}"]`)?.focus({ preventScroll: true });
    for (const el of this.querySelectorAll('[data-translation]')) {
      const t = this.s.translation?.data.find((item) => item.turn === +el.dataset.translation);
      el.firstElementChild.textContent = t?.text || '';
      el.classList.toggle('is-visible', this.s.showTranslation && !!t);
      el.setAttribute('aria-hidden', String(!this.s.showTranslation || !t));
    }
  }

  async translate() {
    if (this.s.translationBusy || this.s.translation) return;
    this.s.translationBusy = true;
    this.updateTranslation();
    try {
      const saved = await this.api.translate(this.convId);
      if (!this.alive) return;
      this.s.translationUsage = saved;
      this.s.translation = saved.translation;
      this.s.showTranslation = true;
    } catch (e) {
      if (!this.alive) return;
      toast(e.message, 'error');
      // Даже невалидный ответ модели мог стоить денег.
      try { this.s.translationUsage = await this.api.translation(this.convId); } catch { /* исходная ошибка уже показана */ }
    } finally {
      this.s.translationBusy = false;
      if (this.alive) this.updateTranslation();
    }
  }

  playerHtml() {
    const src = this.s.conv.audio_src;
    if (!src) return `<div class="cqa-player"><span class="cqa-player-label">${icon('phone')}Запись звонка</span><span class="cqa-muted">Запись недоступна</span></div>`;
    return `<div class="cqa-player">
      <span class="cqa-player-label">${icon('phone')}Запись звонка</span>
      <audio controls preload="metadata" src="${esc(src)}" data-part="audio"></audio>
      <span class="cqa-player-now" data-part="now">клик по №реплики — переход</span>
    </div>`;
  }

  textWithPii(m) {
    const text = String(m.text ?? '');
    const spans = (m.pii || []).filter((s) => s && s.end > s.start).sort((a, b) => a.start - b.start);
    if (!spans.length) return esc(text);
    let out = '', pos = 0;
    for (const s of spans) {
      if (s.start < pos) continue;
      out += esc(text.slice(pos, s.start));
      out += `<mark class="cqa-pii" title="Скрыто от модели: она видела «${esc(s.label || '[скрыто]')}»">${icon('lock')}${esc(text.slice(s.start, s.end))}</mark>`;
      pos = s.end;
    }
    return out + esc(text.slice(pos));
  }

  transcriptHtml() {
    const items = this.s.conv.transcript || [];
    if (!items.length) return '<div class="cqa-empty">Транскрипт пуст</div>';
    return items.map((m) => {
      const role = ['client', 'operator', 'bot', 'system'].includes(m.speaker) ? m.speaker : 'system';
      const text = this.textWithPii(m);
      const seek = m.audio_offset != null ? `<button type="button" class="cqa-link" data-seek="${m.n}" title="Прослушать с этого места">${icon('play')}${fmtClock(m.audio_offset)}</button>` : '';
      if (role === 'system') {
        return `<div class="cqa-msg cqa-msg--system" data-n="${m.n}"><div class="cqa-bubble"><span class="cqa-sys-n">#${m.n}</span>${text}<span class="cqa-msg-ts cqa-faint" title="${esc(m.timestamp)}">${esc(fmtTs(m.timestamp))}</span>${seek}</div>${this.translationHtml(m.n)}</div>`;
      }
      return `<div class="cqa-msg cqa-msg--${role}" data-n="${m.n}">
        <div class="cqa-msg-meta"><span class="cqa-msg-n">#${m.n}</span><span class="cqa-msg-role">${ROLE_LABEL[role]}</span><span class="cqa-msg-ts" title="${esc(m.timestamp)}">${esc(fmtTs(m.timestamp))}</span>${seek}</div>
        <div class="cqa-bubble">${text}</div>${this.translationHtml(m.n)}</div>`;
    }).join('');
  }

  rerunHtml(inline = false) {
    const { checklists, meta, ev } = this.s;
    const avail = (meta?.providers || []);
    const curC = ev?.checklist?.id ?? [...checklists].sort((a, b) => b.id - a.id)[0]?.id;
    const curP = ev?.provider && avail.some((p) => p.id === ev.provider && p.available !== false) ? ev.provider : (meta?.default_provider || avail[0]?.id);
    const run = this.s.run;
    const status = run
      ? (isRunFinished(run) ? '' : `<span class="cqa-status cqa-status--run"><span class="cqa-spinner"></span>Модель оценивает диалог…</span>`)
      : '';
    return `<div class="${inline ? '' : 'cqa-panel cqa-panel-pad'}" style="${inline ? '' : 'margin-bottom:14px'}">
      <div class="cqa-row" style="align-items:flex-end">
        <label class="cqa-field"><span class="cqa-label">Чек-лист</span><select class="cqa-select cqa-select--sm" data-f="rerun-checklist">
          ${[...checklists].sort((a, b) => b.id - a.id).map((c) => `<option value="${c.id}" ${c.id === curC ? 'selected' : ''}>${esc(checklistLabel(c))}</option>`).join('')}</select></label>
        <label class="cqa-field"><span class="cqa-label">Модель</span><select class="cqa-select cqa-select--sm" data-f="rerun-provider">
          ${avail.map((p) => `<option value="${esc(p.id)}" data-desc="${esc(PROVIDER_DESC[p.id] || '')}" ${p.id === curP ? 'selected' : ''} ${p.available === false ? 'disabled' : ''}>${esc(p.label || p.id)}${p.available === false ? ' — не настроена' : ''}</option>`).join('')}</select></label>
        <button type="button" class="cqa-btn cqa-btn--primary" data-act="rerun-start" ${run && !isRunFinished(run) ? 'disabled' : ''}>${icon('play')}${ev ? 'Оценить заново' : 'Оценить диалог'}</button>
        <span data-part="rerun-status">${status}</span>
      </div>
      ${inline ? '' : '<div class="cqa-faint" style="font-size:12px;margin-top:8px">Модель оценит изменённые определения; остальные ответы перенесём. Прежние оценки доступны в списке «Оценка».</div>'}
    </div>`;
  }

  resultsHtml() {
    if (this.s.blind) return this.calibrationHtml();
    if (this.s.comparison) return this.comparisonHtml();
    return this.callMetricsHtml() + this.evaluationHtml();
  }

  calibrationReady() {
    return this.s.ev?.status === 'ok' && !!this.s.ev.original && !!this.s.cfg;
  }

  calibrationHtml() {
    const cfg = this.s.cfg;
    const count = Object.keys(this.s.calAnswers).length;
    return `<form data-form="calibration" class="cqa-calibration">
      <div class="cqa-row"><h2>Оценка вслепую</h2><span class="cqa-spacer"></span><button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-act="cal-close">Отмена</button></div>
      <p class="cqa-muted">${esc(this.s.ev.checklist.label)} · <span data-part="cal-count">${count} из ${cfg.criteria.length}</span></p>
      ${cfg.rules?.length ? `<details class="cqa-details"><summary>${icon('info')}Правила</summary><ul>${cfg.rules.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></details>` : ''}
      ${cfg.criteria.map((c) => {
        const allowed = this.s.ev.criteria.find((x) => x.id === c.id)?.allowed || [];
        const value = this.s.calAnswers[c.id];
        return `<div class="cqa-calibration-question">
          <details class="cqa-details"><summary><span class="cqa-crit-id">${esc(c.id)}</span>${esc(c.title)}</summary>
            <div class="cqa-calibration-definition">
              <p>${c.type === 'scale' ? '' : '<b>Да: </b>'}${esc(c.yes)}</p>
              ${c.no ? `<p><b>Нет: </b>${esc(c.no)}</p>` : ''}
              ${(c.steps || []).map((s) => `<p><b>${esc(s.value)}: </b>${esc(s.text)}</p>`).join('')}
              ${c.na ? `<p><b>N/A: </b>${esc(c.na_when)}</p>` : ''}
              ${c.guidance ? `<p>${esc(c.guidance)}</p>` : ''}
            </div>
          </details>
          <select class="cqa-select cqa-select--sm" data-cal-criterion="${esc(c.id)}" aria-label="Ответ: ${esc(c.title)}">
            <option value="" disabled ${value == null ? 'selected' : ''}>Ваш ответ</option>
            ${allowed.map((a) => `<option value="${esc(a)}" ${a === value ? 'selected' : ''}>${esc(a)}</option>`).join('')}
          </select>
        </div>`;
      }).join('')}
      <details class="cqa-details cqa-calibration-signature"><summary>${icon('edit')}Подпись и комментарий</summary><div>
        <label class="cqa-field"><span class="cqa-label">Имя</span><input class="cqa-input" name="reviewer" maxlength="200" autocomplete="name"></label>
        <label class="cqa-field"><span class="cqa-label">Комментарий</span><textarea class="cqa-textarea" name="comment" maxlength="5000"></textarea></label>
      </div></details>
      <button type="submit" class="cqa-btn cqa-btn--primary" data-part="cal-submit" ${count === cfg.criteria.length ? '' : 'disabled'}>Сравнить с ИИ</button>
    </form>`;
  }

  comparisonHtml() {
    const result = this.s.comparison;
    const matched = result.criteria.filter((c) => c.matched).length;
    const n = this.s.conv.transcript?.length || 0;
    return `<div class="cqa-calibration" aria-live="polite">
      <div class="cqa-row"><h2>Ваш ${fmtScore(result.human.total)} · ИИ ${fmtScore(result.model.total)}</h2><span class="cqa-spacer"></span><button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-act="cal-close">К оценке</button></div>
      <p class="cqa-muted">Совпало ${matched} из ${result.criteria.length} · ИИ №${result.evaluation_id} · сохранено${result.reviewer ? ` · ${esc(result.reviewer)}` : ''}</p>
      ${result.human.gross || result.model.gross ? `<p class="cqa-muted">Грубая ошибка: вы — ${result.human.gross ? 'Да' : 'Нет'} · ИИ — ${result.model.gross ? 'Да' : 'Нет'}</p>` : ''}
      <div class="cqa-calibration-table"><table class="cqa-table"><thead><tr><th>Критерий</th><th>Ваш</th><th>ИИ</th><th aria-label="Совпало">=</th></tr></thead>
        <tbody>${result.criteria.map((c) => `<tr><td>${c.matched ? `${esc(c.id)} · ${esc(c.title)}` : `<details class="cqa-details"><summary>${esc(c.id)} · ${esc(c.title)}</summary><div class="cqa-calibration-definition"><p>${linkifyRefs(c.explanation, n)}</p><div class="cqa-crit-ev">${this.chipsHtml(c.evidence)}</div></div></details>`}</td>
          <td>${esc(c.human_answer)}</td><td>${esc(c.model_answer)}</td><td class="cqa-calibration-match" data-matched="${c.matched}" aria-label="${c.matched ? 'Совпало' : 'Не совпало'}">${icon(c.matched ? 'check' : 'x')}</td></tr>`).join('')}</tbody>
      </table></div>
    </div>`;
  }

  startCalibration() {
    if (!this.calibrationReady()) return;
    this.closePop();
    this.s.blind = true;
    this.s.calAnswers = {};
    this.s.comparison = null;
    this.s.rerunOpen = false;
    this.render();
    this.part('results')?.scrollTo(0, 0);
  }

  toggleCalibrationDetails(details) {
    const body = details.lastElementChild;
    const opening = !details.open || details.dataset.closing === '1';
    details._calAnimation?.cancel();
    details.dataset.closing = opening ? '0' : '1';
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) { details.open = opening; return; }
    details.open = true;
    const frames = [{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }];
    const animation = body.animate(opening ? frames : [...frames].reverse(), {
      duration: opening ? 180 : 144, easing: 'cubic-bezier(.16, 1, .3, 1)',
    });
    details._calAnimation = animation;
    animation.finished.then(() => { if (!opening) details.open = false; }).catch(() => {});
  }

  async submitCalibration(form) {
    if (this.s.calBusy || Object.keys(this.s.calAnswers).length !== this.s.cfg.criteria.length) return;
    const data = new FormData(form);
    this.s.calBusy = true;
    form.querySelectorAll('button, select, input, textarea').forEach((el) => { el.disabled = true; });
    try {
      const result = await this.api.createCalibration({ conversation_id: this.convId, checklist_id: this.s.ev.checklist.id,
        answers: this.s.calAnswers, reviewer: data.get('reviewer'), comment: data.get('comment') });
      if (!this.alive) return;
      this.s.blind = false;
      this.s.comparison = result;
      this.render();
    } catch (e) {
      if (!this.alive) return;
      form.querySelectorAll('button, select, input, textarea').forEach((el) => { el.disabled = false; });
      toast(`Не удалось сохранить: ${e.message}`, 'error');
    } finally { this.s.calBusy = false; }
  }

  callMetricsHtml() {
    const m = this.s.conv.call_metrics;
    if (!isCall(this.s.conv.channel) || !m) return '';
    const ref = (n) => `<a href="#" class="cqa-ref" data-ref="${n}">#${n}</a>`;
    const interval = (h) => {
      const start = this.s.conv.transcript[h.start_n - 1];
      const seek = start?.audio_offset != null
        ? `<button type="button" class="cqa-link" data-seek="${h.start_n}" title="Прослушать с этого места">${icon('play')}${fmtClock(start.audio_offset)}</button>` : '';
      return `<li>${ref(h.start_n)} → ${h.end_n == null ? 'конец разговора (не закрыто)' : ref(h.end_n)} · ${fmtSeconds(h.duration_s)} ${seek}</li>`;
    };
    const share = (s) => s.share == null ? '—' : `${Math.round(s.share * 100)}%`;
    const pace = (s) => s.words_per_minute == null ? '—' : `${Math.round(s.words_per_minute)} слов/мин`;
    const p = m.response_pauses;
    return `<div class="cqa-panel cqa-panel-pad cqa-callmetrics" aria-label="Звонок по меткам времени">
      <h2>Звонок по меткам времени</h2>
      <p class="cqa-muted">оценка по меткам времени расшифровки, не по звуку</p>
      <div class="cqa-row"><span>Длительность <b>${fmtClock(m.duration_s)}</b></span><span>Удержания <b>${fmtSeconds(m.hold_s)}</b></span></div>
      ${m.holds.length ? `<ul>${m.holds.map(interval).join('')}</ul>` : ''}
      <p title="Доля от суммарного оценочного времени речи оператора и клиента">Доля речи: оператор <b>${share(m.speech.operator)}</b> · клиент <b>${share(m.speech.client)}</b></p>
      <p title="Только реплики от трёх слов с положительным оценочным временем">Темп: оператор <b>${pace(m.speech.operator)}</b> · клиент <b>${pace(m.speech.client)}</b></p>
      <p>Паузы перед ответом: медиана <b>${fmtSeconds(p.median_s)}</b> · максимум <b>${fmtSeconds(p.max_s)}</b></p>
      <p class="cqa-faint">Между началами соседних реплик клиента и оператора, включая речь клиента.</p>
      ${p.long.length ? `<details><summary>Дольше 10 с: ${p.long.length}</summary><ul>${p.long.map((v) => `<li>${ref(v.client_n)} → ${ref(v.operator_n)} · ${fmtSeconds(v.duration_s)}</li>`).join('')}</ul></details>` : ''}
      ${m.recording_pauses.length ? `<details><summary>Паузы записи: ${fmtSeconds(m.recording_pause_s)}</summary><ul>${m.recording_pauses.map(interval).join('')}</ul></details>` : ''}
      ${m.unknown_events.length ? `<details><summary>Прочие системные события: ${m.unknown_events.length}</summary><ul>${m.unknown_events.map((e) => `<li>${ref(e.n)} ${esc(e.text)}</li>`).join('')}</ul></details>` : ''}
      ${(m.warnings || []).map((w) => `<p class="cqa-muted">${esc(w)}</p>`).join('')}
    </div>`;
  }

  evaluationHtml() {
    const { ev } = this.s;
    if (!ev) {
      return `<div class="cqa-panel"><div class="cqa-empty"><div class="cqa-empty-icon">${icon('spark')}</div><h3>Диалог ещё не оценён</h3>
        <p>Выберите чек-лист и модель — оценка займёт несколько секунд.</p></div><div style="padding:0 20px 20px">${this.rerunHtml(true)}</div></div>${this.insightHtml()}`;
    }
    if (ev.status === 'error') {
      return `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Оценка не удалась: ${esc(ev.error || 'неизвестная ошибка')}</div>
        <div style="margin-top:14px">${this.rerunHtml(true)}</div></div>${this.insightHtml()}`;
    }
    const fin = ev.final || ev.original || {};
    const orig = ev.original || fin;
    const crits = ev.criteria || [];
    const changedCrit = crits.filter((c) => c.final_answer != null && c.final_answer !== c.original?.answer);
    const totalChanged = orig.total !== fin.total;
    return `
      ${this.grossHtml(fin)}
      ${this.heroHtml(orig, fin, crits, totalChanged || changedCrit.length > 0)}
      ${this.insightHtml()}
      ${this.metaHtml()}
      ${this.criteriaHtml(crits)}`;
  }

  thinkingDialogHtml() {
    const runs = this.s.ev?.thinking || [];
    const i = Math.min(this.s.thinkingTab, Math.max(0, runs.length - 1));
    const n = this.s.conv.transcript?.length || 0;
    const para = (t) => (t || '').trim().split(/\n{2,}/).filter(Boolean).map((x) => `<p>${linkifyRefs(x, n)}</p>`).join('') || '<p class="cqa-faint">Модель не вернула размышления для этой оценки.</p>';
    return `<div class="cqa-dialog-head"><span class="cqa-empty-icon" style="width:36px;height:36px;margin:0;border-radius:10px">${icon('spark')}</span><h2>Ход мысли модели</h2>
        <button type="button" class="cqa-pop-x" data-act="thinking-close" aria-label="Закрыть">${icon('x')}</button></div>
      <div class="cqa-dialog-body">
        <p class="cqa-muted" style="margin:0 0 12px">Модель оценивает диалог три раза независимо; ответ по каждому критерию — по большинству. Перед ответом она рассуждает — ниже пересказ этих рассуждений (на английском, рабочем языке модели).</p>
        ${runs.length > 1 ? `<div class="cqa-segctl cqa-tabs" role="tablist">${runs.map((_, k) => `<button type="button" role="tab" data-act="thinking-tab" data-i="${k}" aria-pressed="${k === i}">Оценка ${k + 1}</button>`).join('')}</div>` : ''}
        <div class="cqa-thinking-run">${para(runs[i])}</div>
      </div>`;
  }

  heroHtml(orig, fin, crits, changed) {
    const weighted = crits.filter((c) => c.type !== 'flag' && (c.weight || 0) > 0);
    const applicable = weighted.filter((c) => (c.score_final || c.score_original)?.applicable);
    const naList = weighted.filter((c) => !(c.score_final || c.score_original)?.applicable).map((c) => c.id);
    let caption;
    if (fin.total == null) caption = 'Итог не рассчитывается: нет применимых критериев с весом';
    else caption = `Учтено ${applicable.length} из ${weighted.length} ${plural(weighted.length, 'критерия', 'критериев', 'критериев')} с баллами${naList.length ? ` · N/A: ${naList.join(', ')} — вес перераспределён` : ''}`;
    let delta = '';
    if (changed && orig.total != null && fin.total != null) {
      const d = fin.total - orig.total;
      delta = `<div class="cqa-hero-delta"><span class="cqa-muted">до апелляций</span><s class="cqa-num cqa-muted">${fmtScore(orig.total)}</s>
        ${Math.abs(d) > 1e-9 ? `<span class="cqa-delta ${d > 0 ? 'cqa-delta--up' : 'cqa-delta--down'}">${d > 0 ? '+' : '−'}${fmtScore(Math.abs(d))}</span>` : '<span class="cqa-muted">без изменений</span>'}
        <span class="cqa-badge cqa-badge--info">${icon('gavel')}учтено решение проверяющего</span></div>`;
    }
    const secs = (fin.sections || []).filter((s) => (s.ids || []).some((id) => crits.find((c) => c.id === id && c.type !== 'flag')));
    const bars = secs.map((s) => (s.percent == null
      ? `<div class="cqa-sec"><span class="cqa-sec-name" title="${esc(s.name)}">${esc(s.name)}</span><div class="cqa-bar cqa-bar--na"></div><span class="cqa-sec-val"><small>N/A — не учитывается</small></span></div>`
      : `<div class="cqa-sec"><span class="cqa-sec-name" title="${esc(s.name)}">${esc(s.name)}</span>
        <div class="cqa-bar"><div class="cqa-bar-fill" data-tone="${pctTone(s.percent)}" style="width:${Math.max(0, Math.min(100, s.percent))}%"></div></div>
        <span class="cqa-sec-val" title="Баллы секции после перераспределения / её максимум">${fmtScore(s.percent)}%<small>${fmtPts(s.points_eff)} из ${fmtPts(s.max_eff)}</small></span></div>`)).join('');
    return `<div class="cqa-panel cqa-hero">
      <div class="cqa-eyebrow">Итоговый балл</div>
      <div class="cqa-hero-top">
        <div><div class="cqa-hero-score"><span class="cqa-hero-num" data-tone="${scoreTone(fin.total)}">${fmtScore(fin.total)}</span><span class="cqa-hero-of">/ 100</span></div>
          <button type="button" class="cqa-link cqa-score-link" data-act="score-open" aria-haspopup="dialog">Как посчитан итог</button></div>
        <div class="cqa-hero-side">${delta}<div class="cqa-hero-caption">${esc(caption)}</div></div>
      </div>
      ${bars ? `<div class="cqa-sections" aria-label="Результаты секций">${bars}</div>` : ''}
      ${this.s.ev.precedents?.length ? `<p class="cqa-hero-caption">Учтены похожие оценённые диалоги: ${this.s.ev.precedents.map((p) => `<a href="#/c/${encodeURIComponent(p.conversation_id)}">${esc(p.conversation_id)}</a>${p.source === 'reviewer' ? ' (решение проверяющего)' : ''}`).join(', ')}</p>` : ''}
    </div>`;
  }

  scoreDialogHtml() {
    const ev = this.s.ev;
    const fin = ev.final || ev.original || {};
    return `<div class="cqa-dialog-head"><h2 id="cqa-score-title">Как посчитан итог</h2>
        <button type="button" class="cqa-pop-x" data-act="score-close" aria-label="Закрыть">${icon('x')}</button></div>
      <div class="cqa-dialog-body">
        <div class="cqa-score-table"><table class="cqa-table"><thead><tr><th>Критерий</th><th>Ответ</th><th>Вес, %</th><th>После N/A, %</th><th>Баллы</th></tr></thead>
          <tbody>${(ev.criteria || []).map((c) => {
            const sc = c.score_final || c.score_original || {};
            const counted = c.type !== 'flag' && sc.applicable;
            return `<tr><td>${esc(c.id)} · ${esc(c.title)}</td><td>${esc(c.final_answer ?? c.original?.answer ?? '—')}</td>
              <td>${fmtPts(c.weight)}</td><td>${counted ? fmtPts(sc.weight_eff) : '—'}</td>
              <td>${c.type === 'flag' ? 'без баллов' : counted ? fmtPts(sc.points_eff) : 'исключён'}</td></tr>`;
          }).join('')}</tbody></table></div>
        <p class="cqa-muted">N/A исключены, веса применимых пересчитаны до 100</p>
        <div class="cqa-score-formula">${fin.total == null ? 'Итог не рассчитывается: нет применимых критериев с весом' : `итог = сумма баллов / сумма весов применимых × 100 = <b>${fmtScore(fin.total)}</b>`}</div>
        <details><summary class="cqa-link">По секциям</summary><div class="cqa-score-sections">${(fin.sections || []).map((s) => `<div class="cqa-row"><span>${esc(s.name)}</span><span class="cqa-num">${fmtPts(s.points_eff)} / ${fmtPts(s.max_eff)} · ${s.percent == null ? 'N/A' : `${fmtScore(s.percent)}%`}</span></div>`).join('')}</div></details>
      </div>`;
  }

  openScoreDialog() {
    let d = this.part('score-dialog');
    if (!d) {
      d = document.createElement('dialog');
      d.className = 'cqa-dialog cqa-score-dialog';
      d.dataset.part = 'score-dialog';
      d.setAttribute('aria-labelledby', 'cqa-score-title');
      d.addEventListener('cancel', (e) => { e.preventDefault(); this.closeScoreDialog(); });
      d.addEventListener('click', (e) => { if (e.target === d) this.closeScoreDialog(); });
      this.appendChild(d);
    }
    d.classList.remove('is-closing');
    d.innerHTML = this.scoreDialogHtml();
    if (!d.open) d.showModal();
  }

  closeScoreDialog() {
    const d = this.part('score-dialog');
    if (!d?.open || d.classList.contains('is-closing')) return;
    d.classList.add('is-closing');
    const close = () => { d.close(); d.classList.remove('is-closing'); };
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) close();
    else setTimeout(close, 144);
  }

  grossHtml(fin) {
    if (!fin.gross) return '';
    const flagged = (this.s.ev.criteria || []).filter((c) => c.type === 'flag' && (c.final_answer ?? c.original?.answer) === 'Да');
    const n = this.s.conv.transcript?.length || 0;
    return `<div class="cqa-gross" role="alert">
      <div class="cqa-gross-icon">${icon('alert')}</div>
      <div style="min-width:0;flex:1">
        <div class="cqa-gross-title">${flagged.length === 1 && /груб/i.test(flagged[0].title) ? 'Грубая ошибка' : `Грубая ошибка: ${flagged.map((c) => esc(c.title)).join(', ') || 'отмечена'}`}</div>
        ${flagged.map((c) => `<div class="cqa-gross-text">${linkifyRefs(c.original?.explanation || '', n)}</div>
          ${(c.original?.evidence || []).length ? `<div class="cqa-crit-ev">${this.chipsHtml(c.original.evidence)}</div>` : ''}`).join('')}
        <div class="cqa-gross-text" style="margin-top:6px;font-size:12px;opacity:.85">Отмечается отдельно и не обнуляет итоговый балл (правило чек-листа).</div>
      </div>
    </div>`;
  }

  metaHtml() {
    const ev = this.s.ev;
    const pii = ev.pii_masked?.length || 0;
    return `<div class="cqa-evalmeta">
      <span title="Модель">${icon('cpu')}<b>${esc(providerLabel(this.s.meta, ev.provider, ev.model))}</b></span>
      <span title="Чек-лист">${icon('checklist')}${esc(checklistLabel(ev.checklist))}</span>
      <span title="Когда и сколько длилась оценка">${icon('clock')}${esc(fmtDateTime(ev.created_at))} · ${fmtSeconds(ev.latency_s)}</span>
      <span title="Токены: вход → выход">${icon('spark')}${fmtInt(ev.input_tokens)} → ${fmtInt(ev.output_tokens)} ток.</span>
      <span title="Стоимость этой оценки">${icon('coins')}<b>${fmtMoney(ev.cost_usd)}</b>&nbsp;за оценку</span>
      ${pii ? `<span title="Личные данные заменены метками до отправки модели">${icon('lock')}скрыто от модели: ${pii}</span>` : ''}
      ${(ev.thinking || []).some((t) => t && t.trim()) ? `<button type="button" class="cqa-link cqa-think-btn" data-act="thinking-open">${icon('spark')}Ход мысли модели</button>` : ''}
      <span class="cqa-faint">оценка №${esc(ev.id)}${ev.prompt_version ? ` · промпт ${esc(ev.prompt_version)}` : ''}</span>
    </div>`;
  }

  criteriaHtml(crits) {
    return `<p class="cqa-crit-hint">Нажмите на критерий или на значок у реплики — откроется объяснение</p>
      <div class="cqa-tiles">${crits.map((c) => this.critHtml(c)).join('')}</div>`;
  }

  critDef(id) { return this.s.cfg?.criteria?.find((x) => x.id === id) || null; }

  critView(c) {
    const def = this.critDef(c.id);
    const crit = { ...c, steps: def?.steps };
    const orig = c.original || {};
    const fa = c.final_answer ?? orig.answer;
    const tone = answerTone(crit, fa);
    const changed = fa !== orig.answer;
    const sc = c.score_final || c.score_original || {};
    const scored = c.type !== 'flag' && sc.applicable;
    const pts = c.type === 'flag' ? { main: 'без баллов', sub: 'отмечается отдельно' }
      : !sc.applicable ? { main: 'не учитывается', sub: `вес ${fmtPts(c.weight)} → другим` }
        : { main: `${fmtPts(sc.points_eff)} из ${fmtPts(sc.weight_eff)}`, sub: `вес ${fmtPts(c.weight)}`, val: sc.points_eff, max: sc.weight_eff };
    const answer = changed
      ? `<span class="cqa-row" style="gap:6px;flex-wrap:nowrap"><span class="cqa-answer cqa-answer--sm cqa-answer--old" data-tone="${answerTone(crit, orig.answer)}" title="Ответ модели">${esc(answerLabel(crit, orig.answer))}</span><span class="cqa-arrow">→</span><span class="cqa-answer" data-tone="${tone}" title="Решение проверяющего">${esc(answerLabel(crit, fa))}</span></span>`
      : `<span class="cqa-answer" data-tone="${tone}">${esc(answerLabel(crit, fa))}</span>`;
    const m = /^(\d+)\/(\d+)$/.exec(orig.agreement || '');
    const split = m && +m[1] < +m[2] ? (orig.votes || []) : null;
    // Сомнение модели открыто, пока по критерию нет решения проверяющего (ответ на вопрос или апелляция).
    const decided = (c.appeals || []).some((a) => a.status !== 'open');
    const doubt = !!split && !decided && (c.positions || []).length > 1;
    return { def, crit, orig, fa, tone, changed, scored, pts, answer, split, decided, doubt };
  }

  critHtml(c) {
    const v = this.critView(c);
    const def = v.def;
    const pts = c.type === 'flag' ? '' : v.scored ? fmtPts(v.pts.val) : '—';
    const ans = `<span class="cqa-answer cqa-answer--sm" data-tone="${v.tone}">${esc(answerLabel(v.crit, v.fa))}</span>`;
    return `<div class="cqa-crit cqa-tile ${this.s.pop?.crit === c.id ? 'is-active' : ''}" role="button" tabindex="0" aria-haspopup="dialog" data-tone="${v.tone}" data-crit="${esc(c.id)}"
        title="${esc(`${c.title} — ${answerLabel(v.crit, v.fa)}${v.scored ? ` · ${v.pts.main} (вес ${fmtPts(c.weight)})` : ''}`)}">
      <span class="cqa-crit-id">${esc(c.id)}</span>
      <span class="cqa-tile-title">${esc(c.title)}</span>
      ${v.doubt ? `<span class="cqa-tile-flag cqa-tile-flag--ask" title="${esc(`Модель спрашивает: три её оценки разошлись (${v.split.join(' · ')})`)}">${icon('help')}</span>`
        : v.split ? `<span class="cqa-tile-flag" title="${esc(`Три оценки модели разошлись (${v.split.join(' · ')}); решение принял проверяющий`)}">≠</span>` : ''}
      ${c.appeal?.status === 'open' ? `<span class="cqa-tile-flag cqa-tile-flag--warn" title="Апелляция на рассмотрении">${icon('gavel')}</span>` : ''}
      ${v.changed ? `<span class="cqa-tile-flag cqa-tile-flag--info" title="Изменено проверяющим">${icon('edit')}</span>` : ''}
      ${ans}
      <span class="cqa-tile-pts">${pts}</span>
    </div>`;
  }

  popHtml(cid) {
    const c = (this.s.ev?.criteria || []).find((x) => x.id === cid);
    if (!c) return '';
    const v = this.critView(c);
    const n = this.s.conv.transcript?.length || 0;
    const def = v.def;
    const stepText = !def ? '' : v.fa === 'N/A' ? (def.na_when || '') : def.type === 'scale' ? ((def.steps || []).find((st) => String(st.value) === String(v.fa))?.text || '') : '';
    const ev = v.orig.evidence || [];
    return `<div class="cqa-pop-head"><span class="cqa-crit-id">${esc(c.id)}</span><b>${esc(c.title)}</b>
        <button type="button" class="cqa-pop-x" data-act="pop-close" aria-label="Закрыть">${icon('x')}</button></div>
      <div class="cqa-pop-verdict">${v.answer}<span class="cqa-pop-pts">${esc(v.pts.main)}</span></div>
      ${stepText ? `<div class="cqa-crit-step">${esc(stepText)}</div>` : ''}
      ${v.doubt ? this.doubtHtml(c, v, n) : v.split && !v.decided ? `<p class="cqa-pop-split">${icon('info')}<span>Модель оценила диалог трижды и разошлась: ${esc(v.split.join(' · '))}. Итог — по большинству; стоит проверить.</span></p>` : ''}
      ${v.orig.explanation ? `<p class="cqa-pop-text">${linkifyRefs(v.orig.explanation, n)}</p>` : ''}
      ${ev.length ? `<div class="cqa-crit-ev"><span class="cqa-ev-label">${esc(evidenceLabel(v.crit, v.orig.answer))}</span>${this.chipsHtml(ev)}</div>` : ''}
      ${c.reused_from != null ? `<p class="cqa-faint">Перенесено из оценки №${esc(c.reused_from)}: определение не менялось.</p>` : ''}
      ${this.coachingHtml(c.id)}
      ${this.appealHtml(c, v.crit)}`;
  }

  renderMarkers() {
    this.querySelectorAll('.cqa-mark-row').forEach((el) => el.remove());
    if (this.s.blind || this.s.comparison) return;
    const crits = this.s.ev?.status === 'ok' ? (this.s.ev.criteria || []) : [];
    const by = new Map();
    for (const c of crits) {
      for (const k of new Set((c.original?.evidence || []).map(Number))) {
        if (!by.has(k)) by.set(k, []);
        by.get(k).push(c);
      }
    }
    for (const [k, list] of by) {
      const msg = this.querySelector(`.cqa-transcript [data-n="${k}"]`);
      const host = msg?.querySelector('.cqa-msg-meta') || msg?.querySelector('.cqa-bubble');
      if (!host) continue;
      const row = document.createElement('span');
      row.className = 'cqa-mark-row';
      row.innerHTML = list.map((c) => {
        const v = this.critView(c);
        return `<button type="button" class="cqa-mark ${this.s.pop?.crit === c.id ? 'is-on' : ''}" data-mark="${esc(c.id)}" data-n="${k}" data-tone="${v.tone}" title="${esc(`${c.id} · ${c.title} — ${answerLabel(v.crit, v.fa)}`)}">${esc(c.id)}</button>`;
      }).join('');
      host.appendChild(row);
    }
  }

  openPop(cid, n = null) {
    this.s.pop = { crit: cid, n };
    this.s.active = cid;
    if (this.s.appealFor !== cid) this.s.appealFor = null;
    this.renderPop();
  }

  openFromRow(cid) {
    const c = (this.s.ev?.criteria || []).find((x) => x.id === cid);
    const first = (c?.original?.evidence || []).map(Number).find((k) => this.querySelector(`.cqa-mark[data-mark="${cid}"][data-n="${k}"]`));
    if (this.s.pop?.crit === cid) { this.closePop(); return; }
    this.openPop(cid, first ?? null);
  }

  closePop() {
    this.s.pop = null;
    this.s.active = null;
    this.s.appealFor = null;
    const pop = this.querySelector('.cqa-pop:not(.is-closing)');
    if (pop) { pop.classList.add('is-closing'); setTimeout(() => pop.remove(), matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 144); }
    this.querySelectorAll('.cqa-mark.is-on, .cqa-crit.is-active').forEach((el) => el.classList.remove('is-on', 'is-active'));
    this.highlight(null);
  }

  /** Окно критерия — часть ленты: раскрывается прямо под репликой со значком и прокручивается вместе с ней. */
  renderPop() {
    this.querySelectorAll('.cqa-pop').forEach((el) => el.remove());
    if (!this.s.pop) return;
    const pop = document.createElement('div');
    pop.className = 'cqa-pop';
    pop.dataset.part = 'pop';
    pop.setAttribute('role', 'dialog');
    pop.innerHTML = this.popHtml(this.s.pop.crit);
    const msg = this.s.pop.n != null ? this.querySelector(`.cqa-transcript [data-n="${this.s.pop.n}"]`) : null;
    if (msg) {
      pop.classList.add(msg.classList.contains('cqa-msg--operator') ? 'cqa-pop--right' : msg.classList.contains('cqa-msg--system') ? 'cqa-pop--center' : 'cqa-pop--left');
      msg.after(pop);
    } else {
      pop.classList.add('cqa-pop--block');
      const tiles = this.querySelector('.cqa-tiles');
      if (tiles) tiles.after(pop); else this.part('results')?.appendChild(pop);
    }
    this.querySelectorAll('.cqa-mark').forEach((el) => el.classList.toggle('is-on', el.dataset.mark === this.s.pop.crit && +el.dataset.n === this.s.pop.n));
    this.querySelectorAll('.cqa-crit').forEach((el) => el.classList.toggle('is-active', el.dataset.crit === this.s.pop.crit));
    this.highlight(this.s.pop.crit);
    this.maybeLoadDoubt(this.s.pop.crit);
    requestAnimationFrame(() => pop.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
  }

  chipsHtml(nums) {
    const items = this.s.conv.transcript || [];
    const list = [...new Set(nums.map(Number))].filter((k) => k >= 1);
    const shown = list.slice(0, 6);
    const chips = shown.map((k) => {
      const m = items.find((x) => x.n === k) || items[k - 1];
      if (!m) return `<span class="cqa-chip" title="Реплики нет в транскрипте"><span class="cqa-chip-n">#${k}</span></span>`;
      const who = ROLE_LABEL[m.speaker] || m.speaker;
      return `<button type="button" class="cqa-chip" data-ref="${k}" title="${esc(`${who}: ${m.text}`)}"><span class="cqa-chip-n">#${k}</span><span class="cqa-chip-text">${esc(who)}: ${esc(m.text)}</span></button>`;
    }).join('');
    const more = list.length > shown.length ? `<span class="cqa-faint">+${list.length - shown.length}</span>` : '';
    return chips + more;
  }

  /** «Модель спрашивает»: три оценки разошлись — вопрос проверяющему и довод каждой позиции. */
  doubtHtml(c, v, n) {
    const st = this.s.doubts.get(`${this.s.ev.id}:${c.id}`) || {};
    const q = st.result?.questions?.[0];
    const reviewer = getRole() === 'reviewer';
    const positions = c.positions || [];
    const total = positions.reduce((a, p) => a + p.count, 0);
    const opt = (a) => q?.options?.find((o) => o.answer === a);
    const ask = q ? `<p class="cqa-doubt-q">${esc(q.question)}</p>`
      : st.loading ? '<p class="cqa-doubt-q cqa-doubt-q--wait"><span class="cqa-spinner"></span>Формулирую вопрос…</p>'
        : st.error ? `<p class="cqa-faint">Вопрос не сформулирован (${esc(st.error)}). Ниже — доводы оценок.</p>` : '';
    return `<div class="cqa-doubt" data-part="doubt">
      <div class="cqa-doubt-head">${icon('help')}<b>Модель спрашивает</b><span class="cqa-doubt-votes">оценки разошлись: ${esc(positions.map((p) => `${answerLabel(v.crit, p.answer)} ×${p.count}`).join(' · '))}</span></div>
      ${ask}
      ${q?.existing_rule ? `<p class="cqa-doubt-rule">${icon('checklist')}<span>В чек-листе уже есть: «${esc(q.existing_rule)}» — оценки понимают это по-разному.</span></p>` : ''}
      <div class="cqa-doubt-opts">${positions.map((p) => `<div class="cqa-doubt-opt">
        <div class="cqa-doubt-opt-head"><span class="cqa-answer cqa-answer--sm" data-tone="${answerTone(v.crit, p.answer)}">${esc(answerLabel(v.crit, p.answer))}</span>
          <b>${esc(opt(p.answer)?.label || `${p.count} из ${total} оценок`)}</b>
          ${reviewer ? `<button type="button" class="cqa-btn cqa-btn--xs" data-act="doubt-pick" data-crit="${esc(c.id)}" data-answer="${esc(p.answer)}" ${st.saving ? 'disabled' : ''}>${icon('check')}Верно</button>` : ''}</div>
        ${p.explanation ? `<p class="cqa-doubt-arg">${linkifyRefs(p.explanation, n)}</p>` : ''}
      </div>`).join('')}</div>
      <p class="cqa-doubt-note">${reviewer ? 'Ваш выбор станет решением по диалогу: итог пересчитается, а решение будет примером для похожих диалогов.' : 'Отвечает проверяющий (переключите роль): его выбор станет решением и примером для похожих диалогов.'}</p>
    </div>`;
  }

  maybeLoadDoubt(cid) {
    const c = (this.s.ev?.criteria || []).find((x) => x.id === cid);
    if (!c || !this.critView(c).doubt) return;
    const key = `${this.s.ev.id}:${cid}`;
    if (this.s.doubts.has(key)) return;
    this.s.doubts.set(key, { loading: true });
    this.api.doubtQuestions({ criterion_id: cid, evaluation_id: this.s.ev.id })
      .then((result) => this.s.doubts.set(key, { result }))
      .catch((e) => this.s.doubts.set(key, { error: e.message }))
      .finally(() => { if (this.alive) this.refreshDoubt(cid); });
  }

  refreshDoubt(cid) {
    const box = this.querySelector('.cqa-pop [data-part="doubt"]');
    const c = (this.s.ev?.criteria || []).find((x) => x.id === cid);
    if (!box || !c || this.s.pop?.crit !== cid) return;
    const v = this.critView(c);
    if (v.doubt) box.outerHTML = this.doubtHtml(c, v, this.s.conv.transcript?.length || 0);
  }

  async resolveDoubt(cid, answer) {
    const key = `${this.s.ev.id}:${cid}`;
    const st = this.s.doubts.get(key) || {};
    if (st.saving) return;
    const q = st.result?.questions?.[0];
    const opt = q?.options?.find((o) => o.answer === answer);
    this.s.doubts.set(key, { ...st, saving: true });
    this.refreshDoubt(cid);
    try {
      await this.api.resolveDoubt({ evaluation_id: this.s.ev.id, criterion_id: cid, answer,
        question: q?.question || '', reason: opt ? `${opt.label}. ${opt.rule}` : '' });
      toast(`Решение по ${cid} сохранено — итог пересчитан`);
      emit('cqa-appeals-changed');
      await this.reloadEval();
    } catch (e) {
      this.s.doubts.set(key, { ...st, saving: false });
      this.refreshDoubt(cid);
      toast(`Не удалось сохранить решение: ${e.message}`, 'error', 6000);
      if (e.status === 409) await this.reloadEval();
    }
  }

  appealHtml(c, crit) {
    const a = c.appeal;
    const role = getRole();
    const ev = this.s.ev;
    if (!a) {
      if (this.s.appealFor === c.id) {
        return `<form class="cqa-appeal cqa-appeal-form" data-form="appeal" data-crit="${esc(c.id)}">
          <div class="cqa-label">Причина апелляции</div>
          <input type="hidden" name="reason_code" value="other">
          <div class="cqa-segctl cqa-appeal-reasons" role="group" aria-label="Причина апелляции">${Object.entries(APPEAL_REASONS).map(([code, label]) => `<button type="button" data-appeal-reason="${code}" aria-pressed="${code === 'other'}">${label}</button>`).join('')}</div>
          <label class="cqa-field"><span class="cqa-label">Комментарий</span>
            <textarea class="cqa-textarea" name="comment" required minlength="3" placeholder="Например: оператор представился во второй реплике — см. №12"></textarea></label>
          <div class="cqa-row"><button type="button" class="cqa-btn cqa-btn--sm" data-act="appeal-cancel">Отмена</button>
          <button type="submit" class="cqa-btn cqa-btn--primary cqa-btn--sm">${icon('gavel')}Подать апелляцию</button></div>
        </form>`;
      }
      if (role === 'operator' && ev?.status !== 'error') {
        return `<div class="cqa-appeal"><button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--xs" data-act="appeal-open" data-crit="${esc(c.id)}">${icon('gavel')}Оспорить оценку</button></div>`;
      }
      return '';
    }
    const fa = c.final_answer ?? c.original?.answer;
    if (a.status === 'open') {
      return `<div class="cqa-appeal"><div class="cqa-appeal-status">
        <span class="cqa-badge cqa-badge--warn">${icon('clock')}Апелляция на рассмотрении</span>
        <div class="cqa-appeal-text">${esc(appealReasonLabel(a.reason_code))} · <q>${esc(a.comment)}</q> <span class="cqa-faint">· ${esc(fmtDateTime(a.created_at))}</span></div>
        <a class="cqa-btn cqa-btn--primary cqa-btn--xs" href="#/appeals?id=${encodeURIComponent(a.id)}">${icon('arrow')}${role === 'reviewer' ? 'Рассмотреть' : 'К апелляции'}</a>
      </div></div>`;
    }
    const changed = a.status === 'changed';
    return `<div class="cqa-appeal"><div class="cqa-appeal-status">
      <span class="cqa-badge ${changed ? 'cqa-badge--info' : 'cqa-badge--mint'}">${icon(changed ? 'edit' : 'check')}${changed ? 'Изменено проверяющим' : 'Оценка подтверждена'}</span>
      <div class="cqa-appeal-text">
        ${changed ? `<span class="cqa-final">Финальный ответ: <span class="cqa-answer cqa-answer--sm" data-tone="${answerTone(crit, fa)}">${esc(answerLabel(crit, fa))}</span></span> · ` : ''}
        Причина: <q>${esc(a.reason || '—')}</q><br>
        <span class="cqa-faint">${esc(appealReasonLabel(a.reason_code))}: «${esc(a.comment)}» · решение ${esc(fmtDateTime(a.decided_at))}</span>
      </div>
    </div></div>`;
  }

  /* ---------- переход к реплике, подсветка, плеер ---------- */
  focusReply(k) {
    const el = this.querySelector(`.cqa-transcript [data-n="${k}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' });
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    const m = (this.s.conv.transcript || []).find((x) => x.n === k);
    if (this.audio && m?.audio_offset != null) this.seek(m.audio_offset - 2);
  }

  seek(sec) {
    const a = this.audio;
    if (!a) return;
    const go = () => { try { a.currentTime = Math.max(0, sec); } catch { /* ещё не загружено */ } a.play().catch(() => {}); };
    if (a.readyState >= 1) go(); else { a.addEventListener('loadedmetadata', go, { once: true }); a.load(); }
  }

  bindAudio() {
    const a = this.part('audio');
    this.audio = a || null;
    if (!a) return;
    const items = (this.s.conv.transcript || []).filter((m) => m.audio_offset != null).sort((x, y) => x.audio_offset - y.audio_offset);
    let cur = null;
    const now = this.part('now');
    a.addEventListener('timeupdate', () => {
      const t = a.currentTime;
      let hit = null;
      for (const m of items) { if (m.audio_offset <= t + 0.25) hit = m; else break; }
      if (now) now.textContent = `${fmtClock(t)}${hit ? ` · реплика №${hit.n}` : ''}`;
      if (hit?.n === cur) return;
      this.querySelector('.cqa-transcript .is-playing')?.classList.remove('is-playing');
      cur = hit?.n ?? null;
      if (cur != null) {
        const el = this.querySelector(`.cqa-transcript [data-n="${cur}"]`);
        el?.classList.add('is-playing');
        if (!a.paused && el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    });
    a.addEventListener('error', () => { if (now) now.textContent = 'запись не загрузилась'; });
  }

  highlight(cid) {
    this.querySelectorAll('.cqa-transcript .is-evidence').forEach((e) => { e.classList.remove('is-evidence'); e.style.removeProperty('--cqa-ev'); });
    if (this.s.blind || this.s.comparison || !cid || !this.s.ev) return;
    const c = (this.s.ev.criteria || []).find((x) => x.id === cid);
    if (!c) return;
    const def = this.critDef(cid);
    const tone = answerTone({ ...c, steps: def?.steps }, c.original?.answer);
    for (const k of c.original?.evidence || []) {
      const el = this.querySelector(`.cqa-transcript [data-n="${k}"]`);
      if (el) { el.classList.add('is-evidence'); el.style.setProperty('--cqa-ev', TONE_COLOR[tone] || 'var(--cqa-green-500)'); }
    }
  }

  applyHighlight() { this.highlight(this.s.active); }

  onHover(e, enter) {
    const card = e.target.closest?.('.cqa-crit');
    if (!card || card.contains(e.relatedTarget)) return;
    this.highlight(enter ? card.dataset.crit : this.s.active);
  }

  /* ---------- события ---------- */
  onClick(e) {
    const t = e.target;
    const reason = t.closest('[data-appeal-reason]');
    if (reason) {
      const form = reason.closest('form');
      form.elements.reason_code.value = reason.dataset.appealReason;
      form.querySelectorAll('[data-appeal-reason]').forEach((b) => b.setAttribute('aria-pressed', String(b === reason)));
      return;
    }
    const calSummary = t.closest('.cqa-calibration details > summary');
    if (calSummary) { e.preventDefault(); this.toggleCalibrationDetails(calSummary.parentElement); return; }
    if (this.s.pop && !t.closest('.cqa-pop, .cqa-mark, .cqa-crit, [data-part="translation-tools"]')) this.closePop();
    const mark = t.closest('.cqa-mark');
    if (mark) {
      e.preventDefault();
      if (this.s.pop?.crit === mark.dataset.mark && this.s.pop?.n === +mark.dataset.n) this.closePop();
      else this.openPop(mark.dataset.mark, +mark.dataset.n);
      return;
    }
    const ref = t.closest('[data-ref]');
    if (ref) {
      e.preventDefault();
      const crit = ref.closest('.cqa-crit');
      if (crit) this.setActive(crit.dataset.crit, false);
      this.focusReply(+ref.dataset.ref);
      return;
    }
    const seek = t.closest('[data-seek]');
    if (seek) {
      e.preventDefault();
      const m = (this.s.conv.transcript || []).find((x) => x.n === +seek.dataset.seek);
      if (m?.audio_offset != null) this.seek(m.audio_offset - 1);
      return;
    }
    const act = t.closest('[data-act]');
    if (act) {
      const a = act.dataset.act;
      if (a === 'translate') this.translate();
      else if (a === 'translation-original' || a === 'translation-show') {
        this.s.showTranslation = a === 'translation-show';
        this.updateTranslation();
      }
      else if (a === 'insight-start') this.startInsight();
      else if (a === 'insight-toggle') {
        this.s.insightOpen = !this.s.insightOpen;
        const section = act.closest('.cqa-insight');
        section.classList.toggle('is-expanded', this.s.insightOpen);
        act.setAttribute('aria-expanded', String(this.s.insightOpen));
        section.querySelector('.cqa-insight-detail').inert = !this.s.insightOpen;
      }
      else if (a === 'cal-start') this.startCalibration();
      else if (a === 'cal-close') { if (!this.s.calBusy) { this.s.blind = false; this.s.comparison = null; this.render(); } }
      else if (a === 'pop-close') this.closePop();
      else if (a === 'score-open') this.openScoreDialog();
      else if (a === 'score-close') this.closeScoreDialog();
      else if (a === 'thinking-open' || a === 'thinking-tab') {
        if (a === 'thinking-tab') this.s.thinkingTab = +act.dataset.i; else this.s.thinkingTab = 0;
        let d = this.part('thinking-dialog');
        if (!d) { d = document.createElement('dialog'); d.className = 'cqa-dialog cqa-dialog--wide'; d.dataset.part = 'thinking-dialog'; this.appendChild(d); }
        d.innerHTML = this.thinkingDialogHtml();
        if (!d.open) d.showModal();
      } else if (a === 'thinking-close') this.part('thinking-dialog')?.close();
      else if (a === 'appeal-open') { this.s.appealFor = act.dataset.crit; this.renderPop(); this.querySelector('form[data-form="appeal"] textarea')?.focus(); }
      else if (a === 'appeal-cancel') { this.s.appealFor = null; this.renderPop(); }
      else if (a === 'doubt-pick') this.resolveDoubt(act.dataset.crit, act.dataset.answer);
      else if (a === 'rerun-toggle') { this.s.rerunOpen = !this.s.rerunOpen; this.part('rerun').innerHTML = this.s.rerunOpen ? this.rerunHtml() : ''; act.setAttribute('aria-expanded', String(this.s.rerunOpen)); }
      else if (a === 'rerun-start') this.startRerun(act.closest('div'));
      return;
    }
    const row = t.closest('.cqa-crit');
    if (row && !t.closest('a, button, textarea, input, select, form')) this.openFromRow(row.dataset.crit);
  }

  setActive(cid, scroll) {
    this.s.active = this.s.active === cid && scroll ? null : cid;
    this.querySelectorAll('.cqa-crit').forEach((el) => el.classList.toggle('is-active', el.dataset.crit === this.s.active));
    this.highlight(this.s.active);
    if (scroll && this.s.active) {
      const c = (this.s.ev?.criteria || []).find((x) => x.id === cid);
      const first = c?.original?.evidence?.[0];
      if (first) this.querySelector(`.cqa-transcript [data-n="${first}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  onChange(e) {
    const t = e.target;
    if (t.dataset.calCriterion && this.s.blind) {
      this.s.calAnswers[t.dataset.calCriterion] = t.value;
      const count = Object.keys(this.s.calAnswers).length;
      this.part('cal-count').textContent = `${count} из ${this.s.cfg.criteria.length}`;
      this.part('cal-submit').disabled = count !== this.s.cfg.criteria.length;
    }
    if (t.dataset.f === 'eval') this.openEval(t.value);
  }

  /** Открыть другую оценку: в приложении — через адрес (#/c/…?e=…), во встроенном виде — на месте. */
  openEval(id) {
    this.s.blind = false; this.s.comparison = null;
    if (this.hasAttribute('embedded')) { this.evalId = String(id); this.s.run = null; this.s.rerunOpen = false; this.load(); }
    else navigate(`#/c/${encodeURIComponent(this.convId)}?e=${encodeURIComponent(id)}`);
  }

  async onSubmit(e) {
    const f = e.target;
    if (f.dataset.form === 'calibration') { e.preventDefault(); await this.submitCalibration(f); return; }
    if (f.dataset.form !== 'appeal') return;
    e.preventDefault();
    const fd = new FormData(f);
    const comment = String(fd.get('comment') || '').trim();
    const reason_code = fd.get('reason_code') || 'other';
    if (comment.length < 3) { toast('Напишите, с чем вы не согласны', 'warn'); return; }
    const btn = f.querySelector('[type=submit]');
    btn.disabled = true;
    try {
      await this.api.createAppeal({ evaluation_id: this.s.ev.id, criterion_id: f.dataset.crit, comment, reason_code });
      this.s.appealFor = null;
      toast('Апелляция подана — её увидит проверяющий в разделе «Апелляции»');
      emit('cqa-appeals-changed');
      await this.reloadEval();
    } catch (er) {
      btn.disabled = false;
      toast(er.status === 409 ? 'По этому критерию уже есть открытая апелляция' : `Не удалось подать апелляцию: ${er.message}`, 'error', 6000);
      if (er.status === 409) await this.reloadEval();
    }
  }

  async startRerun(box) {
    const scope = box?.closest('.cqa-panel, .cqa-results') || this;
    const checklist_id = +(scope.querySelector('[data-f="rerun-checklist"]')?.value || 0);
    const provider = scope.querySelector('[data-f="rerun-provider"]')?.value;
    if (!checklist_id) { toast('Нет чек-листа', 'warn'); return; }
    try {
      const res = await this.api.startRun({ conversation_ids: [this.convId], checklist_id, provider });
      const runId = res?.run_id ?? res?.id;
      this.s.run = { id: runId, status: 'running' };
      scope.querySelectorAll('[data-act="rerun-start"]').forEach((b) => { b.disabled = true; });
      const st = scope.querySelector('[data-part="rerun-status"]');
      if (st) st.innerHTML = '<span class="cqa-status cqa-status--run"><span class="cqa-spinner"></span>Модель оценивает диалог…</span>';
      const stop = pollRun(this.api, runId, (run, err) => {
        if (!this.alive || err || !run) return;
        this.s.run = run;
        if (!isRunFinished(run)) return;
        const item = (run.items || []).find((it) => it.conversation_id === this.convId);
        if (item?.evaluation_id != null && item.status !== 'error') {
          toast(`Готово: ${fmtScore(item.total)} · ${fmtMoney(run.cost_usd)}`);
          emit('cqa-data-changed');
          this.openEval(item.evaluation_id);
        } else {
          toast(`Оценка не удалась: ${item?.error || run.error || 'ошибка модели'}`, 'error', 8000);
          this.s.run = null;
          if (st) st.innerHTML = '';
          scope.querySelectorAll('[data-act="rerun-start"]').forEach((b) => { b.disabled = false; });
        }
      });
      this.onCleanup(stop);
    } catch (er) { toast(`Не удалось запустить оценку: ${er.message}`, 'error', 7000); }
  }
}

function shortLabel(label) {
  if (!label) return 'оценка';
  const m = /v(\d+)\s*$/.exec(label);
  return m ? `v${m[1]}` : label;
}

customElements.define('cqa-card', Card);
