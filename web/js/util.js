// Общие утилиты интерфейса CQA: экранирование, форматы, ответы по критериям, иконки, роль, сообщения.

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const nf = (min, max) => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: min, maximumFractionDigits: max });
const NF2 = nf(2, 2), NF1 = nf(0, 1), NF0 = nf(0, 0);

export const fmtScore = (x) => (x == null || Number.isNaN(+x) ? '—' : NF2.format(+x));
export const fmtPts = (x) => (x == null || Number.isNaN(+x) ? '—' : nf(0, 2).format(+x));
export const fmtInt = (x) => (x == null || Number.isNaN(+x) ? '—' : NF0.format(+x));
export function fmtMoney(x) {
  if (x == null || Number.isNaN(+x)) return '—';
  const v = +x;
  const d = v === 0 ? 2 : v >= 1 ? 2 : v >= 0.01 ? 3 : v >= 0.0001 ? 5 : 6;
  return '$' + nf(d, d).format(v);
}
export function fmtSeconds(s) {
  if (s == null || Number.isNaN(+s)) return '—';
  const v = +s;
  if (v < 60) return `${NF1.format(v)} с`;
  const m = Math.floor(v / 60), r = Math.round(v % 60);
  return r ? `${m} мин ${r} с` : `${m} мин`;
}
export function fmtClock(s) {
  if (s == null || Number.isNaN(+s)) return '—';
  const v = Math.max(0, Math.round(+s));
  const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60), r = v % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(r).padStart(2, '0')}`;
}
export function parseDate(v) {
  if (!v) return null;
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : String(v).replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? null : d;
}
export function fmtDateTime(v) {
  const d = parseDate(v);
  if (!d) return v ? String(v) : '—';
  const now = new Date();
  const hm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `сегодня, ${hm}`;
  return `${d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })}, ${hm}`;
}
export function secondsBetween(a, b) {
  const x = parseDate(a), y = parseDate(b) || new Date();
  return x ? Math.max(0, (y - x) / 1000) : null;
}
/** Исходная метка времени реплики: чат — как есть (MM:SS), звонок (ISO 8601) — часы:минуты:секунды. */
export function fmtTs(ts) {
  const m = /T(\d{2}:\d{2}:\d{2})/.exec(ts || '');
  return m ? m[1] : (ts || '');
}
export function fmtDuration(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'number') return fmtClock(v);
  return String(v);
}

/* ---------- ответы по критериям ---------- */
export const ROLE_LABEL = { client: 'Клиент', operator: 'Оператор', bot: 'Бот', system: 'Система' };
export const CHANNEL_LABEL = (ch) => (isCall(ch) ? 'Звонок' : 'Чат');
export const isCall = (ch) => /voice|call|звон/i.test(ch || '');
export const TYPE_LABEL = { bool: 'Да / Нет', scale: 'Шкала', flag: 'Грубая ошибка' };

export function topStep(crit) {
  if (crit?.steps?.length) return Math.max(...crit.steps.map((s) => +s.value));
  const nums = (crit?.allowed || []).filter((a) => a !== 'N/A' && a !== '' && !Number.isNaN(+a)).map(Number);
  return nums.length ? Math.max(...nums) : null;
}
export function answerTone(crit, a) {
  if (a == null || a === '') return 'na';
  if (a === 'N/A') return 'na';
  if (crit.type === 'flag') return a === 'Да' ? 'gross' : 'ok';
  if (crit.type === 'scale') {
    const top = topStep(crit);
    const r = top ? +a / top : 0;
    return r >= 0.999 ? 'yes' : r >= 0.5 ? 'mid' : 'no';
  }
  return a === 'Да' ? 'yes' : 'no';
}
export function answerLabel(crit, a) {
  if (a == null || a === '') return '—';
  if (a === 'N/A') return 'N/A';
  if (crit.type === 'flag') return a === 'Да' ? 'Нарушение' : 'Нет нарушений';
  if (crit.type === 'scale') { const t = topStep(crit); return t != null ? `${a} из ${t}` : String(a); }
  return a;
}
export function evidenceLabel(crit, a) {
  if (a === 'N/A') return 'Почему не применимо';
  if (crit.type === 'flag') return a === 'Да' ? 'Где нарушение' : 'Что проверено';
  if (a === 'Нет') return 'Где этого не хватило';
  if (crit.type === 'scale' && answerTone(crit, a) === 'no') return 'Где этого не хватило';
  return 'На чём основано';
}
export function scoreTone(total) {
  if (total == null) return 'none';
  return total >= 85 ? 'hi' : total >= 60 ? 'mid' : 'lo';
}
export const pctTone = (p) => (p == null ? 'na' : p >= 85 ? 'hi' : p >= 60 ? 'mid' : 'lo');

/* ---------- ссылки на реплики в обосновании ---------- */
function refLink(n, maxN, prefix = '') {
  const k = +n;
  if (!maxN || k < 1 || k > maxN) return prefix + n;
  return `<a href="#" class="cqa-ref" data-ref="${k}" title="Показать в диалоге: №${k}">${prefix}${n}</a>`;
}
/** Экранирует текст и превращает «[12]», «[12, 14]», «реплика 12», «#12» в ссылки на реплики. */
export function linkifyRefs(text, maxN) {
  let s = esc(text);
  s = s.replace(/\[(\d+(?:\s*(?:,|;|–|—|-|и|and)\s*\d+)*)\]/g,
    (m, inner) => '[' + inner.replace(/\d+/g, (n) => refLink(n, maxN)) + ']');
  s = s.replace(/((?:реплик[аеиуойам]{0,3}|сообщени[еяюй]{1,2}|replies|reply|messages|message|line)\s+)(\d+(?:\s*(?:,|и|and|–|-)\s*\d+)*)(?![^<]*<\/a>)/gi,
    (m, pre, nums) => pre + nums.replace(/\d+/g, (n) => refLink(n, maxN)));
  s = s.replace(/(^|[\s(])#(\d+)\b/g, (m, pre, n) => pre + refLink(n, maxN, '#'));
  return s;
}

/* ---------- иконки (линейные, currentColor) ---------- */
// Значки — Lucide 1.61.0 (lucide.dev), ISC License, © Lucide Icons and Contributors; текст лицензии — web/LICENSE-lucide.txt.
// Имена ключей — наши (по смыслу в интерфейсе), рисунки — из Lucide.
const P = {
  chat: '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/> <path d="M7 11h10"/> <path d="M7 15h6"/> <path d="M7 7h8"/>',
  phone: '<path d="M13.832 16.568a1 1 0 0 0 1.213-.303l.355-.465A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.468.351a1 1 0 0 0-.292 1.233 14 14 0 0 0 6.392 6.384"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/> <path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/> <path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/> <path d="M12 9v4"/> <path d="M12 17h.01"/>',
  back: '<path d="m12 19-7-7 7-7"/> <path d="M19 12H5"/>',
  right: '<path d="m9 18 6-6-6-6"/>',
  arrow: '<path d="M7 7h10v10"/> <path d="M7 17 17 7"/>',
  play: '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/> <path d="M21 3v5h-5"/> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/> <path d="M8 16H3v5"/>',
  upload: '<path d="M12 3v12"/> <path d="m17 8-5-5-5 5"/> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>',
  download: '<path d="M12 15V3"/> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/> <path d="m7 10 5 5 5-5"/>',
  plus: '<path d="M5 12h14"/> <path d="M12 5v14"/>',
  trash: '<path d="M10 11v6"/> <path d="M14 11v6"/> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/> <path d="M3 6h18"/> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  search: '<path d="m21 21-4.34-4.34"/> <circle cx="11" cy="11" r="8"/>',
  flag: '<path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528"/>',
  gavel: '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/> <path d="M12 15h.01"/> <path d="M12 7v4"/>',
  scale: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/> <path d="M18 17V9"/> <path d="M13 17V5"/> <path d="M8 17v-3"/>',
  list: '<path d="M3 5h.01"/> <path d="M3 12h.01"/> <path d="M3 19h.01"/> <path d="M8 5h13"/> <path d="M8 12h13"/> <path d="M8 19h13"/>',
  journal: '<path d="M15 12h-5"/> <path d="M15 8h-5"/> <path d="M19 17V5a2 2 0 0 0-2-2H4"/> <path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
  checklist: '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/> <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/> <path d="m9 14 2 2 4-4"/>',
  user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/> <circle cx="12" cy="7" r="4"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/> <path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>',
  coins: '<path d="M13.744 17.736a6 6 0 1 1-7.48-7.48"/> <path d="M15 6h1v4"/> <path d="m6.134 14.768.866-.5 2 3.464"/> <circle cx="16" cy="8" r="6"/>',
  cpu: '<path d="M12 20v2"/> <path d="M12 2v2"/> <path d="M17 20v2"/> <path d="M17 2v2"/> <path d="M2 12h2"/> <path d="M2 17h2"/> <path d="M2 7h2"/> <path d="M20 12h2"/> <path d="M20 17h2"/> <path d="M20 7h2"/> <path d="M7 20v2"/> <path d="M7 2v2"/> <rect x="4" y="4" width="16" height="16" rx="2"/> <rect x="8" y="8" width="8" height="8" rx="1"/>',
  spark: '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/> <path d="M20 2v4"/> <path d="M22 4h-4"/> <circle cx="4" cy="20" r="2"/>',
  file: '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/> <path d="M14 2v5a1 1 0 0 0 1 1h5"/> <path d="M10 9H8"/> <path d="M16 13H8"/> <path d="M16 17H8"/>',
  edit: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/> <path d="m15 5 4 4"/>',
  eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/> <circle cx="12" cy="12" r="3"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/> <path d="M3 5V19A9 3 0 0 0 21 19V5"/> <path d="M3 12A9 3 0 0 0 21 12"/>',
  info: '<circle cx="12" cy="12" r="10"/> <path d="M12 16v-4"/> <path d="M12 8h.01"/>',
  help: '<circle cx="12" cy="12" r="10"/> <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/> <path d="M12 17h.01"/>',
  logo: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 3 13.5Z" fill="none"/><path d="m8.5 9.5 2.2 2.2 4.8-4.7"/>',
};
export function icon(name, cls = '') {
  return `<svg class="cqa-icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
}

