/* Login page: tab switching, password visibility toggle, login and registration requests. */
(() => {
  'use strict';
  const { request } = window.LG;
  const $ = (id) => document.getElementById(id);

  const MIN_PASSWORD = 8;
  const tabs = { login: $('tab-login'), register: $('tab-register') };
  const panels = { login: $('panel-login'), register: $('panel-register') };
  const box = $('message');

  /* ---------- Password show / hide ----------
     One eye button per password field, built here so it never depends on the template's own
     markup. Any icon or button the template already placed next to the field is hidden, so only
     one eye can ever show (and the browser's built-in reveal icon is hidden in login.css). */
  const EYE_ICONS =
    '<svg class="eye-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>' +
    '<svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>' +
    '<line x1="1" y1="1" x2="23" y2="23"/></svg>';

  const passwordToggles = [];

  function setRevealed(entry, revealed) {
    entry.input.type = revealed ? 'text' : 'password';
    entry.button.classList.toggle('is-visible', revealed);
    entry.button.setAttribute('aria-label', revealed ? 'Hide password' : 'Show password');
    entry.button.setAttribute('aria-pressed', String(revealed));
  }

  function hideAllPasswords() {
    passwordToggles.forEach((entry) => setRevealed(entry, false));
  }

  function setupPasswordToggles() {
    document.querySelectorAll('input[type="password"]').forEach((input) => {
      if (input.dataset.pwReady) return;
      input.dataset.pwReady = '1';

      // Hide whatever eye icon/button the template already put in this field (no duplicates).
      const field = input.closest('.field, .password-field, .input-group') || input.parentElement;
      field.querySelectorAll('button, svg, i, img, [class*="eye"], [class*="toggle"], [class*="reveal"]')
        .forEach((el) => {
          if (el.contains(input) || el.querySelector('input')) return;   // never hide the input itself
          el.classList.add('pw-hidden-original');
        });

      const wrap = document.createElement('div');
      wrap.className = 'pw-wrap';
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      input.classList.add('pw-input');

      const button = document.createElement('button');
      button.type = 'button';                       // must never submit the form
      button.className = 'pw-toggle';
      button.innerHTML = EYE_ICONS;
      wrap.appendChild(button);

      const entry = { input, button };
      passwordToggles.push(entry);
      setRevealed(entry, false);

      button.addEventListener('mousedown', (event) => event.preventDefault());   // keep focus in the field
      button.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        setRevealed(entry, input.type === 'password');
        input.focus();
        const end = input.value.length;
        try { input.setSelectionRange(end, end); } catch (_) { /* not supported while hidden */ }
      });
    });
  }

  /* ---------- Messages and tabs ---------- */
  function showMessage(text, isError) {
    box.className = `message ${isError ? 'message-error' : 'message-success'}`;
    box.setAttribute('role', isError ? 'alert' : 'status');
    box.textContent = text;
    box.hidden = false;
  }

  function selectTab(name) {
    for (const key of Object.keys(tabs)) {
      const active = key === name;
      tabs[key].setAttribute('aria-selected', String(active));
      tabs[key].tabIndex = active ? 0 : -1;
      panels[key].hidden = !active;
    }
    hideAllPasswords();
    box.hidden = true;
  }

  for (const [name, tab] of Object.entries(tabs)) {
    tab.addEventListener('click', () => selectTab(name));
    tab.addEventListener('keydown', (e) => {          // arrow keys move between tabs
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const next = name === 'login' ? 'register' : 'login';
      selectTab(next);
      tabs[next].focus();
    });
  }

  setupPasswordToggles();

  /** Run a submit handler with the button disabled and a friendly network-error fallback. */
  function onSubmit(form, handler) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const label = button.textContent;
      button.disabled = true;
      button.textContent = 'Please wait...';
      try {
        await handler();
      } catch (_) {
        showMessage("Can't reach the LifeGuard server. Check that it is running and try again.", true);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  }

  onSubmit(panels.login, async () => {
    const username = $('login-username').value.trim();
    const password = $('login-password').value;
    if (!username || !password) return showMessage('Enter your username and password.', true);

    const { data } = await request('/api/login', { body: { username, password } });
    if (data && data.success) {
      location.assign(data.redirect || '/dashboard');
    } else {
      showMessage((data && (data.message || data.error)) || 'Login failed. Check your details and try again.', true);
      $('login-password').select();
    }
  });

  onSubmit(panels.register, async () => {
    const username = $('register-username').value.trim();
    const password = $('register-password').value;
    const role = $('register-role').value;
    if (!username || !password) return showMessage('Fill in a username and password.', true);
    if (password.length < MIN_PASSWORD) return showMessage(`Password must be at least ${MIN_PASSWORD} characters.`, true);

    const { ok, data } = await request('/api/register', { body: { username, password, role } });
    if (ok && data && data.success) {
      selectTab('login');
      $('login-username').value = username;
      $('register-password').value = '';
      showMessage(data.message || 'Account requested. An administrator needs to approve it before you can log in.', false);
      $('login-password').focus();
    } else {
      showMessage((data && (data.error || data.message)) || 'Registration failed.', true);
    }
  });
})();
