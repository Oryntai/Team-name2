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
  if (a === 'N/A') return 'Почему N/A';
  if (crit.type === 'flag') return a === 'Да' ? 'Доказательство' : 'Проверенный участок';
  if (a === 'Нет') return 'Где ожидалось';
  if (crit.type === 'scale' && answerTone(crit, a) === 'no') return 'Где ожидалось';
  return 'Доказательство';
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
const P = {
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 21 12Z"/>',
  phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2Z"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  right: '<path d="m9 18 6-6-6-6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  play: '<path d="m6 4 14 8-14 8Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5M12 3v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  flag: '<path d="M4 22V4M4 4h13l-2 4 2 4H4"/>',
  gavel: '<path d="m14 13-7.5 7.5a2.1 2.1 0 0 1-3-3L11 10M16 16l6-6M8 8l6-6M9 7l8 8M21 11l-8-8"/>',
  scale: '<path d="M12 3v18M7 21h10M5 7h14M5 7l-3 7a4 4 0 0 0 6 0Zm14 0-3 7a4 4 0 0 0 6 0Z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  journal: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15Z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5M8 7h8M8 11h6"/>',
  checklist: '<path d="M9 11l2 2 4-4"/><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 17h8"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  coins: '<circle cx="8" cy="8" r="6"/><path d="M18.1 10.4A6 6 0 1 1 10.3 18M7 6h1v4M16.7 13.9l.7.7-2.8 2.8"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  spark: '<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4Z"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
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