/* ---------- роль (без входа) ---------- */
const ROLE_KEY = 'cqa.role';
export function getRole() {
  let r = window.__cqaRole;
  try { r = localStorage.getItem(ROLE_KEY) || r; } catch { /* приватный режим */ }
  return r === 'reviewer' ? 'reviewer' : 'operator';
}
export function setRole(r) {
  window.__cqaRole = r;
  try { localStorage.setItem(ROLE_KEY, r); } catch { /* приватный режим */ }
  document.documentElement.dataset.cqaRole = r;
  window.dispatchEvent(new CustomEvent('cqa-role', { detail: r }));
}
export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* приватный режим */ }
  return null;
}

/* ---------- всплывающие сообщения ---------- */
export function toast(message, tone = 'info', ms = 4200) {
  let box = document.querySelector('.cqa-toasts');
  if (!box) { box = document.createElement('div'); box.className = 'cqa-toasts'; box.setAttribute('role', 'status'); document.body.append(box); }
  const t = document.createElement('div');
  t.className = `cqa-toast cqa-toast--${tone}`;
  t.innerHTML = `${icon(tone === 'error' ? 'alert' : tone === 'warn' ? 'info' : 'check')}<div>${esc(message)}</div>`;
  box.append(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 320); }, ms);
}

export function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
export const clone = (x) => (x == null ? x : JSON.parse(JSON.stringify(x)));
export const navigate = (hash) => { if (location.hash !== hash) location.hash = hash; else window.dispatchEvent(new HashChangeEvent('hashchange')); };
export function emit(name, detail) { window.dispatchEvent(new CustomEvent(name, { detail })); }

