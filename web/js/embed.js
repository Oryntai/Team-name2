// Пример встраивания: один компонент <cqa-card> на чужой странице (форма CRM). Параметры: ?c=<id диалога>&api=<база API>.
import './components/card.js';

const params = new URLSearchParams(location.search);
const id = params.get('c');
const slot = document.querySelector('[data-embed="slot"]');
document.querySelector('[data-embed="id"]').textContent = id || '—';
if (id) {
  const card = document.createElement('cqa-card');
  card.setAttribute('conversation-id', id);
  card.setAttribute('api-base', params.get('api') || '/api');
  card.setAttribute('embedded', '');
  slot.append(card);
} else {
  slot.innerHTML = '<p class="cqa-muted">Укажите диалог: <code>embed.html?c=p1-ru-025</code></p>';
}
