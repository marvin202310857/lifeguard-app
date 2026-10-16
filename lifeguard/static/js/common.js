/* Shared helpers: HTML escaping, JSON requests, toasts, password visibility. */
(() => {
  'use strict';

  const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  /** Raw JSON request -> { ok, status, data }. Throws only on network failure. */
  async function request(url, { method, body } = {}) {
    const init = { method: method || (body !== undefined ? 'POST' : 'GET'), credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty or non-JSON body */ }
    return { ok: res.ok, status: res.status, data };
  }

  /** Like request(), but returns the data, throws ApiError, and sends signed-out users to the login page. */
  async function api(url, options) {
    const res = await request(url, options);
    if (res.status === 401) {
      location.assign('/');
      throw new ApiError('You have been signed out.', 401);
    }
    if (!res.ok) {
      throw new ApiError((res.data && (res.data.message || res.data.error)) || `Request failed (${res.status}).`, res.status);
    }
    return res.data;
  }

  let toastHost = null;
  function toast(message, kind = 'error', ms = 5000) {
    if (!toastHost) {
      toastHost = document.createElement('div');
      toastHost.className = 'toasts';
      toastHost.setAttribute('role', 'status');
      toastHost.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastHost);
    }
    const el = document.createElement('div');
    el.className = `message toast message-${kind === 'success' ? 'success' : 'error'}`;
    el.textContent = message;
    toastHost.appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  document.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-toggle-password]');
    if (!toggle) return;
    const input = document.getElementById(toggle.dataset.togglePassword);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(show));
    toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  });

  window.LG = { esc, request, api, ApiError, toast };
})();
