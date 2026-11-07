// <cqa-journal> — журнал прогонов: модель, чек-лист, время, токены, стоимость (в т.ч. одной оценки).
import { CqaElement } from '../base.js';
import { askAnswerHtml, toggleAskDisclosure } from './ask.js';
import {
  esc, icon, fmtDateTime, fmtInt, fmtMoney, fmtSeconds, fmtClock, secondsBetween, isRunFinished, providerLabel, plural,
} from '../util.js';

class Journal extends CqaElement {
  init() {
    this.s = { runs: [], asks: [], meta: null };
    this.listen(this, 'click', (event) => {
      const toggle = event.target.closest('[data-ask-toggle]');
      if (toggle) toggleAskDisclosure(toggle);
    });
    this.innerHTML = '<div class="cqa-loading"><span class="cqa-spinner cqa-spinner--lg"></span>Загружаю журнал…</div>';
    this.load();
  }

  async load() {
    try {
      const [runs, meta, asks] = await Promise.all([this.api.runs(), this.s.meta ? Promise.resolve(this.s.meta) : this.api.meta(), this.api.mock ? [] : this.api.asks()]);
      if (!this.alive) return;
      this.s.runs = [...(runs || [])].sort((a, b) => (b.id ?? 0) - (a.id ?? 0));
      this.s.meta = meta;
      this.s.asks = asks || [];
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
    const cost = evaluationCost + this.s.asks.reduce((sum, ask) => sum + (ask.cost_usd || 0), 0);
    const per = evals ? evaluationCost / evals : null;
    const lat = runs.filter((r) => r.avg_latency_s != null && r.done);
    const avgLat = lat.length ? lat.reduce((a, r) => a + r.avg_latency_s * r.done, 0) / lat.reduce((a, r) => a + r.done, 0) : null;
    const models = [...new Set(runs.map((r) => providerLabel(this.s.meta, r.provider, r.model)))];
    this.innerHTML = `
      <div class="cqa-page-head">
        <div><h1>Журнал прогонов</h1>
          <div class="cqa-page-sub">Модель оценивает изменённые критерии, остальные ответы переносим по отпечаткам определений. Здесь — модель, чек-лист, вызовы, время, токены и стоимость.</div></div>
        <div class="cqa-page-actions"><button type="button" class="cqa-btn" data-act="reload">${icon('refresh')}Обновить</button></div>
      </div>
      <div class="cqa-kpis">
        <div class="cqa-kpi"><div class="cqa-kpi-label">${icon('play')}Прогонов</div><div class="cqa-kpi-value">${fmtInt(runs.length)}</div></div>
        <div class="cqa-kpi"><div class="cqa-kpi-label">${icon('check')}Оценок</div><div class="cqa-kpi-value">${fmtInt(evals)}</div></div>
        <div class="cqa-kpi"><div class="cqa-kpi-label">${icon('coins')}Потрачено всего</div><div class="cqa-kpi-value">${fmtMoney(cost)}</div></div>
        <div class="cqa-kpi"><div class="cqa-kpi-label">${icon('coins')}Одна оценка</div><div class="cqa-kpi-value">${per == null ? '—' : fmtMoney(per)} <small>все вызовы</small></div></div>
        <div class="cqa-kpi"><div class="cqa-kpi-label">${icon('clock')}Время оценки</div><div class="cqa-kpi-value">${avgLat == null ? '—' : fmtSeconds(avgLat)} <small>в среднем</small></div></div>
      </div>
      ${models.length ? `<div class="cqa-note cqa-note--mint" style="margin-bottom:14px">${icon('cpu')} ${models.length === 1 ? 'Модель' : 'Модели'}: <b>${models.map(esc).join(', ')}</b>${per != null ? ` · примерная стоимость одной оценки со всеми вызовами — <b>${fmtMoney(per)}</b>` : ''}.</div>` : ''}
      <div class="cqa-panel"><div class="cqa-table-wrap">${runs.length ? this.tableHtml(runs) : `<div class="cqa-empty"><div class="cqa-empty-icon">${icon('journal')}</div><h3>Прогонов ещё не было</h3><p>Запустите оценку на экране «Диалоги» или примените чек-лист к набору.</p></div>`}</div></div>
      <section class="cqa-ask-log"><h2>Вопросы ИИ <span class="cqa-faint">${fmtInt(this.s.asks.length)} · ${fmtMoney(this.s.asks.reduce((sum, ask) => sum + (ask.cost_usd || 0), 0))}</span></h2>
        ${this.s.asks.length ? this.s.asks.map((ask) => `<div class="cqa-ask-log-item">
          <button class="cqa-btn cqa-btn--ghost cqa-ask-log-head" type="button" data-ask-toggle aria-expanded="false">
            ${icon('right')}<span class="cqa-ask-log-question">${esc(ask.question)}</span><span class="cqa-faint">${esc(fmtDateTime(ask.created_at))} · ${fmtMoney(ask.cost_usd)}${ask.status !== 'ok' ? ` · ${ask.status === 'pending' ? 'ожидание' : 'ошибка'}` : ''}</span></button>
          <div class="cqa-ask-reveal" inert aria-hidden="true"><div><div class="cqa-ask-log-body">
            ${ask.status === 'ok' ? askAnswerHtml(ask) : `<p class="cqa-muted">${esc(ask.error || 'Запрос ещё не завершён.')}</p>`}
            <div class="cqa-faint cqa-ask-meta">${esc(ask.model)} · токены ${fmtInt(ask.input_tokens)} / ${fmtInt(ask.output_tokens)}</div>
          </div></div></div></div>`).join('') : '<p class="cqa-muted">Вопросов ещё нет. <a href="#/ask">Спросить ИИ</a></p>'}</section>`;
    this.querySelector('[data-act="reload"]')?.addEventListener('click', () => this.load());
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
          <td>${esc(providerLabel(this.s.meta, r.provider, r.model))}<div class="cqa-cell-sub cqa-mono">${esc(r.model || '')}</div><div class="cqa-cell-sub">${r.mode === 'incremental' ? 'Инкрементально' : 'Полностью'} · вызовов ${fmtInt(r.model_calls || 0)}<br>Критериев оценено ${fmtInt(r.evaluated || 0)}, перенесено ${fmtInt(r.reused || 0)}</div></td>
          <td class="cqa-col-right cqa-num">${fmtInt(r.total)}</td>
          <td class="cqa-col-right cqa-num">${fmtInt(r.done)}${r.failed ? ` / <span style="color:var(--cqa-no)">${fmtInt(r.failed)}</span>` : ' / 0'}</td>
          <td class="cqa-col-right cqa-num">${fmtClock(dur)}</td>
          <td class="cqa-col-right cqa-num">${r.tokens_in != null ? `${fmtInt(r.tokens_in)} / ${fmtInt(r.tokens_out)}` : '—'}</td>
          <td class="cqa-col-right cqa-num"><b>${fmtMoney(r.cost_usd || 0)}</b></td>
          <td class="cqa-col-right cqa-num">${per == null ? '—' : fmtMoney(per)}</td>
          <td class="cqa-col-right cqa-num">${r.avg_latency_s == null ? '—' : fmtSeconds(r.avg_latency_s)}</td>
          <td>${st}</td></tr>`;
  }).join('')}</tbody></table>
      <div class="cqa-faint" style="font-size:12px;padding:10px 14px">Всего ${fmtInt(runs.length)} ${plural(runs.length, 'прогон', 'прогона', 'прогонов')}. Стоимость — по токенам и тарифу модели.</div>`;
  }
}

customElements.define('cqa-journal', Journal);
