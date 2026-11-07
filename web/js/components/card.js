// <cqa-card conversation-id="…" [evaluation-id="…"] [embedded]> — карточка диалога: транскрипт + результат оценки,
// переход от обоснования к реплике (и к секунде записи звонка), апелляция оператора.
import { CqaElement } from '../base.js';
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
    this.s = { conv: null, ev: null, cfg: null, meta: null, checklists: [], evals: [], appealFor: null, active: null, rerunOpen: false, run: null, openCrit: new Set() };
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Открываю диалог…</div>';
    this.addEventListener('click', (e) => this.onClick(e));
    this.addEventListener('submit', (e) => this.onSubmit(e));
    this.addEventListener('change', (e) => this.onChange(e));
    this.addEventListener('mouseover', (e) => this.onHover(e, true));
    this.addEventListener('mouseout', (e) => this.onHover(e, false));
    // раскрытые критерии переживают перерисовку (toggle не всплывает — ловим на погружении)
    this.addEventListener('toggle', (e) => {
      const d = e.target;
      if (d.matches?.('details.cqa-crit')) { if (d.open) this.s.openCrit.add(d.dataset.crit); else this.s.openCrit.delete(d.dataset.crit); }
    }, true);
    this.listen(window, 'cqa-role', () => this.renderResults());
    this.load();
  }

  async load() {
    try {
      const [conv, meta, checklists] = await Promise.all([this.api.conversation(this.convId), this.api.meta(), this.api.checklists()]);
      this.s.conv = conv; this.s.meta = meta; this.s.checklists = checklists || [];
      this.s.evals = [...(conv.evaluations || [])].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || (b.evaluation_id - a.evaluation_id));
      const id = this.evalId || this.s.evals[0]?.evaluation_id;
      if (id != null) await this.loadEval(id);
      if (!this.alive) return;
      this.render();
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-card-head"><a class="cqa-back" href="#/">${icon('back')}Диалоги</a></div>
        <div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось открыть диалог «${esc(this.convId)}»: ${esc(e.message)}</div></div>`;
    }
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
    const evOpts = this.s.evals.length > 1
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
          <button type="button" class="cqa-btn" data-act="rerun-toggle" aria-expanded="${this.s.rerunOpen}">${icon('refresh')}Переоценить</button>
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
    this.applyHighlight();
  }

  renderResults() {
    const el = this.part('results');
    if (!el) return;
    el.innerHTML = this.resultsHtml();
    this.applyHighlight();
  }

  transcriptHeadHtml() {
    const pii = (this.s.conv.transcript || []).reduce((a, m) => a + (m.pii?.length || 0), 0);
    return `<div class="cqa-transcript-head">
      <h2>Транскрипт</h2>
      <span class="cqa-faint" style="font-size:12px">метки времени — как в исходных данных</span>
      <span class="cqa-spacer"></span>
      ${pii ? `<span class="cqa-badge cqa-badge--warn" title="Эти фрагменты заменены метками до отправки модели">${icon('lock')}скрыто от модели: ${pii}</span>` : ''}
      <span class="cqa-badge cqa-badge--ghost" title="Реплики бота и системные события модель видит как контекст, но оценивает только оператора">оценивается только оператор</span>
    </div>`;
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
        return `<div class="cqa-msg cqa-msg--system" data-n="${m.n}"><div class="cqa-bubble"><span class="cqa-sys-n">#${m.n}</span>${text}<span class="cqa-msg-ts cqa-faint" title="${esc(m.timestamp)}">${esc(fmtTs(m.timestamp))}</span>${seek}</div></div>`;
      }
      return `<div class="cqa-msg cqa-msg--${role}" data-n="${m.n}">
        <div class="cqa-msg-meta"><span class="cqa-msg-n">#${m.n}</span><span class="cqa-msg-role">${ROLE_LABEL[role]}</span><span class="cqa-msg-ts" title="${esc(m.timestamp)}">${esc(fmtTs(m.timestamp))}</span>${seek}</div>
        <div class="cqa-bubble">${text}</div></div>`;
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
    return this.callMetricsHtml() + this.evaluationHtml();
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
        <p>Выберите чек-лист и модель — оценка займёт несколько секунд.</p></div><div style="padding:0 20px 20px">${this.rerunHtml(true)}</div></div>`;
    }
    if (ev.status === 'error') {
      return `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Оценка не удалась: ${esc(ev.error || 'неизвестная ошибка')}</div>
        <div style="margin-top:14px">${this.rerunHtml(true)}</div></div>`;
    }
    const fin = ev.final || ev.original || {};
    const orig = ev.original || fin;
    const crits = ev.criteria || [];
    const changedCrit = crits.filter((c) => c.final_answer != null && c.final_answer !== c.original?.answer);
    const totalChanged = orig.total !== fin.total;
    return `
      ${this.grossHtml(fin)}
      ${this.heroHtml(orig, fin, crits, totalChanged || changedCrit.length > 0)}
      ${this.metaHtml()}
      ${this.criteriaHtml(crits)}
      ${this.thinkingHtml()}`;
  }

  thinkingHtml() {
    const runs = (this.s.ev.thinking || []).filter((t) => t && t.trim());
    if (!runs.length) return '';
    const n = this.s.conv.transcript?.length || 0;
    const para = (t) => t.trim().split(/\n{2,}/).map((x) => `<p>${linkifyRefs(x, n)}</p>`).join('');
    return `<details class="cqa-thinking">
      <summary>${icon('spark')}<b>Как модель рассуждала</b><span class="cqa-faint">${runs.length > 1 ? `${runs.length} ${plural(runs.length, 'независимый прогон', 'независимых прогона', 'независимых прогонов')}` : 'пересказ размышлений'} · на рабочем языке модели</span>${icon('down')}</summary>
      ${runs.map((t, i) => `<div class="cqa-thinking-run">${runs.length > 1 ? `<div class="cqa-thinking-label">Прогон ${i + 1}</div>` : ''}${para(t)}</div>`).join('')}
    </details>`;
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
        <div class="cqa-hero-score"><span class="cqa-hero-num" data-tone="${scoreTone(fin.total)}">${fmtScore(fin.total)}</span><span class="cqa-hero-of">/ 100</span></div>
        <div class="cqa-hero-side">${delta}<div class="cqa-hero-caption">${esc(caption)}</div></div>
      </div>
      ${bars ? `<div class="cqa-sections" aria-label="Результаты секций">${bars}</div>` : ''}
      ${this.s.ev.precedents?.length ? `<p class="cqa-hero-caption">Учтены похожие оценённые диалоги: ${this.s.ev.precedents.map((p) => `<a href="#/c/${encodeURIComponent(p.conversation_id)}">${esc(p.conversation_id)}</a>${p.source === 'reviewer' ? ' (решение проверяющего)' : ''}`).join(', ')}</p>` : ''}
    </div>`;
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
      <span class="cqa-faint">оценка №${esc(ev.id)}${ev.prompt_version ? ` · промпт ${esc(ev.prompt_version)}` : ''}</span>
    </div>`;
  }

  criteriaHtml(crits) {
    const groups = [];
    for (const c of crits) {
      const name = c.section || 'Без секции';
      let g = groups.find((x) => x.name === name);
      if (!g) { g = { name, items: [] }; groups.push(g); }
      g.items.push(c);
    }
    const allOpen = crits.length && crits.every((c) => this.s.openCrit.has(c.id));
    return `<div class="cqa-crit-tools"><span class="cqa-faint">Нажмите на критерий — обоснование и реплики</span><button type="button" class="cqa-link" data-act="crit-all">${allOpen ? 'Свернуть все' : 'Развернуть все'}</button></div>` + groups.map((g) => `<div class="cqa-sec-head"><span class="cqa-eyebrow">${esc(g.name)}</span></div>
      ${g.items.map((c) => this.critHtml(c)).join('')}`).join('');
  }

  critDef(id) { return this.s.cfg?.criteria?.find((x) => x.id === id) || null; }

  critHtml(c) {
    const def = this.critDef(c.id);
    const crit = { ...c, steps: def?.steps };
    const orig = c.original || {};
    const fa = c.final_answer ?? orig.answer;
    const tone = answerTone(crit, fa);
    const changed = fa !== orig.answer;
    const sc = c.score_final || c.score_original || {};
    const n = this.s.conv.transcript?.length || 0;
    let points;
    if (c.type === 'flag') points = '<div class="cqa-crit-points"><span>без баллов</span><small>отмечается отдельно</small></div>';
    else if (!sc.applicable) points = `<div class="cqa-crit-points"><span>не учитывается</span><small>вес ${fmtPts(c.weight)} → другим</small></div>`;
    else points = `<div class="cqa-crit-points" title="Баллы после перераспределения весов N/A / максимум критерия"><b>${fmtPts(sc.points_eff)}</b> <span>/ ${fmtPts(sc.weight_eff)}</span><small>вес ${fmtPts(c.weight)}</small></div>`;
    const answer = changed
      ? `<span class="cqa-row" style="gap:6px;flex-wrap:nowrap"><span class="cqa-answer cqa-answer--sm cqa-answer--old" data-tone="${answerTone(crit, orig.answer)}" title="Ответ модели">${esc(answerLabel(crit, orig.answer))}</span><span class="cqa-arrow">→</span><span class="cqa-answer" data-tone="${tone}" title="Решение проверяющего">${esc(answerLabel(crit, fa))}</span></span>`
      : `<span class="cqa-answer" data-tone="${tone}">${esc(answerLabel(crit, fa))}</span>`;
    const stepText = (a) => {
      if (!def) return '';
      if (a === 'N/A') return def.na_when || '';
      if (def.type === 'scale') return (def.steps || []).find((s) => String(s.value) === String(a))?.text || '';
      return '';
    };
    let step = '';
    if (changed && (stepText(orig.answer) || stepText(fa))) {
      step = `<div class="cqa-crit-step"><b>Модель: ${esc(orig.answer)}</b>${stepText(orig.answer) ? ` — ${esc(stepText(orig.answer))}` : ''}<br><b>Проверяющий: ${esc(fa)}</b>${stepText(fa) ? ` — ${esc(stepText(fa))}` : ''}</div>`;
    } else if (stepText(fa)) step = `<div class="cqa-crit-step"><b>${esc(fa)}</b> — ${esc(stepText(fa))}</div>`;
    const defTitle = def ? [`Да — ${def.yes}`, def.no ? `Нет — ${def.no}` : '', def.na ? `N/A — ${def.na_when}` : ''].filter(Boolean).join('\n') : '';
    const ev = orig.evidence || [];
    const evHtml = ev.length ? `<div class="cqa-crit-ev"><span class="cqa-ev-label">${esc(evidenceLabel(crit, orig.answer))}</span>${this.chipsHtml(ev)}</div>` : '';
    const agreement = /^(\d+)\/(\d+)$/.exec(orig.agreement || '');
    const uncertain = agreement && Number(agreement[1]) < Number(agreement[2]);
    const votesTitle = (orig.votes || []).map((a, i) => `Прогон ${i + 1}: ${a}`).join('\n');
    const open = this.s.openCrit.has(c.id) || this.s.appealFor === c.id || this.s.active === c.id;
    const flags = [uncertain ? `<span class="cqa-crit-flag" title="${esc(votesTitle)}">не уверена ${esc(agreement[1])}/${esc(agreement[2])}</span>` : '',
      c.appeal?.status === 'open' ? `<span class="cqa-crit-flag cqa-crit-flag--warn">${icon('gavel')}апелляция</span>` : ''].join('');
    return `<details class="cqa-crit ${this.s.active === c.id ? 'is-active' : ''}" data-tone="${tone}" data-crit="${esc(c.id)}" ${open ? 'open' : ''}>
      <summary class="cqa-crit-head">
        <span class="cqa-crit-id">${esc(c.id)}</span>
        <div class="cqa-crit-title">${esc(c.title)}${flags}${defTitle ? ` <span class="cqa-faint" title="${esc(defTitle)}">${icon('info')}</span>` : ''}</div>
        ${answer}
        ${points}
        <span class="cqa-crit-chev">${icon('down')}</span>
      </summary>
      <div class="cqa-crit-body">
      ${step}
      ${c.reused_from != null ? `<p class="cqa-faint">Перенесено из оценки №${esc(c.reused_from)}: определение не менялось.</p>` : ''}
      ${uncertain ? `<p class="cqa-muted" title="${esc(votesTitle)}">${icon('info')} модель не уверена: ${esc(agreement[1])} из ${esc(agreement[2])}</p>` : ''}
      ${orig.explanation ? `<p class="cqa-crit-expl">${linkifyRefs(orig.explanation, n)}</p>` : ''}
      ${evHtml}
      ${this.appealHtml(c, crit)}
      </div>
    </details>`;
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

  appealHtml(c, crit) {
    const a = c.appeal;
    const role = getRole();
    const ev = this.s.ev;
    if (!a) {
      if (this.s.appealFor === c.id) {
        return `<form class="cqa-appeal cqa-appeal-form" data-form="appeal" data-crit="${esc(c.id)}">
          <label class="cqa-field"><span class="cqa-label">Почему вы не согласны с оценкой «${esc(c.title)}»? <small>обязательно</small></span>
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
        <div class="cqa-appeal-text">Комментарий оператора: <q>${esc(a.comment)}</q> <span class="cqa-faint">· ${esc(fmtDateTime(a.created_at))}</span></div>
        ${role === 'reviewer' ? `<a class="cqa-btn cqa-btn--primary cqa-btn--xs" href="#/appeals?id=${encodeURIComponent(a.id)}">${icon('shield')}Рассмотреть</a>` : ''}
      </div></div>`;
    }
    const changed = a.status === 'changed';
    return `<div class="cqa-appeal"><div class="cqa-appeal-status">
      <span class="cqa-badge ${changed ? 'cqa-badge--info' : 'cqa-badge--mint'}">${icon(changed ? 'edit' : 'check')}${changed ? 'Изменено проверяющим' : 'Оценка подтверждена'}</span>
      <div class="cqa-appeal-text">
        ${changed ? `<span class="cqa-final">Финальный ответ: <span class="cqa-answer cqa-answer--sm" data-tone="${answerTone(crit, fa)}">${esc(answerLabel(crit, fa))}</span></span> · ` : ''}
        Причина: <q>${esc(a.reason || '—')}</q><br>
        <span class="cqa-faint">Апелляция: «${esc(a.comment)}» · решение ${esc(fmtDateTime(a.decided_at))}</span>
      </div>
    </div></div>`;
  }

  /* ---------- переход к реплике, подсветка, плеер ---------- */
  focusReply(k) {
    const el = this.querySelector(`.cqa-transcript [data-n="${k}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
    if (!cid || !this.s.ev) return;
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
      if (a === 'crit-all') {
        const ids = (this.s.ev?.criteria || []).map((c) => c.id);
        const allOpen = ids.every((id) => this.s.openCrit.has(id));
        this.s.openCrit = new Set(allOpen ? [] : ids);
        this.renderResults();
      } else if (a === 'appeal-open') { this.s.appealFor = act.dataset.crit; this.renderResults(); this.querySelector('form[data-form="appeal"] textarea')?.focus(); }
      else if (a === 'appeal-cancel') { this.s.appealFor = null; this.renderResults(); }
      else if (a === 'rerun-toggle') { this.s.rerunOpen = !this.s.rerunOpen; this.part('rerun').innerHTML = this.s.rerunOpen ? this.rerunHtml() : ''; act.setAttribute('aria-expanded', String(this.s.rerunOpen)); }
      else if (a === 'rerun-start') this.startRerun(act.closest('div'));
      return;
    }
    const card = t.closest('.cqa-crit');
    if (card && !t.closest('a, button, textarea, input, select, form')) this.setActive(card.dataset.crit, true);
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
    if (t.dataset.f === 'eval') this.openEval(t.value);
  }

  /** Открыть другую оценку: в приложении — через адрес (#/c/…?e=…), во встроенном виде — на месте. */
  openEval(id) {
    if (this.hasAttribute('embedded')) { this.evalId = String(id); this.s.run = null; this.s.rerunOpen = false; this.load(); }
    else navigate(`#/c/${encodeURIComponent(this.convId)}?e=${encodeURIComponent(id)}`);
  }

  async onSubmit(e) {
    const f = e.target;
    if (f.dataset.form !== 'appeal') return;
    e.preventDefault();
    const comment = String(new FormData(f).get('comment') || '').trim();
    if (comment.length < 3) { toast('Напишите, с чем вы не согласны', 'warn'); return; }
    const btn = f.querySelector('[type=submit]');
    btn.disabled = true;
    try {
      await this.api.createAppeal({ evaluation_id: this.s.ev.id, criterion_id: f.dataset.crit, comment });
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
