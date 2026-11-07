// <cqa-conversation-list> — список диалогов: импорт набора, фильтры, запуск оценки с прогрессом.
import { CqaElement } from '../base.js';
import {
  esc, icon, fmtScore, fmtDateTime, fmtMoney, fmtClock, fmtDuration, fmtInt, scoreTone, isCall, CHANNEL_LABEL,
  toast, pollRun, isRunFinished, store, providerLabel, checklistLabel, navigate, emit, secondsBetween, plural,
} from '../util.js';

const RUN_KEY = 'cqa.activeRun';
const LANG_ORDER = ['RU', 'KZ', 'EN'];

const finalTotal = (l) => (l ? (l.final_total ?? l.total) : null);
const finalGross = (l) => (l ? (l.final_gross ?? l.gross) : null);

class ConversationList extends CqaElement {
  init() {
    this.s = {
      meta: null, datasets: [], checklists: [], rows: [],
      dataset: this.getAttribute('dataset') || null, lang: 'all', channel: 'all', q: '',
      sort: { key: 'id', dir: 1 }, selected: new Set(), checklistId: null, provider: null,
      run: null, runItems: new Map(),
    };
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Загружаю диалоги…</div>';
    this.addEventListener('click', (e) => this.onClick(e));
    this.addEventListener('change', (e) => this.onChange(e));
    this.addEventListener('input', (e) => this.onInput(e));
    this.addEventListener('submit', (e) => this.onSubmit(e));
    this.load();
  }

