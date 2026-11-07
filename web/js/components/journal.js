// <cqa-journal> — журнал прогонов: модель, чек-лист, время, токены, стоимость (в т.ч. одной оценки).
import { CqaElement } from '../base.js';
import { askAnswerHtml, toggleAskDisclosure } from './ask.js';
import {
  esc, icon, fmtDateTime, fmtInt, fmtMoney, fmtSeconds, fmtClock, secondsBetween, isRunFinished, providerLabel,
} from '../util.js';

class Journal extends CqaElement {
  init() {
    this.s = { runs: [], asks: [], calls: [], meta: null, folds: new Set() };
    this.listen(this, 'click', (event) => {
      const toggle = event.target.closest('[data-ask-toggle]');
      if (toggle) {
        toggleAskDisclosure(toggle);
        if (toggle.dataset.logFold) {
          if (toggle.getAttribute('aria-expanded') === 'true') this.s.folds.add(toggle.dataset.logFold);
          else this.s.folds.delete(toggle.dataset.logFold);
        }
      }
    });
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Загружаю журнал…</div>';
    this.load();
  }

  async load() {
    try {
      const [runs, meta, asks, calls] = await Promise.all([this.api.runs(), this.s.meta ? Promise.resolve(this.s.meta) : this.api.meta(),
        this.api.mock ? [] : this.api.asks(), this.api.mock ? [] : this.api.checklistTestCalls()]);
      if (!this.alive) return;
      this.s.runs = [...(runs || [])].sort((a, b) => (b.id ?? 0) - (a.id ?? 0));
      this.s.meta = meta;
      this.s.asks = asks || [];
      this.s.calls = calls || [];
      this.render();
      clearTimeout(this.timer);
      if (this.s.runs.some((r) => !isRunFinished(r))) { this.timer = setTimeout(() => this.load(), 2000); this.onCleanup(() => clearTimeout(this.timer)); }
    } catch (e) {
      if (!this.alive) return;
      this.innerHTML = `<div class="cqa-panel cqa-panel-pad"><div class="cqa-error-box">${icon('alert')} Не удалось загрузить журнал: ${esc(e.message)}</div></div>`;
    }
  }

  render() {
    const runs = this.s.runs;
    const evals = runs.reduce((a, r) => a + (r.done || 0), 0);
    const evaluationCost = runs.reduce((a, r) => a + (r.cost_usd || 0), 0);
    const checksCost = this.s.calls.reduce((a, c) => a + (c.cost_usd || 0), 0);
    const cost = evaluationCost + this.s.asks.reduce((sum, ask) => sum + (ask.cost_usd || 0), 0);
    const per = evals ? evaluationCost / evals : null;
    const lat = runs.filter((r) => r.avg_latency_s != null && r.done);
    const avgLat = lat.length ? lat.reduce((a, r) => a + r.avg_latency_s * r.done, 0) / lat.reduce((a, r) => a + r.done, 0) : null;
    this.innerHTML = `
      <div class="cqa-page-head">
        <h1>Журнал</h1>
        <div class="cqa-page-actions"><button type="button" class="cqa-btn" data-act="reload">${icon('refresh')}Обновить</button></div>
      </div>
      <div class="cqa-journal-summary">
        <span>Прогонов <b>${fmtInt(runs.length)}</b></span><span>Оценок <b>${fmtInt(evals)}</b></span>
        <span title="Оценки, вопросы ИИ и сверки">Потрачено всего <b>${fmtMoney(cost + checksCost)}</b></span>
        <span title="Средняя стоимость оценки со всеми вызовами">Одна оценка <b>${per == null ? '—' : fmtMoney(per)}</b></span>
        <span title="Среднее время одной оценки">Время <b>${avgLat == null ? '—' : fmtSeconds(avgLat)}</b></span>
      </div>
      <div class="cqa-panel"><div class="cqa-table-wrap">${runs.length ? this.tableHtml(runs) : `<div class="cqa-empty"><div class="cqa-empty-icon">${icon('journal')}</div><h3>Прогонов ещё не было</h3><p>Запустите оценку на экране «Диалоги» или примените чек-лист к набору.</p></div>`}</div></div>
      ${this.foldHtml('asks', `Вопросы ИИ · ${fmtInt(this.s.asks.length)} · ${fmtMoney(this.s.asks.reduce((sum, ask) => sum + (ask.cost_usd || 0), 0))}`,
        this.s.asks.length ? this.s.asks.map((ask) => `<div class="cqa-ask-log-item">
          <button class="cqa-btn cqa-btn--ghost cqa-ask-log-head" type="button" data-ask-toggle data-log-fold="ask-${ask.id}" aria-expanded="${this.s.folds.has(`ask-${ask.id}`)}">
            ${icon('right')}<span class="cqa-ask-log-question">${esc(ask.question)}</span><span class="cqa-faint">${esc(fmtDateTime(ask.created_at))} · ${fmtMoney(ask.cost_usd)}${ask.status !== 'ok' ? ` · ${ask.status === 'pending' ? 'ожидание' : 'ошибка'}` : ''}</span></button>
          <div class="cqa-ask-reveal ${this.s.folds.has(`ask-${ask.id}`) ? 'is-open' : ''}" ${this.s.folds.has(`ask-${ask.id}`) ? '' : 'inert'} aria-hidden="${!this.s.folds.has(`ask-${ask.id}`)}"><div><div class="cqa-ask-log-body">
            ${ask.status === 'ok' ? askAnswerHtml(ask) : `<p class="cqa-muted">${esc(ask.error || 'Запрос ещё не завершён.')}</p>`}
            <div class="cqa-faint cqa-ask-meta">${esc(ask.model)} · токены ${fmtInt(ask.input_tokens)} / ${fmtInt(ask.output_tokens)}</div>
          </div></div></div></div>`).join('') : '<p class="cqa-muted">Вопросов ещё нет. <a href="#/ask">Спросить ИИ</a></p>')}
      ${this.callsHtml()}`;
    this.querySelector('[data-act="reload"]')?.addEventListener('click', () => this.load());
  }

