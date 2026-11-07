// <cqa-appeals [appeal-id="…"]> — очередь апелляций и решение проверяющего с пересчётом итога.
import { CqaElement } from '../base.js';
import { appealReasonLabel } from '../appeal-reasons.js';
import {
  esc, icon, fmtScore, fmtDateTime, fmtTs, answerTone, answerLabel, evidenceLabel, linkifyRefs, ROLE_LABEL,
  toast, getRole, setRole, emit, navigate,
} from '../util.js';

const CONTEXT = 2;

class Appeals extends CqaElement {
  init() {
    this.s = { list: [], tab: 'open', sel: this.getAttribute('appeal-id'), detail: null, decision: 'confirm', result: null };
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Загружаю апелляции…</div>';
    this.addEventListener('click', (e) => this.onClick(e));
    this.addEventListener('change', (e) => this.onChange(e));
    this.addEventListener('submit', (e) => this.onSubmit(e));
    this.listen(window, 'cqa-role', () => this.render());
    this.load();
  }

  async load() {
    try {
      this.s.list = (await this.api.appeals('all')) || [];
      if (!this.alive) return;
      const sel = this.s.list.find((a) => String(a.id) === String(this.s.sel));
      if (sel) this.s.tab = sel.status === 'open' ? 'open' : 'decided';
      else {
        const open = this.s.list.filter((a) => a.status === 'open');
        this.s.tab = open.length ? 'open' : 'decided';
        this.s.sel = (open[0] || this.sorted()[0])?.id ?? null;
      }
      this.render();
      if (this.s.sel != null) this.select(this.s.sel);
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось загрузить апелляции: ${esc(e.message)}</div></div>`;
    }
  }

  sorted() {
    const order = { open: 0, changed: 1, confirmed: 1 };
    return [...this.s.list].sort((a, b) => (order[a.status] ?? 2) - (order[b.status] ?? 2) || String(b.created_at).localeCompare(String(a.created_at)));
  }

  visible() {
    const t = this.s.tab;
    return this.sorted().filter((a) => t === 'all' || (t === 'open' ? a.status === 'open' : a.status !== 'open'));
  }

  render() {
    const open = this.s.list.filter((a) => a.status === 'open').length;
    this.innerHTML = `
      <div class="cqa-page-head">
        <div><h1>Апелляции</h1></div>
        <div class="cqa-page-actions">${getRole() === 'reviewer'
    ? `<span class="cqa-badge cqa-badge--mint cqa-badge--lg">${icon('shield')}Проверяющий</span>`
    : `<button type="button" class="cqa-btn cqa-btn--accent" data-act="be-reviewer">${icon('shield')}Я проверяющий</button>`}</div>
      </div>
      <div class="cqa-appeals-grid">
        <aside class="cqa-panel cqa-appeal-queue">
          <div class="cqa-tabs" role="tablist">
            ${[['open', 'Открытые', open], ['decided', 'Решённые', this.s.list.length - open]].map(([k, l, n]) => `<button type="button" class="cqa-tab" role="tab" data-tab="${k}" aria-selected="${this.s.tab === k}">${l}<span class="cqa-tab-count">${n}</span></button>`).join('')}
          </div>
          <div class="cqa-appeal-columns" aria-hidden="true"><span>Диалог</span><span>Критерий</span><span>Модель</span><span>Причина</span><span>Статус</span><span>Когда</span></div>
          <div class="cqa-appeal-list" data-part="list"></div>
        </aside>
        <section class="cqa-panel cqa-appeal-detail" data-part="detail" aria-live="polite"></section>
      </div>`;
    this.renderList();
    this.renderDetail();
  }

  renderList() {
    const el = this.part('list');
    if (!el) return;
    const items = this.visible();
    if (!items.length) {
      el.innerHTML = `<div class="cqa-empty" style="padding:28px 10px"><div class="cqa-empty-icon">${icon('gavel')}</div><h3>${this.s.tab === 'open' ? 'Открытых апелляций нет' : 'Пусто'}</h3><p>Апелляцию подаёт оператор кнопкой «Оспорить оценку» в карточке диалога.</p></div>`;
      return;
    }
    el.innerHTML = items.map((a) => {
      const crit = { type: a.criterion_type, allowed: a.allowed || [] };
      return `<button type="button" class="cqa-appeal-item" data-id="${esc(a.id)}" aria-pressed="${String(a.id) === String(this.s.sel)}">
        <span class="cqa-cell-id" title="${esc(a.conversation_id)}">${esc(a.conversation_id)}</span>
        <span title="${esc(a.criterion_title)}">${esc(a.criterion_id)} · ${esc(a.criterion_title)}</span>
        <span class="cqa-answer cqa-answer--sm" data-tone="${answerTone(crit, a.original_answer)}">${esc(a.original_answer ?? '—')}</span>
        <span title="${esc(appealReasonLabel(a.reason_code))}">${esc(appealReasonLabel(a.reason_code))}</span>
        ${statusBadge(a.status)}
        <span class="cqa-faint" title="${esc(fmtDateTime(a.created_at))}">${esc(fmtDateTime(a.created_at))}</span>
      </button>`;
    }).join('');
  }

  async select(id) {
    this.s.sel = id;
    this.s.result = null;
    this.s.decision = 'confirm';
    this.querySelectorAll('.cqa-appeal-item').forEach((b) => b.setAttribute('aria-pressed', String(String(b.dataset.id) === String(id))));
    const a = this.s.list.find((x) => String(x.id) === String(id));
    if (!a) { this.s.detail = null; this.renderDetail(); return; }
    this.s.detail = { appeal: a, loading: true };
    this.renderDetail();
    try {
      const [ev, conv] = await Promise.all([this.api.evaluation(a.evaluation_id), this.api.conversation(a.conversation_id)]);
      if (!this.alive || String(this.s.sel) !== String(id)) return;
      this.s.detail = { appeal: a, ev, conv, crit: (ev.criteria || []).find((c) => c.id === a.criterion_id) };
    } catch (e) {
      if (!this.alive || String(this.s.sel) !== String(id)) return;
      this.s.detail = { appeal: a, error: e.message };
    }
    this.renderDetail();
  }

  renderDetail() {
    const el = this.part('detail');
    if (!el) return;
    const d = this.s.detail;
    if (!d) {
      el.innerHTML = `<div class="cqa-empty"><div class="cqa-empty-icon">${icon('shield')}</div><h3>Выберите апелляцию</h3><p>Нажмите на строку слева.</p></div>`;
      return;
    }
    const a = d.appeal;
    const head = `<div class="cqa-detail-head">
        <div style="flex:1;min-width:0">
          <div class="cqa-eyebrow">Апелляция №${esc(a.id)} · ${esc(fmtDateTime(a.created_at))}</div>
          <h2>${esc(a.criterion_id)} · ${esc(a.criterion_title || '')}</h2>
          <div class="cqa-row" style="margin-top:6px;gap:8px"><span class="cqa-cell-id">${esc(a.conversation_id)}</span>${statusBadge(a.status)}</div>
        </div>
        <a class="cqa-btn cqa-btn--sm" href="#/c/${encodeURIComponent(a.conversation_id)}?e=${encodeURIComponent(a.evaluation_id)}">Карточка ${icon('arrow')}</a>
      </div>`;
    if (d.loading) { el.innerHTML = `<div class="cqa-detail">${head}<div class="cqa-loading"><span class="cqa-spinner"></span>Загружаю оценку и фрагмент…</div></div>`; return; }
    if (d.error) { el.innerHTML = `<div class="cqa-detail">${head}<div class="cqa-error-box" style="margin-top:14px">${esc(d.error)}</div></div>`; return; }
    const c = d.crit || { id: a.criterion_id, title: a.criterion_title, type: a.criterion_type, allowed: a.allowed || [], original: { answer: a.original_answer, explanation: a.explanation, evidence: a.evidence } };
    const orig = c.original || {};
    const n = d.conv?.transcript?.length || 0;
    const sc = c.score_original || {};
    const pts = c.type === 'flag' ? 'без баллов' : sc.applicable ? `${fmtScore(sc.points_eff)} из ${fmtScore(sc.weight_eff)} баллов` : 'не учитывается (N/A)';
    el.innerHTML = `<div class="cqa-detail">
      ${head}
      <div class="cqa-block">
        <div class="cqa-block-title">Ответ модели</div>
        <div class="cqa-row" style="gap:10px"><span class="cqa-answer" data-tone="${answerTone(c, orig.answer)}">${esc(answerLabel(c, orig.answer))}</span><span class="cqa-muted">${esc(pts)}</span>
          <span class="cqa-faint">· ${esc(d.ev?.model || '')}</span></div>
        ${orig.explanation ? `<p class="cqa-crit-expl">${linkifyRefs(orig.explanation, n)}</p>` : ''}
        ${(orig.evidence || []).length ? `<div class="cqa-crit-ev"><span class="cqa-ev-label">${esc(evidenceLabel(c, orig.answer))}</span>${orig.evidence.map((k) => `<button type="button" class="cqa-chip" data-ref="${k}"><span class="cqa-chip-n">#${k}</span></button>`).join('')}</div>` : ''}
      </div>
      <div class="cqa-block">
        <div class="cqa-block-title">Оператор · ${esc(appealReasonLabel(a.reason_code))}</div>
        <blockquote class="cqa-quote">${esc(a.comment)}</blockquote>
      </div>
      <div class="cqa-block">
        <div class="cqa-block-title">Реплики-доказательства</div>
        <div class="cqa-fragment" data-part="fragment">${this.fragmentHtml(d.conv, orig.evidence || [])}</div>
      </div>
      <div class="cqa-block" data-part="decision">${this.decisionHtml(d, c)}</div>
    </div>`;
  }

  fragmentHtml(conv, evidence) {
    const items = conv?.transcript || [];
    if (!items.length) return '<span class="cqa-muted">Транскрипт недоступен</span>';
    const ev = new Set(evidence.map(Number));
    let keep = new Set();
    if (ev.size) for (const k of ev) for (let i = k - CONTEXT; i <= k + CONTEXT; i++) keep.add(i);
    else return '<span class="cqa-muted">Модель не указала реплики. Откройте карточку.</span>';
    let prev = null;
    const out = [];
    for (const m of items) {
      if (!keep.has(m.n)) continue;
      if (prev != null && m.n !== prev + 1) out.push('<div class="cqa-gap">• • •</div>');
      prev = m.n;
      const role = ['client', 'operator', 'bot', 'system'].includes(m.speaker) ? m.speaker : 'system';
      const hit = ev.has(m.n) ? ' is-evidence' : '';
      if (role === 'system') out.push(`<div class="cqa-msg cqa-msg--system${hit}" data-n="${m.n}"><div class="cqa-bubble"><span class="cqa-sys-n">#${m.n}</span>${esc(m.text)}</div></div>`);
      else out.push(`<div class="cqa-msg cqa-msg--${role}${hit}" data-n="${m.n}"><div class="cqa-msg-meta"><span class="cqa-msg-n">#${m.n}</span><span class="cqa-msg-role">${ROLE_LABEL[role]}</span><span class="cqa-msg-ts" title="${esc(m.timestamp)}">${esc(fmtTs(m.timestamp))}</span></div><div class="cqa-bubble">${esc(m.text)}</div></div>`);
    }
    return out.join('');
  }

  decisionHtml(d, c) {
    const a = d.appeal;
    const res = this.s.result;
    if (a.status !== 'open') {
      const changed = a.status === 'changed';
      const before = res ? res.before : a.total_before_decision;
      const after = res ? res.after : a.total_after_decision;
      return `<div class="cqa-block-title">Решение проверяющего</div>
        <div class="cqa-decision">
          <div class="cqa-row"><span class="cqa-badge ${changed ? 'cqa-badge--info' : 'cqa-badge--mint'} cqa-badge--lg">${icon(changed ? 'edit' : 'check')}${changed ? 'Оценка изменена' : 'Оценка подтверждена'}</span>
            ${changed ? `<span class="cqa-answer cqa-answer--sm cqa-answer--old" data-tone="${answerTone(c, c.original?.answer)}">${esc(answerLabel(c, c.original?.answer))}</span><span class="cqa-arrow">→</span><span class="cqa-answer" data-tone="${answerTone(c, a.new_answer)}">${esc(answerLabel(c, a.new_answer))}</span>` : ''}
            <span class="cqa-faint">${esc(fmtDateTime(a.decided_at))}</span></div>
          <div><b>Причина:</b> ${esc(a.reason || '—')}</div>
          ${totalsBanner(before, after)}
          ${this.nextOpenId() != null ? `<div class="cqa-row" style="justify-content:flex-end"><button type="button" class="cqa-btn cqa-btn--primary cqa-btn--sm" data-act="next">Следующая апелляция ${icon('arrow')}</button></div>` : ''}
        </div>`;
    }
    if (getRole() !== 'reviewer') {
      return `<div class="cqa-block-title">Решение</div>
        <div class="cqa-note cqa-note--warn" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">${icon('info')}<span style="flex:1">Решение принимает проверяющий.</span>
          <button type="button" class="cqa-btn cqa-btn--sm" data-act="be-reviewer">${icon('shield')}Я проверяющий</button></div>`;
    }
    const current = a.current_answer ?? c.final_answer ?? c.original?.answer;
    const allowed = (c.allowed || []).filter((x) => x !== current);
    const change = this.s.decision === 'change';
    return `<div class="cqa-block-title">Решение проверяющего</div>
      <form class="cqa-decision" data-form="decision">
        <div class="cqa-decision-options" role="group" aria-label="Решение">
          <label class="cqa-check"><input type="radio" name="decision" value="confirm" ${!change ? 'checked' : ''}>Подтвердить</label>
          <label class="cqa-check"><input type="radio" name="decision" value="change" ${change ? 'checked' : ''} ${allowed.length ? '' : 'disabled'}>Изменить</label>
        </div>
        ${change ? `<div class="cqa-row" role="group" aria-label="Новый ответ">${allowed.map((v, i) => `<label class="cqa-check"><input type="radio" name="new_answer" value="${esc(v)}" ${i === 0 ? 'checked' : ''} required><span class="cqa-answer cqa-answer--sm" data-tone="${answerTone(c, v)}">${esc(answerLabel(c, v))}</span></label>`).join('')}</div>` : `<span class="cqa-muted">Текущий ответ: ${esc(current)}</span>`}
        <label class="cqa-field"><span class="cqa-label">Причина решения</span>
          <textarea class="cqa-textarea" name="reason" required minlength="3" placeholder="Почему ответ подтверждён или изменён"></textarea></label>
        <div class="cqa-row"><button type="submit" class="cqa-btn cqa-btn--primary">${icon('check')}Сохранить решение</button></div>
      </form>`;
  }

  nextOpenId() {
    const open = this.sorted().filter((x) => x.status === 'open' && String(x.id) !== String(this.s.sel));
    return open[0]?.id ?? null;
  }

  /* ---------- события ---------- */
  onClick(e) {
    const t = e.target;
    const tab = t.closest('[data-tab]');
    if (tab) {
      this.s.tab = tab.dataset.tab;
      const first = this.visible()[0];
      this.s.sel = first?.id ?? null;
      this.s.detail = null;
      this.render();
      if (first) this.select(first.id);
      return;
    }
    const item = t.closest('.cqa-appeal-item');
    if (item) { this.select(item.dataset.id); history.replaceState(null, '', `#/appeals?id=${encodeURIComponent(item.dataset.id)}`); return; }
    const act = t.closest('[data-act]')?.dataset.act;
    if (act === 'be-reviewer') { setRole('reviewer'); this.render(); if (this.s.detail) this.renderDetail(); return; }
    if (act === 'next') {
      const id = this.nextOpenId();
      if (id != null) { this.s.tab = 'open'; this.render(); this.select(id); history.replaceState(null, '', `#/appeals?id=${encodeURIComponent(id)}`); }
      return;
    }
    const ref = t.closest('[data-ref]');
    if (ref) {
      e.preventDefault();
      const el = this.querySelector(`[data-part="fragment"] [data-n="${ref.dataset.ref}"]`);
      if (el) { el.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' }); el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash'); }
      else navigate(`#/c/${encodeURIComponent(this.s.detail.appeal.conversation_id)}?e=${encodeURIComponent(this.s.detail.appeal.evaluation_id)}`);
    }
  }

