// Базовый класс веб-компонентов CQA: клиент API по атрибуту api-base и уборка подписок при удалении.
import { getApi } from './api.js';

export class CqaElement extends HTMLElement {
  get api() { return getApi(this.getAttribute('api-base') || '/api'); }

  connectedCallback() {
    if (this._connected) return;
    this._connected = true;
    this._cleanups = [];
    this.init();
  }

  disconnectedCallback() {
    this._connected = false;
    for (const fn of this._cleanups || []) { try { fn(); } catch { /* уже убрано */ } }
    this._cleanups = [];
  }

  init() {}

  get alive() { return this._connected; }

  onCleanup(fn) { (this._cleanups ||= []).push(fn); }

  listen(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    this.onCleanup(() => target.removeEventListener(type, fn, opts));
  }

  part(name) { return this.querySelector(`[data-part="${name}"]`); }
}