  async load() {
    try {
      const [meta, datasets, checklists] = await Promise.all([this.api.meta(), this.api.datasets(), this.api.checklists()]);
      Object.assign(this.s, { meta, datasets: datasets || [], checklists: checklists || [] });
      const latest = [...this.s.checklists].sort((a, b) => b.id - a.id)[0];
      this.s.checklistId = latest?.id ?? null;
      const avail = (meta?.providers || []).filter((p) => p.available !== false);
      this.s.provider = avail.find((p) => p.id === meta?.default_provider)?.id || avail[0]?.id || meta?.default_provider || null;
      if (!this.s.dataset || !this.s.datasets.some((d) => d.id === this.s.dataset)) {
        this.s.dataset = this.s.datasets.length === 1 ? this.s.datasets[0].id : 'all';
      }
      await this.loadRows();
      if (!this.alive) return;
      this.renderAll();
      this.resumeRun();
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось загрузить данные: ${esc(e.message)}</div></div>`;
    }
  }

  async loadRows() {
    const rows = await this.api.conversations(this.s.dataset && this.s.dataset !== 'all' ? { dataset_id: this.s.dataset } : {});
    this.s.rows = rows || [];
    const ids = new Set(this.s.rows.map((r) => r.id));
    for (const id of [...this.s.selected]) if (!ids.has(id)) this.s.selected.delete(id);
  }

  /* ---------- данные для показа ---------- */
  filtered() {
    const { lang, channel, q, sort } = this.s;
    const qq = q.trim().toLowerCase();
    const rows = this.s.rows.filter((r) => (lang === 'all' || (r.language || '').toUpperCase() === lang)
      && (channel === 'all' || (channel === 'call') === isCall(r.channel))
      && (!qq || r.id.toLowerCase().includes(qq)));
    const val = (r) => (sort.key === 'total' ? (finalTotal(r.latest) ?? -1) : sort.key === 'language' ? (r.language || '') : r.id);
    return rows.sort((a, b) => {
      const x = val(a), y = val(b);
      const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y), 'ru', { numeric: true });
      return c * sort.dir || a.id.localeCompare(b.id, 'ru', { numeric: true });
    });
  }

  /* ---------- отрисовка ---------- */
  renderAll() {
    const ds = this.s.datasets.find((d) => d.id === this.s.dataset);
    this.innerHTML = `
      <div class="cqa-page-head">
        <div>
          <h1>Диалоги</h1>
          <div class="cqa-page-sub" data-part="sub">${this.subtitle(ds)}</div>
        </div>
        <div class="cqa-page-actions">
          <button type="button" class="cqa-btn cqa-btn--primary" data-act="import">${icon('download')}Загрузить диалоги</button>
          <input type="file" accept=".json,application/json" hidden data-part="file">
        </div>
      </div>
      <div data-part="run"></div>
      <div class="cqa-panel">
        <div class="cqa-toolbar" data-part="toolbar"></div>
        <div class="cqa-table-wrap" data-part="table"></div>
      </div>
      ${this.importDialogHtml()}`;
    this.renderToolbar();
    this.renderTable();
    this.renderRun();
  }

  subtitle(ds) {
    if (!this.s.datasets.length) return 'Диалогов пока нет — нажмите «Загрузить диалоги».';
    const n = this.s.rows.length;
    return `${ds ? `Набор <b>${esc(ds.id)}</b>` : 'Все наборы'} · ${fmtInt(n)} ${plural(n, 'диалог', 'диалога', 'диалогов')}`;
  }

  renderKpis(rows) {
    const el = this.part('sub');
    if (!el) return;
    const ds = this.s.datasets.find((d) => d.id === this.s.dataset);
    if (!this.s.datasets.length) { el.innerHTML = this.subtitle(ds); return; }
    const evald = rows.filter((r) => r.latest && r.latest.status !== 'error');
    const totals = evald.map((r) => finalTotal(r.latest)).filter((x) => x != null);
    const avg = totals.length ? totals.reduce((a, b) => a + b, 0) / totals.length : null;
    const gross = evald.filter((r) => finalGross(r.latest)).length;
    const open = rows.reduce((a, r) => a + (r.appeals?.open || 0), 0);
    const parts = [`${ds ? `Набор <b>${esc(ds.id)}</b>` : 'Все наборы'} · ${fmtInt(rows.length)} ${plural(rows.length, 'диалог', 'диалога', 'диалогов')}`];
    if (evald.length < rows.length) parts.push(`оценено ${fmtInt(evald.length)}`);
    if (avg != null) parts.push(`средний итог <b>${fmtScore(avg)}</b> из 100`);
    if (gross) parts.push(`<span class="cqa-sub-alert">грубые ошибки — ${fmtInt(gross)}</span>`);
    if (open) parts.push(`<a class="cqa-link" href="#/appeals">ждут решения: ${fmtInt(open)} ${plural(open, 'апелляция', 'апелляции', 'апелляций')} →</a>`);
    el.innerHTML = parts.join(' · ');
  }

  renderToolbar() {
    const el = this.part('toolbar');
    if (!el) return;
    const { datasets, checklists, meta } = this.s;
    const langs = LANG_ORDER.filter((l) => this.s.rows.some((r) => (r.language || '').toUpperCase() === l));
    const extra = [...new Set(this.s.rows.map((r) => (r.language || '').toUpperCase()))].filter((l) => l && !LANG_ORDER.includes(l));
    const seg = (f, items, cur) => `<div class="cqa-segctl" data-seg="${f}" role="group">${items.map(([v, l]) => `<button type="button" data-v="${esc(v)}" aria-pressed="${v === cur}">${esc(l)}</button>`).join('')}</div>`;
    const busy = this.runActive();
    const providers = meta?.providers || [];
    const nsel = this.s.selected.size;
    el.innerHTML = `
      <div class="cqa-toolbar-group">
        ${datasets.length > 1 ? `<select class="cqa-select cqa-select--sm" data-f="dataset" aria-label="Набор">
            <option value="all" ${this.s.dataset === 'all' ? 'selected' : ''}>Все наборы</option>
            ${datasets.map((d) => `<option value="${esc(d.id)}" ${d.id === this.s.dataset ? 'selected' : ''}>${esc(d.id)} · ${fmtInt(d.total)}</option>`).join('')}
          </select>` : ''}
        ${seg('lang', [['all', 'Все языки'], ...[...langs, ...extra].map((l) => [l, l])], this.s.lang)}
        ${seg('channel', [['all', 'Все'], ['chat', 'Чаты'], ['call', 'Звонки']], this.s.channel)}
        <div class="cqa-search">${icon('search')}<input class="cqa-input cqa-input--sm" data-f="q" placeholder="Поиск по ID" value="${esc(this.s.q)}"></div>
      </div>
      <div class="cqa-toolbar-group cqa-toolbar-run">
        ${checklists.length > 1 ? `<select class="cqa-select cqa-select--sm" data-f="checklist" aria-label="Чек-лист" title="Версия чек-листа" ${busy ? 'disabled' : ''}>
          ${[...checklists].sort((a, b) => b.id - a.id).map((c) => `<option value="${c.id}" ${c.id === this.s.checklistId ? 'selected' : ''}>${esc(checklistLabel(c))}</option>`).join('')}
        </select>` : `<a class="cqa-inline-value" href="#/checklist" title="Открыть чек-лист">${checklists.length ? esc(checklistLabel(checklists[0])) : 'нет'}</a>`}
        <span class="cqa-inline-label">Модель</span>
        <select class="cqa-select cqa-select--sm" data-f="provider" aria-label="Модель" title="Модель" ${busy ? 'disabled' : ''}>
          ${providers.map((p) => `<option value="${esc(p.id)}" ${p.id === this.s.provider ? 'selected' : ''} ${p.available === false ? 'disabled' : ''}>${esc(p.label || p.id)}${p.available === false ? ' — не настроена' : ''}</option>`).join('')}
        </select>
        <button type="button" class="cqa-btn" data-act="run-selected" ${nsel ? '' : 'hidden'} ${busy ? 'disabled' : ''}>Оценить выбранные <span class="cqa-num" data-part="sel-count">(${nsel})</span></button>
        <button type="button" class="cqa-btn cqa-btn--primary" data-act="run-all" ${busy ? 'disabled' : ''}>${icon('play')}Оценить все <span class="cqa-num" data-part="all-count">(0)</span></button>
      </div>`;
  }

  renderTable() {
    const rows = this.filtered();
    this.renderKpis(rows);
    const wrap = this.part('table');
    if (!wrap) return;
    const all = this.part('all-count'), sel = this.part('sel-count');
    if (all) all.textContent = `(${rows.length})`;
    if (sel) sel.textContent = `(${this.s.selected.size})`;
    const selBtn = this.querySelector('[data-act="run-selected"]');
    if (selBtn) selBtn.hidden = !this.s.selected.size;
    if (!this.s.rows.length) {
      wrap.innerHTML = `<div class="cqa-empty"><div class="cqa-empty-icon">${icon('db')}</div><h3>Диалогов пока нет</h3>
        <p>Импортируйте набор из API организаторов по group_id (например, DEV) или загрузите JSON-файл с транскриптами.</p>
        <div class="cqa-row" style="justify-content:center"><button type="button" class="cqa-btn cqa-btn--primary" data-act="import">${icon('download')}Загрузить диалоги</button></div></div>`;
      return;
    }
    if (!rows.length) {
      wrap.innerHTML = `<div class="cqa-empty"><div class="cqa-empty-icon">${icon('search')}</div><h3>Ничего не найдено</h3><p>Измените фильтры или строку поиска.</p></div>`;
      return;
    }
    const allSel = rows.every((r) => this.s.selected.has(r.id));
    const sortBtn = (key, label) => {
      const on = this.s.sort.key === key;
      return `<button type="button" data-sort="${key}" title="Сортировать">${label}${on ? (this.s.sort.dir > 0 ? ' ↑' : ' ↓') : ''}</button>`;
    };
    wrap.innerHTML = `<table class="cqa-table">
      <thead><tr>
        <th class="cqa-col-check"><input type="checkbox" class="cqa-check" data-f="all" ${allSel ? 'checked' : ''} aria-label="Выбрать все"></th>
        <th>${sortBtn('id', 'Диалог')}</th><th>Канал</th><th>${sortBtn('language', 'Язык')}</th>
        <th class="cqa-col-right">${sortBtn('total', 'Итог')}</th><th>Отметки</th><th></th>
      </tr></thead>
      <tbody>${rows.map((r) => this.rowHtml(r)).join('')}</tbody></table>`;
  }

  rowHtml(r) {
    const l = r.latest;
    const item = this.s.runItems.get(r.id);
    const running = item && (item.status === 'pending' || item.status === 'running');
    const ft = finalTotal(l);
    let score;
    if (running) score = `<span class="cqa-status cqa-status--run"><span class="cqa-spinner"></span>${item.status === 'running' ? 'оценивается' : 'в очереди'}</span>`;
    else if (item?.status === 'error') score = `<span class="cqa-status cqa-status--err" title="${esc(item.error || '')}"><span class="cqa-dot"></span>ошибка</span>`;
    else if (!l) score = '<span class="cqa-faint">не оценён</span>';
    else if (l.status === 'error') score = '<span class="cqa-badge cqa-badge--no">ошибка</span>';
    else {
      score = `<span class="cqa-score" data-tone="${scoreTone(ft)}">${fmtScore(ft)}</span>`;
      if (l.final_total != null && l.total != null && Math.abs(l.final_total - l.total) > 1e-9) {
        score += `<div class="cqa-score-was">было <s>${fmtScore(l.total)}</s></div>`;
      }
    }
    const marks = [];
    if (finalGross(l)) marks.push(`<span class="cqa-badge cqa-badge--gross">${icon('alert')}Грубая ошибка</span>`);
    if (r.appeals?.open) marks.push(`<span class="cqa-badge cqa-badge--warn">${icon('gavel')}${r.appeals.open} ${plural(r.appeals.open, 'апелляция', 'апелляции', 'апелляций')}</span>`);
    if (r.appeals?.decided) marks.push(`<span class="cqa-badge cqa-badge--mint">${icon('check')}решено: ${r.appeals.decided}</span>`);
    const sel = this.s.selected.has(r.id);
    const meta = [r.n_replies != null ? `${fmtInt(r.n_replies)} ${plural(r.n_replies, 'реплика', 'реплики', 'реплик')}` : '', fmtDuration(r.duration)].filter(Boolean).join(' · ');
    return `<tr class="is-clickable ${sel ? 'is-selected' : ''} ${running ? 'is-running' : ''}" data-id="${esc(r.id)}">
      <td class="cqa-col-check"><input type="checkbox" class="cqa-check" data-sel="${esc(r.id)}" ${sel ? 'checked' : ''} aria-label="Выбрать ${esc(r.id)}"></td>
      <td><div class="cqa-cell-id">${esc(r.id)}</div>${meta ? `<div class="cqa-cell-sub">${esc(meta)}</div>` : ''}</td>
      <td><span class="cqa-channel">${icon(isCall(r.channel) ? 'phone' : 'chat')}${CHANNEL_LABEL(r.channel)}</span></td>
      <td><span class="cqa-lang">${esc((r.language || '—').toUpperCase())}</span></td>
      <td class="cqa-col-right">${score}</td>
      <td><div class="cqa-marks">${marks.join('') || '<span class="cqa-faint">—</span>'}</div></td>
      <td class="cqa-faint">${icon('right')}</td>
    </tr>`;
  }

  /* ---------- прогон ---------- */
  runActive() { return !!this.s.run && !isRunFinished(this.s.run); }

  renderRun() {
    const el = this.part('run');
    if (!el) return;
    const run = this.s.run;
    if (!run) { el.innerHTML = ''; return; }
    const total = run.total || 0, done = run.done || 0, failed = run.failed || 0;
    const fin = isRunFinished(run);
    const pct = total ? Math.round((done / total) * 100) : 0;
    const fpct = total ? Math.round((failed / total) * 100) : 0;
    const secs = secondsBetween(run.created_at, fin ? run.finished_at : null);
    const per = done ? (run.cost_usd || 0) / done : null;
    const title = fin
      ? (run.status === 'error' || run.status === 'failed' ? `${icon('alert')} Оценка завершилась с ошибкой` : `${icon('check')} Оценка завершена`)
      : '<span class="cqa-spinner"></span> Идёт оценка';
    el.innerHTML = `<div class="cqa-panel cqa-run ${fin ? (failed && !done ? 'cqa-run--error' : 'cqa-run--done') : ''}" style="margin-bottom:16px">
      <div class="cqa-run-title">${title}</div>
      <div class="cqa-run-stats">
        <span>${esc(run.checklist_label || checklistLabel(this.s.checklists.find((c) => c.id === run.checklist_id)))}</span>
        <span>${icon('cpu')} ${esc(providerLabel(this.s.meta, run.provider, run.model))}</span>
        <span>Готово <b>${fmtInt(done)}</b> из <b>${fmtInt(total)}</b></span>
        ${failed ? `<span style="color:var(--cqa-no)">Ошибок <b>${fmtInt(failed)}</b></span>` : ''}
        <span>${icon('coins')} <b>${fmtMoney(run.cost_usd || 0)}</b>${per != null ? ` · ${fmtMoney(per)} за оценку` : ''}</span>
        <span>${icon('clock')} <b>${fmtClock(secs)}</b></span>
      </div>
      <div>${fin ? '<button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-act="run-close">Скрыть</button>' : ''}</div>
      <div class="cqa-run-bar"><div class="cqa-progress ${fin ? '' : 'is-running'}"><div class="cqa-progress-fill" style="width:${pct}%"></div>${failed ? `<div class="cqa-progress-fill cqa-progress-fill--err" style="width:${fpct}%"></div>` : ''}</div></div>
    </div>`;
  }

  async startRun(ids) {
    if (!ids.length) { toast('Отметьте диалоги галочками или нажмите «Оценить все»', 'warn'); return; }
    if (!this.s.checklistId) { toast('Нет чек-листа — создайте его в разделе «Чек-лист»', 'warn'); return; }
    try {
      const res = await this.api.startRun({ conversation_ids: ids, checklist_id: +this.s.checklistId, provider: this.s.provider });
      const runId = res?.run_id ?? res?.id;
      this.s.runItems = new Map(ids.map((id) => [id, { status: 'pending' }]));
      this.s.run = { id: runId, status: 'running', total: ids.length, done: 0, failed: 0, cost_usd: 0, created_at: new Date().toISOString(), checklist_id: +this.s.checklistId, provider: this.s.provider };
      this.renderToolbar(); this.renderTable(); this.renderRun();
      this.trackRun(runId);
    } catch (e) { toast(`Не удалось запустить оценку: ${e.message}`, 'error', 7000); }
  }

  trackRun(runId) {
    store(RUN_KEY, { id: runId });
    this.stopPoll?.();
    let lastDone = -1, lastReload = 0, reloading = false;
    const reload = async (force) => {
      if (reloading || (!force && Date.now() - lastReload < 1500)) return;
      reloading = true; lastReload = Date.now();
      try { await this.loadRows(); if (this.alive) this.renderTable(); } catch { /* повторим на следующем шаге */ }
      reloading = false;
    };
    this.stopPoll = pollRun(this.api, runId, async (run, err) => {
      if (!this.alive) return;
      if (err) { if (err.status === 404) { store(RUN_KEY, null); this.s.run = null; this.renderRun(); this.stopPoll?.(); } return; }
      this.s.run = run;
      if (Array.isArray(run.items)) this.s.runItems = new Map(run.items.map((it) => [it.conversation_id, it]));
      this.renderRun();
      const finished = isRunFinished(run);
      const progress = (run.done || 0) + (run.failed || 0);
      if (progress !== lastDone || finished) { lastDone = progress; await reload(finished); }
      if (finished) {
        store(RUN_KEY, null);
        this.s.runItems = new Map([...this.s.runItems].filter(([, it]) => it.status === 'error'));
        this.renderToolbar(); this.renderTable();
        const per = run.done ? (run.cost_usd || 0) / run.done : 0;
        toast(`Оценено ${fmtInt(run.done)} из ${fmtInt(run.total)}${run.failed ? `, ошибок ${run.failed}` : ''} · ${fmtMoney(run.cost_usd || 0)} (${fmtMoney(per)} за оценку)`, run.failed && !run.done ? 'error' : 'info', 6000);
        emit('cqa-data-changed', { run: run.id });
      }
    });
    this.onCleanup(() => this.stopPoll?.());
  }

  async resumeRun() {
    const saved = store(RUN_KEY);
    if (!saved?.id) return;
    try {
      const run = await this.api.run(saved.id);
      if (!this.alive) return;
      if (isRunFinished(run)) { store(RUN_KEY, null); return; }
      this.s.run = run;
      this.renderToolbar(); this.renderRun();
      this.trackRun(saved.id);
    } catch { store(RUN_KEY, null); }
  }

  /* ---------- импорт ---------- */
  importDialogHtml() {
    const configured = !!this.s.meta?.dataset_url_configured;
    return `<dialog class="cqa-dialog" data-part="import-dialog" aria-labelledby="cqa-import-title">
      <form data-form="import">
        <div class="cqa-dialog-head"><span class="cqa-empty-icon" style="width:36px;height:36px;margin:0;border-radius:10px">${icon('download')}</span><h2 id="cqa-import-title">Загрузить диалоги</h2></div>
        <div class="cqa-dialog-body">
          <label class="cqa-field"><span class="cqa-label">Код набора из API организаторов (group_id)</span>
            <input class="cqa-input" name="group_id" required placeholder="например, DEV" value="${esc(this.s.dataset && this.s.dataset !== 'all' ? this.s.dataset : 'DEV')}" autocomplete="off"></label>
          ${configured
    ? `<details class="cqa-details"><summary>${icon('right')}Указать другую ссылку API</summary>
              <label class="cqa-field" style="margin-top:8px"><span class="cqa-label">Ссылка GET из гайда организаторов <small>необязательно</small></span><input class="cqa-input" name="url" placeholder="https://…" autocomplete="off"></label></details>`
    : `<label class="cqa-field"><span class="cqa-label">Ссылка GET из гайда организаторов <small>сохранится на сервере; подпись в интерфейсе не показываем</small></span><input class="cqa-input" name="url" required placeholder="https://…" autocomplete="off"></label>`}
          <div class="cqa-row" style="align-items:center;gap:8px"><span class="cqa-muted">или</span><button type="button" class="cqa-btn cqa-btn--sm" data-act="upload">${icon('upload')}выбрать JSON-файл</button></div>
          <div data-part="import-error"></div>
        </div>
        <div class="cqa-dialog-foot">
          <button type="button" class="cqa-btn" data-act="import-cancel">Отмена</button>
          <button type="submit" class="cqa-btn cqa-btn--primary" data-part="import-submit">${icon('download')}Импортировать</button>
        </div>
      </form>
    </dialog>`;
  }

  async doImport(form) {
    const fd = new FormData(form);
    const group_id = String(fd.get('group_id') || '').trim();
    const url = String(fd.get('url') || '').trim();
    const btn = this.part('import-submit'), errBox = this.part('import-error');
    if (!group_id) return;
    btn.disabled = true; btn.innerHTML = '<span class="cqa-spinner"></span>Загружаю…'; errBox.innerHTML = '';
    try {
      const res = await this.api.importDataset(url ? { group_id, url } : { group_id });
      this.part('import-dialog')?.close();
      toast(`Импортировано ${fmtInt(res?.imported ?? res?.total ?? 0)} ${plural(res?.imported ?? 0, 'диалог', 'диалога', 'диалогов')} в набор ${res?.dataset_id || group_id}`);
      await this.afterImport(res?.dataset_id || group_id);
    } catch (e) {
      errBox.innerHTML = `<div class="cqa-error-box">${esc(e.message)}</div>`;
      btn.disabled = false; btn.innerHTML = `${icon('download')}Импортировать`;
    }
  }

  async doUpload(file) {
    if (!file) return;
    toast(`Загружаю ${file.name}…`, 'info', 2000);
    try {
      const res = await this.api.uploadDataset(file);
      toast(`Загружено ${fmtInt(res?.imported ?? 0)} ${plural(res?.imported ?? 0, 'диалог', 'диалога', 'диалогов')} в набор ${res?.dataset_id || ''}`);
      await this.afterImport(res?.dataset_id);
    } catch (e) { toast(`Файл не принят: ${e.message}`, 'error', 8000); }
  }

  async afterImport(datasetId) {
    try {
      const [datasets, meta] = await Promise.all([this.api.datasets(), this.api.meta()]);
      this.s.datasets = datasets || [];
      this.s.meta = meta || this.s.meta;
      if (datasetId && this.s.datasets.some((d) => d.id === datasetId)) this.s.dataset = datasetId;
      await this.loadRows();
      if (this.alive) this.renderAll();
      emit('cqa-data-changed', { dataset: datasetId });
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---------- события ---------- */
  onClick(e) {
    const t = e.target;
    const act = t.closest('[data-act]')?.dataset.act;
    if (act) {
      e.preventDefault();
      if (act === 'import') { const d = this.part('import-dialog'); this.part('import-error').innerHTML = ''; d.showModal(); d.querySelector('input[name=group_id]')?.select(); }
      else if (act === 'import-cancel') this.part('import-dialog')?.close();
      else if (act === 'upload') { this.part('import-dialog')?.close(); this.part('file')?.click(); }
      else if (act === 'run-all') this.startRun(this.filtered().map((r) => r.id));
      else if (act === 'run-selected') this.startRun(this.s.rows.filter((r) => this.s.selected.has(r.id)).map((r) => r.id));
      else if (act === 'run-close') { this.s.run = null; this.renderRun(); }
      return;
    }
    const seg = t.closest('[data-seg] button');
    if (seg) {
      this.s[seg.closest('[data-seg]').dataset.seg] = seg.dataset.v;
      this.renderToolbar(); this.renderTable();
      return;
    }
    const sortB = t.closest('[data-sort]');
    if (sortB) {
      const key = sortB.dataset.sort;
      this.s.sort = { key, dir: this.s.sort.key === key ? -this.s.sort.dir : (key === 'total' ? -1 : 1) };
      this.renderTable();
      return;
    }
    if (t.matches('input[type=checkbox]') || t.closest('.cqa-col-check')) {
      if (!t.matches('input[type=checkbox]')) { const cb = t.closest('.cqa-col-check')?.querySelector('input'); if (cb) cb.click(); }
      return;
    }
    const tr = t.closest('tr[data-id]');
    if (tr) navigate(`#/c/${encodeURIComponent(tr.dataset.id)}`);
  }

  onChange(e) {
    const t = e.target;
    if (t.dataset.part === 'file') { this.doUpload(t.files?.[0]); t.value = ''; return; }
    if (t.dataset.sel) {
      if (t.checked) this.s.selected.add(t.dataset.sel); else this.s.selected.delete(t.dataset.sel);
      t.closest('tr')?.classList.toggle('is-selected', t.checked);
      const sel = this.part('sel-count'); if (sel) sel.textContent = `(${this.s.selected.size})`;
      const selBtn = this.querySelector('[data-act="run-selected"]'); if (selBtn) selBtn.hidden = !this.s.selected.size;
      const allBox = this.querySelector('[data-f="all"]');
      if (allBox) allBox.checked = this.filtered().every((r) => this.s.selected.has(r.id));
      return;
    }
    const f = t.dataset.f;
    if (f === 'all') { this.filtered().forEach((r) => (t.checked ? this.s.selected.add(r.id) : this.s.selected.delete(r.id))); this.renderTable(); }
    else if (f === 'dataset') { this.s.dataset = t.value; this.loadRows().then(() => { if (this.alive) { this.renderToolbar(); this.renderTable(); } }).catch((er) => toast(er.message, 'error')); }
    else if (f === 'checklist') this.s.checklistId = +t.value;
    else if (f === 'provider') this.s.provider = t.value;
  }

  onInput(e) {
    if (e.target.dataset.f === 'q') { this.s.q = e.target.value; this.renderTable(); }
  }

  onSubmit(e) {
    if (e.target.dataset.form === 'import') { e.preventDefault(); this.doImport(e.target); }
  }
}

customElements.define('cqa-conversation-list', ConversationList);
