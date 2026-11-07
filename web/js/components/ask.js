// Самостоятельный <cqa-ask api-base="/api" dataset-id="DEV" checklist-id="1">.
import { CqaElement } from '../base.js';
import { esc, icon, toast, fmtMoney, fmtInt } from '../util.js';
import { enhance } from '../dropdown.js';

const EXAMPLES = [
  'Где операторы раскрывали данные без проверки личности?',
  'Какие темы чаще получают низкий балл?',
  'Что чаще всего забывают при завершении?',
  'Покажи диалоги, где модель сомневалась',
];

// Общее раскрытие для ответа, фильтров и журнала: CSS анимирует оба направления.
export function toggleAskDisclosure(button) {
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(open));
  const body = button.nextElementSibling;
  body.classList.toggle('is-open', open);
  body.inert = !open;
  body.setAttribute('aria-hidden', String(!open));
}

export function askAnswerHtml(result) {
  const refs = result.refs || [];
  let answer = esc(result.answer);
  for (const cid of new Set(refs.map((ref) => ref.conversation_id))) {
    answer = answer.replaceAll(`[${esc(cid)}]`, `<a href="#/c/${encodeURIComponent(cid)}">[${esc(cid)}]</a>`);
  }
  return `<p class="cqa-ask-answer">${answer}</p>
    ${refs.length ? `<button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-ask-toggle aria-expanded="false">${icon('right')}Диалоги · ${fmtInt(refs.length)}</button>
      <div class="cqa-ask-reveal" inert aria-hidden="true"><div><ul class="cqa-ask-refs">${refs.map((ref) => `<li>
        <a href="#/c/${encodeURIComponent(ref.conversation_id)}">${esc(ref.conversation_id)}${ref.criterion_id ? ` · ${esc(ref.criterion_id)}` : ''}${icon('arrow')}</a>
        <span class="cqa-muted">${esc(ref.why)}</span></li>`).join('')}</ul></div></div>` : ''}
    <div class="cqa-faint cqa-ask-meta">${esc(result.model)} · ${fmtMoney(result.cost_usd)}</div>`;
}

class Ask extends CqaElement {
  init() {
    this.busy = false;
    const compact = this.hasAttribute('compact');
    this.innerHTML = ` ${compact ? '' : `<div class="cqa-page-head"><div><h1>Спросить ИИ</h1>
      <div class="cqa-page-sub">Ответ по оценённым диалогам со ссылками на карточки.</div></div></div>`}
      <div class="cqa-ask-entry"><form class="cqa-ask-form">
        <div class="cqa-ask-compose"><input aria-label="Вопрос по диалогам" class="cqa-input" data-part="question" name="question" maxlength="4000" required autocomplete="off" placeholder="${compact ? 'Спросить ИИ о диалогах…' : 'Что хотите узнать по диалогам?'}">
          <button class="cqa-btn cqa-btn--primary" type="submit" data-part="submit">${icon('spark')}Спросить</button></div>
        ${compact ? '' : `<button type="button" class="cqa-btn cqa-btn--ghost cqa-btn--sm" data-ask-toggle aria-expanded="false">${icon('right')}Набор и чек-лист</button>
        <div class="cqa-ask-reveal" inert aria-hidden="true"><div><div class="cqa-ask-filters" data-part="filters"></div></div></div>`}
      </form>
      <div class="cqa-ask-examples">${EXAMPLES.map((text, i) => `<button type="button" class="cqa-btn cqa-btn--ghost" data-example="${i}">${esc(compact ? ['Данные без проверки?', 'Темы с низким баллом?', 'Что забывают?', 'Где сомнения?'][i] : text)}</button>`).join('')}</div></div>
      <div class="cqa-ask-result" data-part="result" role="status" aria-live="polite" aria-atomic="true"></div>`;
    this.listen(this, 'click', (event) => {
      const toggle = event.target.closest('[data-ask-toggle]');
      if (toggle) toggleAskDisclosure(toggle);
      const example = event.target.closest('[data-example]');
      if (example && !this.busy) {
        this.part('question').value = EXAMPLES[+example.dataset.example];
        this.part('question').focus();
      }
    });
    this.listen(this.querySelector('form'), 'submit', (event) => { event.preventDefault(); this.submit(); });
    if (!compact) this.loadFilters();
    if (this.hasAttribute('autofocus')) this.part('question').focus();
  }

  async loadFilters() {
    try {
      const [datasets, checklists] = await Promise.all([this.api.datasets(), this.api.checklists()]);
      if (!this.alive) return;
      this.part('filters').innerHTML = `<label class="cqa-field">Набор<select class="cqa-select" data-part="dataset"><option value="">Все наборы</option>${datasets.map((d) => `<option value="${esc(d.id)}">${esc(d.id)}</option>`).join('')}</select></label>
        <label class="cqa-field">Чек-лист<select class="cqa-select" data-part="checklist"><option value="">Последние оценки</option>${checklists.map((c) => `<option value="${c.id}">${esc(c.label || `${c.name} · v${c.version}`)}</option>`).join('')}</select></label>`;
      for (const [part, attribute] of [['dataset', 'dataset-id'], ['checklist', 'checklist-id']]) {
        const select = this.part(part);
        select.value = this.getAttribute(attribute) || '';
        select.disabled = this.busy;
        enhance(select);
      }
    } catch (error) {
      if (this.alive) this.part('filters').textContent = `Фильтры недоступны: ${error.message}`;
    }
  }

  async submit() {
    if (this.busy) return;
    const question = this.part('question').value.trim();
    if (!question) { this.part('question').focus(); return; }
    if (this.api.mock) { toast('Для вопросов нужен сервер CQA.', 'info'); return; }
    const body = { question };
    const dataset = this.part('dataset') ? this.part('dataset').value : this.getAttribute('dataset-id');
    const checklist = this.part('checklist') ? this.part('checklist').value : this.getAttribute('checklist-id');
    if (dataset) body.dataset_id = dataset;
    if (checklist) body.checklist_id = +checklist;
    this.setBusy(true);
    this.part('result').innerHTML = '<span class="cqa-spinner"></span> Ищу в оценках…';
    try {
      const result = await this.api.ask(body);
      if (this.alive) this.part('result').innerHTML = askAnswerHtml(result);
    } catch (error) {
      if (this.alive) {
        this.part('result').innerHTML = `<div class="cqa-error-box">${icon('alert')}${esc(error.message)}</div>`;
        toast(error.message, 'error');
      }
    } finally {
      if (this.alive) this.setBusy(false);
    }
  }

  setBusy(busy) {
    this.busy = busy;
    this.querySelectorAll('input, select, [type="submit"], [data-example]').forEach((el) => { el.disabled = busy; });
    this.part('result').setAttribute('aria-busy', String(busy));
  }
}

customElements.define('cqa-ask', Ask);