  onChange(e) {
    const t = e.target;
    if (t.name === 'decision') {
      this.s.decision = t.value;
      const form = t.closest('form');
      const reason = form.querySelector('[name=reason]')?.value || '';
      const d = this.s.detail;
      this.part('decision').innerHTML = this.decisionHtml(d, d.crit);
      const r = this.part('decision').querySelector('[name=reason]');
      if (r) r.value = reason;
    }
  }

  async onSubmit(e) {
    if (e.target.dataset.form !== 'decision') return;
    e.preventDefault();
    const fd = new FormData(e.target);
    const decision = fd.get('decision');
    const reason = String(fd.get('reason') || '').trim();
    const new_answer = decision === 'change' ? fd.get('new_answer') : undefined;
    if (reason.length < 3) { toast('Укажите причину решения', 'warn'); return; }
    if (decision === 'change' && !new_answer) { toast('Выберите новый ответ', 'warn'); return; }
    const btn = e.target.querySelector('[type=submit]');
    btn.disabled = true; btn.innerHTML = '<span class="cqa-spinner"></span>Сохраняю…';
    const a = this.s.detail.appeal;
    try {
      const res = await this.api.decideAppeal(a.id, decision === 'change' ? { decision, new_answer, reason } : { decision, reason });
      const ev = res?.evaluation;
      const before = res.appeal.total_before_decision;
      const after = res.appeal.total_after_decision;
      const updated = res.appeal;
      this.s.list = this.s.list.map((x) => (String(x.id) === String(a.id) ? { ...x, ...updated } : x));
      emit('cqa-appeals-changed');
      if (!this.alive || String(this.s.sel) !== String(a.id)) { if (this.alive) this.renderList(); return; }
      this.s.result = { before, after };
      this.s.detail = { ...this.s.detail, appeal: this.s.list.find((x) => String(x.id) === String(a.id)), ev: ev || this.s.detail.ev, crit: (ev?.criteria || []).find((c) => c.id === a.criterion_id) || this.s.detail.crit };
      toast(decision === 'change' ? `Оценка изменена · итог ${fmtScore(before)} → ${fmtScore(after)}` : 'Оценка подтверждена');
      this.s.tab = 'decided';
      this.render();
    } catch (er) {
      btn.disabled = false; btn.innerHTML = `${icon('check')}Сохранить решение`;
      toast(`Не удалось сохранить решение: ${er.message}`, 'error', 7000);
    }
  }
}

function statusBadge(st) {
  if (st === 'open') return `<span class="cqa-badge cqa-badge--warn">${icon('clock')}открыта</span>`;
  if (st === 'changed') return `<span class="cqa-badge cqa-badge--info">${icon('edit')}изменено</span>`;
  if (st === 'confirmed') return `<span class="cqa-badge cqa-badge--mint">${icon('check')}подтверждено</span>`;
  return `<span class="cqa-badge">${esc(st || '—')}</span>`;
}

function totalsBanner(before, after) {
  return `<div class="cqa-result-banner" role="status"><b>Итог: ${fmtScore(before)} → ${fmtScore(after)}</b></div>`;
}

customElements.define('cqa-appeals', Appeals);
