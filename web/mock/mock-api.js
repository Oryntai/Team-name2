// Демо-режим интерфейса без сервера (?mock=1): выдуманные диалоги из web/mock/*.json, состояние — в памяти вкладки.
// Повторяет контракт docs/ARCHITECTURE.md. Модель не вызывается: «оценка» в демо-прогоне копирует ответы из
// готовых демо-оценок — это только для проверки интерфейса, в продукте каждую оценку делает модель.

const BASE = new URL('./', import.meta.url);
const clone = (x) => (x == null ? x : JSON.parse(JSON.stringify(x)));
const wait = (ms = 140) => new Promise((r) => setTimeout(r, ms));
const now = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); };

class MockError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body || { detail: message }; }
}

/* ---------- формула чек-листа (как cqa/scoring.py) ---------- */
function allowedAnswers(c) {
  const vals = c.type === 'scale' ? [...c.steps].sort((a, b) => b.value - a.value).map((s) => String(s.value)) : ['Да', 'Нет'];
  if (c.na) vals.push('N/A');
  return vals;
}
const r2 = (x, d = 2) => Math.round((x + Number.EPSILON) * 10 ** d) / 10 ** d;
function score(cfg, answers) {
  const rows = {}, raw = {}, gross = [];
  let den = 0;
  for (const c of cfg.criteria) {
    const a = answers[c.id];
    const w = +c.weight || 0;
    rows[c.id] = { answer: a, applicable: false, weight: w, points: 0, weight_eff: 0, points_eff: 0 };
    if (c.type === 'flag') { if (a === 'Да') gross.push(c.id); continue; }
    if (a === 'N/A' || !w) continue;
    const top = c.type === 'scale' ? Math.max(...c.steps.map((s) => +s.value)) : 1;
    const pts = c.type === 'bool' ? (a === 'Да' ? w : 0) : (w * +a) / top;
    raw[c.id] = [w, pts]; den += w;
  }
  const k = den > 0 ? 100 / den : 0;
  for (const [id, [w, p]] of Object.entries(raw)) Object.assign(rows[id], { applicable: true, points: r2(p, 4), weight_eff: r2(w * k), points_eff: r2(p * k) });
  const names = [];
  cfg.criteria.forEach((c) => { const s = (c.section || '').trim() || 'Без секции'; if (!names.includes(s)) names.push(s); });
  const sections = names.map((name) => {
    const ids = cfg.criteria.filter((c) => ((c.section || '').trim() || 'Без секции') === name).map((c) => c.id);
    const mx = ids.reduce((a, i) => a + (raw[i] ? raw[i][0] : 0), 0) * k;
    const pt = ids.reduce((a, i) => a + (raw[i] ? raw[i][1] : 0), 0) * k;
    return { name, ids, points_eff: r2(pt), max_eff: r2(mx), percent: mx > 0 ? r2((pt / mx) * 100) : null };
  });
  const total = den > 0 ? r2(Object.values(raw).reduce((a, [, p]) => a + p, 0) * k, cfg.rounding ?? 2) : null;
  return { total, gross: cfg.criteria.some((c) => c.type === 'flag') ? gross.length > 0 : null, gross_ids: gross, criteria: rows, sections };
}