  foldHtml(key, label, content) {
    const open = this.s.folds.has(key);
    return `<section class="cqa-journal-fold"><button type="button" class="cqa-fold-toggle" data-ask-toggle data-log-fold="${esc(key)}" aria-expanded="${open}">${icon('right')}${label}</button>
      <div class="cqa-ask-reveal ${open ? 'is-open' : ''}" ${open ? '' : 'inert'} aria-hidden="${!open}"><div>${content}</div></div></section>`;
  }

  callsHtml() {
    const calls = this.s.calls;
    return this.foldHtml('calls', `Сверки с утверждёнными · ${fmtInt(calls.length)} · ${fmtMoney(calls.reduce((sum, c) => sum + (c.cost_usd || 0), 0))}`, calls.length ? `
      <div class="cqa-table-wrap"><table class="cqa-table"><thead><tr><th>Время</th><th>Диалог</th><th>Критерии</th><th>Модель</th><th>Токены вход / выход</th><th>Стоимость</th><th>Время</th><th>Статус</th></tr></thead>
      <tbody>${calls.map((c) => `<tr><td class="cqa-nowrap">${esc(fmtDateTime(c.created_at))}</td><td><a href="#/c/${encodeURIComponent(c.conversation_id)}">${esc(c.conversation_id)}</a></td><td>${c.criteria.map(esc).join(', ')}</td><td>${esc(c.model)}</td><td>${fmtInt(c.input_tokens)} / ${fmtInt(c.output_tokens)}</td><td>${fmtMoney(c.cost_usd)}</td><td>${fmtSeconds(c.latency_s)}</td><td title="${esc(c.error || '')}">${c.status === 'ok' ? 'готово' : c.status === 'running' ? 'идёт' : `ошибка: ${esc(c.error)}`}</td></tr>`).join('')}</tbody></table></div>` : '<p class="cqa-muted">Сверок ещё нет.</p>');
  }

  tableHtml(runs) {
    return `<table class="cqa-table">
      <thead><tr><th>№</th><th>Запущен</th><th>Чек-лист</th><th>Модель</th><th class="cqa-col-right">Диалогов</th><th class="cqa-col-right">Готово / ошибок</th>
        <th class="cqa-col-right">Длительность</th><th class="cqa-col-right">Токены вход / выход</th><th class="cqa-col-right">Стоимость</th><th class="cqa-col-right">За оценку</th><th class="cqa-col-right">Ср. время</th><th>Статус</th></tr></thead>
      <tbody>${runs.map((r) => {
    const fin = isRunFinished(r);
    const dur = secondsBetween(r.created_at, fin ? r.finished_at : null);
    const per = r.avg_cost_per_eval ?? (r.done ? (r.cost_usd || 0) / r.done : null);
    const st = !fin ? `<span class="cqa-status cqa-status--run"><span class="cqa-spinner"></span>идёт</span>`
      : (r.status === 'error' || r.status === 'failed') ? `<span class="cqa-status cqa-status--err"><span class="cqa-dot"></span>ошибка</span>`
        : `<span class="cqa-status cqa-status--ok"><span class="cqa-dot"></span>готово</span>`;
    return `<tr>
          <td class="cqa-cell-id">${esc(r.id)}</td>
          <td class="cqa-nowrap">${esc(fmtDateTime(r.created_at))}</td>
          <td>${esc(r.checklist_label || `чек-лист №${r.checklist_id}`)}</td>
          <td>${esc(providerLabel(this.s.meta, r.provider, r.model))}${this.foldHtml(`run-${r.id}`, 'Вызовы', `<div class="cqa-cell-sub">${esc(r.model || '')}<br>${r.mode === 'incremental' ? 'Инкрементально' : 'Полностью'} · вызовов ${fmtInt(r.model_calls || 0)}<br>Критериев оценено ${fmtInt(r.evaluated || 0)}, перенесено ${fmtInt(r.reused || 0)}</div>`)}</td>
          <td class="cqa-col-right cqa-num">${fmtInt(r.total)}</td>
          <td class="cqa-col-right cqa-num">${fmtInt(r.done)}${r.failed ? ` / <span style="color:var(--cqa-no)">${fmtInt(r.failed)}</span>` : ' / 0'}</td>
          <td class="cqa-col-right cqa-num">${fmtClock(dur)}</td>
          <td class="cqa-col-right cqa-num">${r.tokens_in != null ? `${fmtInt(r.tokens_in)} / ${fmtInt(r.tokens_out)}` : '—'}</td>
          <td class="cqa-col-right cqa-num"><b>${fmtMoney(r.cost_usd || 0)}</b></td>
          <td class="cqa-col-right cqa-num">${per == null ? '—' : fmtMoney(per)}</td>
          <td class="cqa-col-right cqa-num">${r.avg_latency_s == null ? '—' : fmtSeconds(r.avg_latency_s)}</td>
          <td>${st}</td></tr>`;
  }).join('')}</tbody></table>
`;
  }
}

customElements.define('cqa-journal', Journal);
