/* Login page: tab switching, password visibility toggle, login and registration requests. */
(() => {
  'use strict';
  const { request } = window.LG;
  const $ = (id) => document.getElementById(id);

  const MIN_PASSWORD = 8;
  const tabs = { login: $('tab-login'), register:$('tab-register') };
  const panels = { login: $('panel-login'), register:$('panel-register') };
  const box = $('message');

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

  /* Password visibility toggle handler */
  document.addEventListener('click', (event) => {
    const toggleBtn = event.target.closest('.toggle-password, [data-toggle-password], .password-toggle');
    if (!toggleBtn) return;

    const field = toggleBtn.closest('.field, .password-field, .input-group') || toggleBtn.parentElement;
    const input = field.querySelector('input[type="password"], input[type="text"]');
    if (!input) return;

    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';

    // Toggle button active class for CSS icon switching or inline SVG/i updates
    toggleBtn.classList.toggle('is-visible', isHidden);
    toggleBtn.setAttribute('aria-label', isHidden ? 'Hide password' : 'Show password');

    // Update icon class if standard FontAwesome/Feather icons are used
    const icon = toggleBtn.querySelector('i, svg');
    if (icon) {
      if (isHidden) {
        // Password revealed -> show closed eye (click to hide)
        icon.classList.remove('fa-eye', 'icon-eye', 'eye-open');
        icon.classList.add('fa-eye-slash', 'icon-eye-off', 'eye-closed');
      } else {
        // Password masked -> show open eye (click to view)
        icon.classList.remove('fa-eye-slash', 'icon-eye-off', 'eye-closed');
        icon.classList.add('fa-eye', 'icon-eye', 'eye-open');
      }
    }
  });

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