function validate(cfg) {
  const errs = [];
  if (!String(cfg?.name || '').trim()) errs.push('Укажите название чек-листа.');
  const cs = cfg?.criteria || [];
  if (!cs.length) return [...errs, 'Добавьте хотя бы один критерий.'];
  const seen = new Set();
  let weighted = 0;
  cs.forEach((c, i) => {
    const name = `Критерий ${c.id || `№${i + 1}`}`;
    if (!c.id) errs.push(`${name}: нет номера.`); else if (seen.has(c.id)) errs.push(`${name}: номер повторяется.`);
    seen.add(c.id);
    if (!String(c.title || '').trim()) errs.push(`${name}: укажите название.`);
    if (c.type === 'flag') { if (+c.weight) errs.push(`${name}: грубая ошибка не даёт баллов — вес должен быть 0.`); }
    else if (!(+c.weight > 0)) errs.push(`${name}: вес критерия с баллами должен быть больше 0.`);
    else weighted++;
    if (!String(c.yes || '').trim()) errs.push(`${name}: опишите, когда ставится «Да»${c.type === 'scale' ? ' (или как выставлять шкалу)' : ''}.`);
    if (c.na && !String(c.na_when || '').trim()) errs.push(`${name}: N/A разрешён — укажите условие, когда он ставится.`);
    if (c.type === 'scale') {
      const st = c.steps || [];
      if (st.length < 2) errs.push(`${name}: у шкалы должно быть не меньше двух ступеней.`);
      st.forEach((s) => { if (!String(s.text || '').trim()) errs.push(`${name}: опишите ступень ${s.value}.`); });
      const vals = st.map((s) => +s.value);
      if (new Set(vals).size !== vals.length) errs.push(`${name}: значения ступеней повторяются.`);
      if (vals.length && Math.max(...vals) <= 0) errs.push(`${name}: верхняя ступень шкалы должна быть больше 0.`);
    }
  });
  if (!weighted) errs.push('Нет ни одного критерия с баллами — итог не посчитать.');
  return errs;
}

/* ---------- тихая запись для плеера звонка (WAV, генерируется на лету) ---------- */
function silentWav(seconds = 60, rate = 8000) {
  const n = seconds * rate;
  const buf = new ArrayBuffer(44 + n);
  const v = new DataView(buf);
  const w = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  w(0, 'RIFF'); v.setUint32(4, 36 + n, true); w(8, 'WAVE'); w(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true); w(36, 'data'); v.setUint32(40, n, true);
  new Uint8Array(buf, 44).fill(128);
  return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
}

