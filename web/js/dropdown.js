// Свой выпадающий список вместо системного. Настоящий <select class="cqa-select"> остаётся внутри скрытым:
// он хранит значение, и обработчики экранов слушают его change как раньше. Рядом — кнопка и меню в стиле
// приложения; у пункта может быть подсказка (атрибут data-desc у <option>). Подключается сам ко всем
// <select class="cqa-select">, в том числе появившимся после перерисовки экранов.
import { esc, icon } from './util.js';

const openMenus = new Set();

const text = (o) => (o ? o.textContent.trim() : '');

export function enhance(sel) {
  if (sel.dataset.dd || sel.multiple || !sel.parentNode) return;
  sel.dataset.dd = '1';
  const wrap = document.createElement('span');
  wrap.className = `cqa-dd${sel.classList.contains('cqa-select--sm') ? ' cqa-dd--sm' : ''}`;
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.classList.add('cqa-dd-native');
  sel.tabIndex = -1;
  sel.setAttribute('aria-hidden', 'true');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cqa-dd-btn';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  for (const a of ['aria-label', 'title']) if (sel.getAttribute(a)) btn.setAttribute(a, sel.getAttribute(a));
  const menu = document.createElement('div');
  menu.className = 'cqa-dd-menu';
  menu.setAttribute('role', 'listbox');
  wrap.append(btn, menu);

  let active = -1;
  const sync = () => {
    btn.innerHTML = `<span class="cqa-dd-value">${esc(text(sel.selectedOptions[0]))}</span>${icon('down')}`;
    btn.disabled = sel.disabled;
  };
  const render = () => {
    menu.innerHTML = [...sel.options].map((o, i) => `
      <div class="cqa-dd-opt" role="option" data-i="${i}" aria-selected="${o.selected}"${o.disabled ? ' aria-disabled="true"' : ''}>
        <span class="cqa-dd-check">${o.selected ? icon('check') : ''}</span>
        <span><span class="cqa-dd-label">${esc(text(o))}</span>${o.dataset.desc ? `<span class="cqa-dd-desc">${esc(o.dataset.desc)}</span>` : ''}</span>
      </div>`).join('');
  };
  const mark = (i) => {
    const items = menu.querySelectorAll('.cqa-dd-opt');
    items.forEach((el, k) => el.classList.toggle('is-active', k === i));
    active = i;
    items[i]?.scrollIntoView({ block: 'nearest' });
  };
  const close = () => {
    wrap.classList.remove('is-open');
    btn.setAttribute('aria-expanded', 'false');
    openMenus.delete(close);
  };
  const open = () => {
    openMenus.forEach((c) => c());
    render();
    // меню у правого края экрана раскрываем влево, чтобы не уезжало за край
    menu.classList.toggle('is-right', btn.getBoundingClientRect().left + 320 > window.innerWidth);
    wrap.classList.add('is-open');
    btn.setAttribute('aria-expanded', 'true');
    openMenus.add(close);
    mark(sel.selectedIndex);
  };
  const choose = (i) => {
    const o = sel.options[i];
    if (!o || o.disabled) return;
    if (sel.selectedIndex !== i) {
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sync();
    close();
    btn.focus();
  };
  const step = (d) => {
    const n = sel.options.length;
    if (!n) return;
    let i = active;
    for (let k = 0; k < n; k++) {
      i = (i + d + n) % n;
      if (!sel.options[i].disabled) break;
    }
    mark(i);
  };

  btn.addEventListener('click', () => (wrap.classList.contains('is-open') ? close() : open()));
  menu.addEventListener('mousedown', (e) => e.preventDefault());
  menu.addEventListener('click', (e) => {
    const el = e.target.closest('.cqa-dd-opt');
    if (el) choose(+el.dataset.i);
  });
  btn.addEventListener('keydown', (e) => {
    const isOpen = wrap.classList.contains('is-open');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!isOpen) open(); else step(e.key === 'ArrowDown' ? 1 : -1);
    } else if ((e.key === 'Enter' || e.key === ' ') && isOpen) {
      e.preventDefault();
      choose(active);
    } else if (e.key === 'Escape' && isOpen) {
      e.preventDefault();
      close();
    }
  });
  sel.addEventListener('change', sync);
  new MutationObserver(sync).observe(sel, { attributes: true, attributeFilter: ['disabled'], childList: true, subtree: true });
  sync();
}

function enhanceIn(node) {
  if (node.nodeType !== 1) return;
  if (node.matches('select.cqa-select')) enhance(node);
  else node.querySelectorAll('select.cqa-select').forEach(enhance);
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.cqa-dd')) openMenus.forEach((c) => c());
});
new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach(enhanceIn)))
  .observe(document.documentElement, { childList: true, subtree: true });
document.querySelectorAll('select.cqa-select').forEach(enhance);