/** Опрос прогона раз в interval мс до завершения. Возвращает функцию остановки. */
export function pollRun(api, runId, onUpdate, interval = 1000) {
  let stopped = false, timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const run = await api.run(runId);
      if (stopped) return;
      onUpdate(run, null);
      if (isRunFinished(run)) { stopped = true; return; }
    } catch (e) { if (!stopped) onUpdate(null, e); }
    if (!stopped) timer = setTimeout(tick, interval);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}
export const isRunFinished = (run) => !!run && ['done', 'finished', 'completed', 'error', 'failed', 'cancelled'].includes(run.status);

/** Подписи модели по id провайдера из /api/meta. */
export function providerLabel(meta, id, model) {
  const p = (meta?.providers || []).find((x) => x.id === id);
  return p?.label || model || id || '—';
}
export function checklistLabel(c) {
  if (!c) return '—';
  return c.label || `${c.name || 'Чек-лист'} · v${c.version ?? '?'}`;
}

/** Русское склонение: plural(3, 'диалог', 'диалога', 'диалогов') → «диалога». */
export function plural(n, one, few, many) {
  const a = Math.abs(+n || 0) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

// Подсказки к моделям в выпадающих списках (атрибут data-desc у <option>).
export const PROVIDER_DESC = {
  haiku: 'По умолчанию · 3 прогона на оценку, ≈ $0,004',
  sonnet: 'Крупнее и примерно в 10 раз дороже',
  local: 'Открытая модель на этом компьютере — данные не уходят в облако',
  fake: 'Без модели — только для тестов',
};
