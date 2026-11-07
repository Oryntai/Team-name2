// Клиент HTTP API CQA (контракт — docs/ARCHITECTURE.md). Все запросы — к своему серверу (CORS обходим через него).
// Режим демо без сервера: ?mock=1 в адресе → данные из web/mock/ (выдуманные диалоги).

export class ApiError extends Error {
  constructor(status, body, message) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export const isMock = () => new URLSearchParams(location.search).has('mock');

function errorMessage(status, data) {
  if (data && typeof data === 'object') {
    if (Array.isArray(data.errors)) return data.errors.join(' ');
    const d = data.detail;
    if (d && typeof d === 'object' && Array.isArray(d.errors)) return d.errors.join(' ');
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => x?.msg || JSON.stringify(x)).join('; ');
    if (typeof data.error === 'string' && data.error) return data.error;
    if (typeof data.message === 'string') return data.message;
  }
  if (typeof data === 'string' && data && data.length < 300) return data;
  return status === 404 ? 'Не найдено' : status >= 500 ? `Ошибка сервера (${status})` : `Ошибка запроса (${status})`;
}

function httpClient(base) {
  const root = base.replace(/\/$/, '');
  async function req(method, path, body, isForm = false) {
    const opts = { method, headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    }
    let r;
    try { r = await fetch(root + path, opts); } catch {
      throw new ApiError(0, null, 'Сервер CQA недоступен. Запустите его: uv run python -m cqa');
    }
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) throw new ApiError(r.status, data, errorMessage(r.status, data));
    return data;
  }
  const q = (params) => {
    const s = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v != null && v !== '' && v !== 'all') s.set(k, v); });
    const str = s.toString();
    return str ? `?${str}` : '';
  };
  const enc = encodeURIComponent;
  return {
    mock: false,
    meta: () => req('GET', '/meta'),
    settings: () => req('GET', '/settings'),
    saveSettings: (body) => req('PUT', '/settings', body),
    datasets: () => req('GET', '/datasets'),
    importDataset: (body) => req('POST', '/datasets/import', body),
    uploadDataset: (file) => { const fd = new FormData(); fd.append('file', file); return req('POST', '/datasets/upload', fd, true); },
    conversations: (params) => req('GET', `/conversations${q(params)}`),
    conversation: (id) => req('GET', `/conversations/${enc(id)}`),
    evaluation: (id) => req('GET', `/evaluations/${enc(id)}`),
    startRun: (body) => req('POST', '/runs', body),
    planRun: (body) => req('POST', '/runs/plan', body),
    runDiff: (id) => req('GET', `/runs/${enc(id)}/diff`),
    run: (id) => req('GET', `/runs/${enc(id)}`),
    runs: () => req('GET', '/runs'),
    checklists: () => req('GET', '/checklists'),
    checklist: (id) => req('GET', `/checklists/${enc(id)}`),
    saveChecklist: (body) => req('POST', '/checklists', body),
    validateChecklist: (config) => req('POST', '/checklists/validate', { config }),
    assistChecklist: (body) => req('POST', '/checklists/assist', body),
    appeals: (status = 'all') => req('GET', `/appeals${q({ status })}`),
    createAppeal: (body) => req('POST', '/appeals', body),
    decideAppeal: (id, body) => req('POST', `/appeals/${enc(id)}/decision`, body),
    audioUrl: (id) => `${root}/audio/${enc(id)}`,
  };
}

function mockClient() {
  const ready = import('../mock/mock-api.js').then((m) => m.createMockApi());
  return new Proxy({ mock: true }, {
    get(target, name) {
      if (name in target) return target[name];
      if (name === 'then') return undefined;
      return (...args) => ready.then((api) => api[name](...args));
    },
  });
}

const clients = new Map();
export function getApi(base = '/api') {
  const key = (isMock() ? 'mock:' : '') + base;
  if (!clients.has(key)) clients.set(key, isMock() ? mockClient() : httpClient(base));
  return clients.get(key);
}
