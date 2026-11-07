// Справочные метрики для демо и загруженных в демо звонков; контракт cqa/callmetrics.py.
const EVENTS = [
  ['hold_end', /клиент больше не находится на удержании|(?:client|customer) is no longer on hold|(?:client|customer) (?:was |has been )?taken off hold|клиент (?:енді )?(?:ұстап тұру|күту) режимінде емес|клиент (?:ұстап тұру|күту) режимінен (?:шығарылды|шықты)/u],
  ['hold_start', /клиент находится на удержании|(?:client|customer) is on hold|(?:client|customer) (?:was |has been )?(?:placed|put) on hold|клиент (?:ұстап тұру|күту) режимінде|клиент (?:ұстап тұру|күту) режиміне (?:қойылды|ауыстырылды)/u],
  ['recording_pause', /(?:запис|транскри).*приостановлен|(?:жазу|транскри).*(?:тоқтатыл|кідіртіл)|(?:record|transcri).*paus/u],
  ['recording_resume', /(?:запис|транскри).*возобновлен|(?:жазу|транскри).*жалғас|(?:record|transcri).*resum/u],
  ['recording_start', /(?:запис|транскри).*запущен|(?:жазу|транскри).*басталды|(?:record|transcri).*start/u],
];
const round = (v, places = 3) => Math.round(v * 10 ** places) / 10 ** places;

export function callMetrics(record) {
  const tr = record.transcript || [];
  if (record.channel !== 'voice call' || !tr.length) return null;
  const warnings = [];
  let naive = false;
  const times = tr.map((t) => {
    if (typeof t.timestamp !== 'string' || !t.timestamp.includes('T')) return NaN;
    let stamp = t.timestamp.trim().replace(/(\.\d{3})\d+/, '$1');
    if (!/(?:Z|[+-]\d\d:\d\d)$/.test(stamp)) { stamp += 'Z'; naive = true; }
    return Date.parse(stamp);
  });
  if (times.some(Number.isNaN)) return null;
  if (naive) warnings.push('Метки без часового пояса приняты за UTC.');
  if (times.some((t, i) => i && t < times[i - 1])) warnings.push('Метки идут не по времени: отрицательные интервалы приняты за 0 с.');
  const seconds = (a, b) => Math.max(0, (times[b] - times[a]) / 1000);
  const interval = (a, b, open = false) => ({ start: tr[a].timestamp, end: tr[b].timestamp,
    start_n: a + 1, end_n: open ? null : b + 1, duration_s: round(seconds(a, b)), open });
  const holds = [], recordingPauses = [], unknown = [], pauses = [];
  const opened = { hold: null, recording: null };
  const groups = { hold: holds, recording: recordingPauses };
  const speech = Object.fromEntries(['operator', 'client'].map((role) => [role, { seconds: 0, words: 0, pace_seconds: 0 }]));
  tr.forEach((t, i) => {
    const role = t.speaker;
    if (role === 'system') {
      const text = (t.text || '').toLowerCase().replaceAll('ё', 'е').replace(/\s+/g, ' ').trim();
      const kind = EVENTS.find(([, re]) => re.test(text))?.[0];
      if (!kind) unknown.push({ n: i + 1, timestamp: t.timestamp, text: t.text || '' });
      else if (kind !== 'recording_start') {
        const [group, action] = kind.split('_');
        if (['start', 'pause'].includes(action) && opened[group] == null) opened[group] = i;
        else if (['end', 'resume'].includes(action) && opened[group] != null) {
          groups[group].push(interval(opened[group], i)); opened[group] = null;
        }
      }
    }
    if (i + 1 === tr.length) return;
    const gap = seconds(i, i + 1);
    if (speech[role]) {
      const words = (t.text.match(/[\p{L}\p{N}]+(?:[-'’][\p{L}\p{N}]+)*/gu) || []).length;
      const estimated = words ? Math.min(gap, words * 0.6 + 2) : 0;
      speech[role].seconds += estimated;
      if (words >= 3 && estimated > 0) { speech[role].words += words; speech[role].pace_seconds += estimated; }
    }
    if (role === 'client' && tr[i + 1].speaker === 'operator') pauses.push({ client_n: i + 1, operator_n: i + 2, duration_s: gap });
  });
  for (const [group, start] of Object.entries(opened)) if (start != null) groups[group].push(interval(start, tr.length - 1, true));
  const total = Object.values(speech).reduce((a, s) => a + s.seconds, 0);
  for (const [role, s] of Object.entries(speech)) speech[role] = { seconds: round(s.seconds),
    share: total ? round(s.seconds / total, 4) : null, words_per_minute: s.pace_seconds ? round(s.words * 60 / s.pace_seconds, 1) : null };
  const gaps = pauses.map((p) => p.duration_s).sort((a, b) => a - b), mid = Math.floor(gaps.length / 2);
  return { estimated: true, note: 'оценка по меткам времени расшифровки, не по звуку', duration_s: round(seconds(0, tr.length - 1)),
    hold_s: round(holds.reduce((a, h) => a + h.duration_s, 0)), holds,
    recording_pause_s: round(recordingPauses.reduce((a, p) => a + p.duration_s, 0)), recording_pauses: recordingPauses, speech,
    response_pauses: { count: gaps.length, median_s: gaps.length ? round(gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2) : null,
      max_s: gaps.length ? round(gaps.at(-1)) : null, long: pauses.filter((p) => p.duration_s > 10).map((p) => ({ ...p, duration_s: round(p.duration_s) })) },
    unknown_events: unknown, warnings };
}