export async function createMockApi() {
  const load = (name) => fetch(new URL(`${name}.json`, BASE)).then((r) => r.json());
  const [meta, datasets, checklists, convs, evals, runs, appeals] = await Promise.all(
    ['meta', 'datasets', 'checklists', 'conversations', 'evaluations', 'runs', 'appeals'].map(load));
  const st = { meta, datasets, checklists, convs, evals, runs, appeals, settings: { dataset_url_configured: true, dataset_url_hint: 'https://…/invoke?…&sig=***', default_provider: 'haiku' } };
  let audio = null;
  const nextId = (arr) => arr.reduce((m, x) => Math.max(m, +x.id || 0), 0) + 1;
  const cfgOf = (id) => st.checklists.find((c) => +c.id === +id)?.config;
  const label = (c) => `${c.name} · v${c.version}`;

  function serveEval(ev) {
    const e = clone(ev);
    const cfg = cfgOf(e.checklist.id);
    const orig = {}, fin = {};
    for (const c of e.criteria) {
      const a = st.appeals.filter((x) => +x.evaluation_id === +e.id && x.criterion_id === c.id).sort((x, y) => y.id - x.id)[0];
      c.appeal = a ? { id: a.id, comment: a.comment, status: a.status, new_answer: a.new_answer, reason: a.reason, created_at: a.created_at, decided_at: a.decided_at } : null;
      orig[c.id] = c.original.answer;
      fin[c.id] = a?.status === 'changed' ? a.new_answer : c.original.answer;
      c.final_answer = fin[c.id];
    }
    const so = score(cfg, orig), sf = score(cfg, fin);
    const pick = (r) => ({ applicable: r.applicable, points: r.points, weight_eff: r.weight_eff, points_eff: r.points_eff });
    for (const c of e.criteria) { c.score_original = pick(so.criteria[c.id]); c.score_final = pick(sf.criteria[c.id]); }
    e.original = { total: so.total, gross: so.gross, sections: so.sections };
    e.final = { total: sf.total, gross: sf.gross, sections: sf.sections };
    return e;
  }

  function evalSummary(ev) {
    const e = serveEval(ev);
    return { evaluation_id: e.id, checklist_id: e.checklist.id, checklist_label: e.checklist.label, provider: e.provider, model: e.model,
      created_at: e.created_at, status: e.status, total: e.original.total, final_total: e.final.total, gross: e.final.gross };
  }

  function latestEval(cid) {
    return st.evals.filter((e) => e.conversation_id === cid).sort((a, b) => b.id - a.id)[0] || null;
  }

  function makeEval(conv, checklistId, runId, provider) {
    const ch = st.checklists.find((c) => +c.id === +checklistId);
    const base = latestEval(conv.id);
    const crits = ch.config.criteria.map((c) => {
      const allowed = allowedAnswers(c);
      const prev = base?.criteria.find((x) => x.id === c.id);
      const same = prev && prev.title === c.title && allowed.includes(prev.original.answer);
      return { id: c.id, title: c.title, section: c.section, type: c.type, weight: c.weight, na: !!c.na, allowed,
        original: same ? clone(prev.original) : { answer: c.type === 'flag' ? 'Нет' : allowed[0], explanation: 'Демо-режим: ответ сгенерирован без модели для нового или изменённого критерия.', evidence: [] } };
    });
    const id = nextId(st.evals);
    const tin = 1900 + Math.round(Math.random() * 400), tout = 1100 + Math.round(Math.random() * 500);
    const p = st.meta.providers.find((x) => x.id === provider) || st.meta.providers[0];
    const ev = { id, run_id: runId, conversation_id: conv.id, checklist: { id: ch.id, name: ch.name, version: ch.version, label: label(ch) },
      provider: p.id, model: p.model, prompt_version: 'p1', created_at: now(), latency_s: +(6 + Math.random() * 5).toFixed(1),
      input_tokens: tin, output_tokens: tout, cost_usd: +((tin * p.price_in + tout * p.price_out) / 1e6).toFixed(6), status: 'ok', error: null,
      pii_masked: (conv.transcript || []).flatMap((m) => (m.pii || []).map((s) => ({ n: m.n, type: s.type, label: s.label }))), criteria: crits };
    st.evals.push(ev);
    return ev;
  }

  function serveRun(run) {
    const r = clone(run);
    r.checklist_label = label(st.checklists.find((c) => +c.id === +r.checklist_id) || { name: 'Чек-лист', version: '?' });
    const done = st.evals.filter((e) => e.run_id === r.id);
    r.tokens_in = done.reduce((a, e) => a + e.input_tokens, 0);
    r.tokens_out = done.reduce((a, e) => a + e.output_tokens, 0);
    r.avg_latency_s = done.length ? +(done.reduce((a, e) => a + e.latency_s, 0) / done.length).toFixed(2) : null;
    r.avg_cost_per_eval = r.done ? +(r.cost_usd / r.done).toFixed(6) : null;
    return r;
  }

  const api = {
    mock: true,
    async meta() { await wait(60); return clone(st.meta); },
    async settings() { await wait(); return clone(st.settings); },
    async saveSettings(b) { await wait(); if (b.dataset_url) st.settings.dataset_url_configured = true; return clone(st.settings); },
    async datasets() { await wait(); return clone(st.datasets); },
    async importDataset({ group_id }) {
      await wait(700);
      if (!group_id) throw new MockError(422, 'Укажите group_id');
      if (group_id.toUpperCase() !== 'DEMO') throw new MockError(404, `В демо-режиме есть только набор DEMO (запрошен «${group_id}»)`);
      return { dataset_id: 'DEMO', imported: st.convs.length, total: st.convs.length };
    },
    async uploadDataset(file) {
      await wait(400);
      let data;
      try { data = JSON.parse(await file.text()); } catch { throw new MockError(422, 'Файл — не JSON'); }
      const recs = data?.data?.records || (Array.isArray(data) ? data : data?.records || [data]);
      const ds = data?.data?.dataset_group_id || `upload-${Date.now().toString(36)}`;
      let n = 0;
      for (const r of recs) {
        if (!r?.conversation_id || !Array.isArray(r.transcript)) continue;
        st.convs = st.convs.filter((c) => c.id !== r.conversation_id);
        st.convs.push({ id: r.conversation_id, dataset_id: ds, channel: r.channel || 'agent chat', language: r.language || 'RU', audio_src: null,
          transcript: r.transcript.map((t, i) => ({ n: i + 1, timestamp: t.timestamp, speaker: t.speaker, text: t.text, audio_offset: null, pii: [] })) });
        n++;
      }
      if (!n) throw new MockError(422, 'В файле нет записей с conversation_id и transcript');
      st.datasets = st.datasets.filter((d) => d.id !== ds).concat([{ id: ds, source: 'файл', fetched_at: now(), total: n }]);
      return { dataset_id: ds, imported: n, total: n };
    },
    async conversations(params = {}) {
      await wait();
      return st.convs.filter((c) => !params.dataset_id || c.dataset_id === params.dataset_id).map((c) => {
        const l = latestEval(c.id);
        const evIds = st.evals.filter((e) => e.conversation_id === c.id).map((e) => e.id);
        const aps = st.appeals.filter((a) => evIds.includes(+a.evaluation_id));
        const t = c.transcript;
        const call = /voice/.test(c.channel);
        const dur = call && t.length ? Math.round((new Date(t[t.length - 1].timestamp) - new Date(t[0].timestamp)) / 1000) : (t[t.length - 1]?.timestamp || null);
        return { id: c.id, dataset_id: c.dataset_id, channel: c.channel, language: c.language, n_replies: t.length, duration: dur,
          latest: l ? evalSummary(l) : null, appeals: { open: aps.filter((a) => a.status === 'open').length, decided: aps.filter((a) => a.status !== 'open').length } };
      });
    },
    async conversation(id) {
      await wait();
      const c = st.convs.find((x) => x.id === id);
      if (!c) throw new MockError(404, 'Диалог не найден');
      const out = clone(c);
      if (out.audio_src === 'mock:audio') out.audio_src = (audio ||= silentWav(60));
      out.evaluations = st.evals.filter((e) => e.conversation_id === id).map(evalSummary).sort((a, b) => b.evaluation_id - a.evaluation_id);
      return out;
    },
    async evaluation(id) {
      await wait();
      const e = st.evals.find((x) => +x.id === +id);
      if (!e) throw new MockError(404, 'Оценка не найдена');
      return serveEval(e);
    },
    async startRun({ conversation_ids, dataset_id, checklist_id, provider }) {
      await wait(200);
      const ids = conversation_ids?.length ? conversation_ids : st.convs.filter((c) => c.dataset_id === dataset_id).map((c) => c.id);
      if (!ids.length) throw new MockError(422, 'Нет диалогов для оценки');
      if (!st.checklists.some((c) => +c.id === +checklist_id)) throw new MockError(404, 'Чек-лист не найден');
      const p = st.meta.providers.find((x) => x.id === provider);
      if (p && p.available === false) throw new MockError(422, `Модель «${p.label}» не настроена`);
      const run = { id: nextId(st.runs), status: 'running', checklist_id: +checklist_id, provider: provider || 'haiku', model: p?.model || 'claude-haiku-5-5',
        dataset_id: dataset_id || null, total: ids.length, done: 0, failed: 0, cost_usd: 0, created_at: now(), finished_at: null,
        items: ids.map((cid) => ({ conversation_id: cid, status: 'pending', evaluation_id: null, total: null, gross: null, error: null })) };
      st.runs.push(run);
      const tick = () => {
        const busy = run.items.find((i) => i.status === 'running');
        if (busy) {
          const conv = st.convs.find((c) => c.id === busy.conversation_id);
          const ev = serveEval(makeEval(conv, checklist_id, run.id, run.provider));
          Object.assign(busy, { status: 'ok', evaluation_id: ev.id, total: ev.original.total, gross: ev.original.gross });
          run.done++; run.cost_usd = +(run.cost_usd + ev.cost_usd).toFixed(6);
        }
        const next = run.items.find((i) => i.status === 'pending');
        if (next) { next.status = 'running'; setTimeout(tick, 450); }
        else if (!run.items.some((i) => i.status === 'running')) { run.status = 'done'; run.finished_at = now(); }
        else setTimeout(tick, 450);
      };
      setTimeout(tick, 300);
      return { run_id: run.id };
    },
    async run(id) {
      await wait(60);
      const r = st.runs.find((x) => +x.id === +id);
      if (!r) throw new MockError(404, 'Прогон не найден');
      return serveRun(r);
    },
    async runs() { await wait(); return st.runs.map(serveRun).map(({ items, ...r }) => r); },
    async checklists() {
      await wait();
      return st.checklists.map(({ config, ...c }) => ({ ...c, n_criteria: config.criteria.length }));
    },
    async checklist(id) {
      await wait();
      const c = st.checklists.find((x) => +x.id === +id);
      if (!c) throw new MockError(404, 'Чек-лист не найден');
      return clone(c);
    },
    async validateChecklist(config) { await wait(80); const errors = validate(config); return { ok: !errors.length, errors }; },
    async saveChecklist({ config, note, parent_id }) {
      await wait(250);
      const errors = validate(config);
      if (errors.length) throw new MockError(422, errors.join(' '), { errors });
      const sameName = st.checklists.filter((c) => c.name === config.name);
      const c = { id: nextId(st.checklists), name: config.name, version: sameName.length ? Math.max(...sameName.map((x) => x.version)) + 1 : 1,
        parent_id: parent_id ?? null, note: note || '', created_at: now(), config: clone(config) };
      st.checklists.push(c);
      return { id: c.id, version: c.version };
    },
    async appeals(status = 'all') {
      await wait();
      return st.appeals.filter((a) => status === 'all' || (status === 'open' ? a.status === 'open' : a.status !== 'open')).map((a) => {
        const ev = st.evals.find((e) => +e.id === +a.evaluation_id);
        const se = serveEval(ev);
        const c = se.criteria.find((x) => x.id === a.criterion_id);
        const conv = st.convs.find((x) => x.id === ev.conversation_id);
        return { ...clone(a), conversation_id: ev.conversation_id, criterion_title: c?.title, original_answer: c?.original.answer,
          explanation: c?.original.explanation, evidence: c?.original.evidence || [],
          evidence_replies: (c?.original.evidence || []).map((k) => conv.transcript.find((m) => m.n === k)).filter(Boolean).map((m) => ({ n: m.n, timestamp: m.timestamp, speaker: m.speaker, text: m.text })),
          total_original: se.original.total, total_final: se.final.total };
      });
    },
    async createAppeal({ evaluation_id, criterion_id, comment }) {
      await wait(200);
      if (!String(comment || '').trim()) throw new MockError(422, 'Добавьте комментарий');
      if (st.appeals.some((a) => +a.evaluation_id === +evaluation_id && a.criterion_id === criterion_id && a.status === 'open')) throw new MockError(409, 'По этому критерию уже есть открытая апелляция');
      const a = { id: nextId(st.appeals), evaluation_id: +evaluation_id, criterion_id, comment, created_at: now(), status: 'open', new_answer: null, reason: null, decided_at: null };
      st.appeals.push(a);
      return clone(a);
    },
    async decideAppeal(id, { decision, new_answer, reason }) {
      await wait(250);
      const a = st.appeals.find((x) => +x.id === +id);
      if (!a) throw new MockError(404, 'Апелляция не найдена');
      if (a.status !== 'open') throw new MockError(409, 'Решение уже принято');
      if (!String(reason || '').trim()) throw new MockError(422, 'Укажите причину решения');
      const ev = st.evals.find((e) => +e.id === +a.evaluation_id);
      const c = ev.criteria.find((x) => x.id === a.criterion_id);
      if (decision === 'change') {
        if (!c.allowed.includes(new_answer)) throw new MockError(422, `Недопустимый ответ «${new_answer}»`);
        if (new_answer === c.original.answer) throw new MockError(422, 'Новый ответ совпадает с исходным — выберите «Подтвердить»');
        Object.assign(a, { status: 'changed', new_answer });
      } else Object.assign(a, { status: 'confirmed', new_answer: null });
      Object.assign(a, { reason, decided_at: now() });
      return { appeal: clone(a), evaluation: serveEval(ev) };
    },
  };
  return api;
}
