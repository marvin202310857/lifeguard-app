/* Dashboard: live status, camera cards, alarm sound and the admin panels. */
(() => {
  'use strict';
  const { esc, api, toast } = window.LG;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const IS_ADMIN = document.body.dataset.admin === 'true';
  const ME = (document.body.dataset.user || '').toLowerCase();
  const STATUS_POLL_MS = 2000;
  const ADMIN_POLL_MS = 10000;

  // ---------------------------------------------------------------- helpers
  /** Only touch the DOM when the markup changed, so images and focus aren't reset every poll. */
  const rendered = new WeakMap();
  function render(el, html) {
    if (rendered.get(el) !== html) {
      el.innerHTML = html;
      rendered.set(el, html);
    }
  }

  /** Run fn now and every `ms`, skipping ticks while the tab is hidden or the last run is still going. */
  function poll(fn, ms) {
    let busy = false;
    const tick = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try { await fn(); } catch (err) { console.error(err); } finally { busy = false; }
    };
    tick();
    setInterval(tick, ms);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
    return tick;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const chip = (kind, text) => `<span class="chip chip-${kind}">${esc(text)}</span>`;
  const dataAttrs = (obj) => Object.entries(obj).map(([k, v]) => ` data-${k}="${esc(v)}"`).join('');
  const button = (kind, action, label, data = {}) =>
    `<button type="button" class="btn btn-sm btn-${kind}" data-action="${action}"${dataAttrs(data)}>${esc(label)}</button>`;
  const sentence = (text) => { const s = String(text || '').replace(/_/g, ' ').toLowerCase(); return s.charAt(0).toUpperCase() + s.slice(1); };

  function formatBytes(n) {
    if (!n) return '0 KB';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    const value = n / 1024 ** i;
    return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
  }

  function duration(seconds) {
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
    return h ? `${h}h ${m}m` : `${Math.max(m, 1)}m`;
  }

  function confirmDialog(text, okLabel) {
    const dialog = $('#confirm-dialog');
    $('#confirm-text').textContent = text;
    $('#confirm-ok').textContent = okLabel;
    dialog.returnValue = '';
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
      dialog.showModal();
    });
  }

  // ------------------------------------------------------------ alarm sound
  const alarm = (() => {
    const KEY = 'lifeguard.alarm';
    const toggle = $('#sound-toggle');
    let enabled = false, ctx = null, timer = null;

    const stored = () => { try { return localStorage.getItem(KEY) === 'on'; } catch (_) { return false; } };
    const store = (on) => { try { localStorage.setItem(KEY, on ? 'on' : 'off'); } catch (_) { /* private mode */ } };

    function ensureAudio() {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!ctx && Ctx) ctx = new Ctx();
      if (ctx && ctx.state === 'suspended') ctx.resume();
    }

    function beep() {
      if (!ctx || ctx.state !== 'running') return;
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = 'square';
      osc.frequency.value = 880;
      gain.gain.value = 0.06;
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.25);
    }

    function paint() {
      toggle.setAttribute('aria-pressed', String(enabled));
      toggle.textContent = enabled ? 'Alarm sound on' : 'Alarm sound off';
    }

    function set(on) {
      enabled = on;
      store(on);
      if (on) { ensureAudio(); beep(); } else { stop(); }
      paint();
    }

    function stop() { clearInterval(timer); timer = null; }

    /** Call with true while a drowning alert is showing. */
    function update(active) {
      if (enabled && active && !timer) { beep(); timer = setInterval(beep, 1500); }
      else if (!(enabled && active)) stop();
    }

    toggle.addEventListener('click', () => set(!enabled));
    enabled = stored();
    paint();
    // Browsers only allow audio after a user gesture; unlock on the first interaction.
    if (enabled) {
      const unlock = () => ensureAudio();
      document.addEventListener('pointerdown', unlock, { once: true });
      document.addEventListener('keydown', unlock, { once: true });
    }
    return { update };
  })();

  // ------------------------------------------------------- live status + cams
  const panel = $('#status-panel');
  const cameras = $$('[data-cam]').map((el) => ({
    id: el.dataset.cam,
    name: el.dataset.name,
    chip: $('[data-role="chip"]', el),
    inCount: $('[data-role="in"]', el),
    outCount: $('[data-role="out"]', el),
    updated: $('[data-role="updated"]', el),
  }));

  const CHIPS = {
    SAFE: ['chip-safe', 'Live'],
    WARNING: ['chip-warn', 'Warning'],
    DANGER: ['chip-danger', 'Alert'],
    OFFLINE: ['chip-offline', 'Offline'],
  };

  function showPanel(kind, headline, detail, counts) {
    panel.className = `status status-${kind}`;
    panel.setAttribute('aria-live', kind === 'danger' ? 'assertive' : 'polite');
    $('#status-headline').textContent = headline;
    $('#status-detail').textContent = detail;
    if (counts) {
      $('#count-pool').textContent = counts.pool;
      $('#count-side').textContent = counts.side;
      $('#count-cams').textContent = counts.cams;
    }
    document.title = kind === 'danger' ? 'ALERT - LifeGuard' : 'LifeGuard - Dashboard';
    alarm.update(kind === 'danger');
  }

  async function refreshStatus() {
    let stats;
    try {
      stats = await api('/api/stats');
    } catch (err) {
      if (err.status === 401) return;
      showPanel('offline', 'Connection lost', "Can't reach the LifeGuard server. Trying again...");
      return;
    }

    const rows = cameras.map((cam) => ({ cam, m: stats[cam.id] || {} }));
    const status = (row) => row.m.status || 'OFFLINE';

    for (const { cam, m } of rows) {
      const [cls, label] = CHIPS[m.status] || CHIPS.OFFLINE;
      cam.chip.className = `chip ${cls}`;
      cam.chip.textContent = label;
      cam.inCount.textContent = m.people_in_pool ?? 0;
      cam.outCount.textContent = m.people_outside_pool ?? 0;
      cam.updated.textContent = m.last_updated ? `Updated ${m.last_updated.slice(-8)}` : 'No data yet';
    }

    const online = rows.filter((r) => status(r) !== 'OFFLINE');
    const danger = rows.filter((r) => status(r) === 'DANGER' || (r.m.submerged_count || 0) > 0);
    const warning = rows.filter((r) => status(r) === 'WARNING');
    const names = (list) => list.map((r) => r.cam.name).join(', ');
    const counts = { pool: stats.total_in_pool ?? 0, side: stats.total_outside ?? 0, cams: `${online.length}/${cameras.length}` };

    if (danger.length) {
      const n = Math.max(1, stats.total_submerged || 0);
      showPanel('danger', 'Drowning alert', `${plural(n, 'swimmer')} in the water too long. Check ${names(danger)} now.`, counts);
    } else if (warning.length) {
      showPanel('warn', 'Check the pool', `A swimmer has been in the water a long time (${names(warning)}).`, counts);
    } else if (!online.length) {
      showPanel('offline', 'Cameras offline', 'Waiting for the camera streams to connect.', counts);
    } else {
      const down = cameras.length - online.length;
      showPanel('safe', 'All clear',
        down ? `${plural(down, 'camera')} offline. No one at risk on the others.` : 'No swimmers at risk.', counts);
    }
  }

  // ---------------------------------------------------------- live video feeds
  // A video <img> never reconnects on its own. Reload it when it drops (server restart,
  // brief network loss) and release it while this tab is hidden, because browsers allow
  // only ~6 connections per address across all tabs and each feed holds one open.
  const feeds = $$('.cam-view img').map((img) => ({ img, url: img.getAttribute('src'), timer: null }));

  function connectFeed(feed) {
    clearTimeout(feed.timer);
    if (document.hidden) return;
    feed.img.src = `${feed.url}?t=${Date.now()}`;      // fresh URL forces a new connection
  }

  for (const feed of feeds) {
    feed.img.addEventListener('error', () => {
      clearTimeout(feed.timer);
      feed.timer = setTimeout(() => connectFeed(feed), 3000);
    });
  }

  document.addEventListener('visibilitychange', () => {
    for (const feed of feeds) {
      if (document.hidden) {
        clearTimeout(feed.timer);
        feed.img.removeAttribute('src');
      } else {
        connectFeed(feed);
      }
    }
  });

  poll(refreshStatus, STATUS_POLL_MS);
  if (!IS_ADMIN) return;

  // ------------------------------------------------------------ admin: tabs
  const tabs = $$('.admin [role="tab"]');
  function selectTab(tab) {
    for (const t of tabs) {
      const active = t === tab;
      t.setAttribute('aria-selected', String(active));
      t.tabIndex = active ? 0 : -1;
      $(`#${t.getAttribute('aria-controls')}`).hidden = !active;
    }
  }
  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      selectTab(next);
      next.focus();
    });
  });

  // ------------------------------------------------------ admin: panels
  /** Placeholder row (tables) or paragraph (grids) shown when a list is empty or fails to load. */
  const emptyState = (text, cols) => cols
    ? `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`
    : `<p class="empty">${esc(text)}</p>`;
  const loadFailed = (cols, detail) => emptyState(`Couldn't load this list${detail ? ` (${detail})` : ''}. Trying again...`, cols);

  async function loadSecurityAlerts() {
    const host = $('#security-alerts');
    const list = await api('/api/security_alerts');
    host.hidden = !list.length;
    render(host, list.map((a) => `
      <div class="sec-alert" role="alert">
        <div><strong>${esc(a.message)}</strong><small>${esc(a.created_at)}</small></div>
        ${button('ghost', 'dismiss-alert', 'Dismiss', { id: Number(a.id) })}
      </div>`).join(''));
  }

  function userRow(u) {
    const status = [u.approved ? chip('safe', 'Approved') : chip('warn', 'Pending')];
    if (u.locked) status.push(chip('danger', `Locked ${duration(u.locked_seconds_left || 0)}`));
    else if (u.failed_attempts) status.push(chip('warn', `${u.failed_attempts} failed`));

    const actions = [];
    if (!u.approved) actions.push(button('safe', 'approve-user', 'Approve', { id: u.id }));
    if (u.locked || u.failed_attempts) actions.push(button('info', 'unlock-user', 'Unlock', { username: u.username }));
    if (u.username.toLowerCase() !== ME) {
      actions.push(button('danger', 'remove-user', u.approved ? 'Remove' : 'Reject', { id: u.id, name: u.username }));
    }
    return `<tr>
      <td><strong>${esc(u.username)}</strong></td><td>${esc(u.role)}</td><td>${esc(u.created_at)}</td>
      <td><div class="cell-chips">${status.join('')}</div></td>
      <td><div class="row-actions">${actions.join('')}</div></td></tr>`;
  }

  async function loadUsers() {
    const users = await api('/api/users');
    const pending = users.filter((u) => !u.approved).length;
    const badge = $('#count-pending');
    badge.hidden = !pending;
    badge.textContent = pending;
    render($('#user-rows'), users.map(userRow).join('') || emptyState('No accounts yet.', 5));
  }

  function incidentCard(i) {
    const thumb = i.has_snapshot
      ? `<img class="incident-thumb" loading="lazy" src="/api/incidents/${Number(i.id)}/snapshot" alt="Snapshot of incident ${Number(i.id)}">`
      : '<div class="incident-thumb-empty">No snapshot</div>';
    const actions = [];
    if (i.has_video) actions.push(button('info', 'play-video', 'Play video', { id: i.id }));
    else actions.push(i.video_state === 'RECORDING' ? chip('warn', 'Recording footage') : chip('offline', 'Video unavailable'));
    actions.push(button('danger', 'delete-incident', 'Delete', { id: i.id }));
    return `<article class="incident">${thumb}
      <div class="incident-body">
        <div class="incident-meta">${i.status === 'ACTIVE' ? chip('danger', 'Active') : chip('offline', 'Resolved')}</div>
        <strong>${esc(i.event)}</strong>
        <span>${esc(i.cam_id)}, swimmer #${esc(i.track_id)}, in the water ${esc(i.elapsed_sec)}s</span>
        <span class="muted">${esc(i.created_at)}</span>
      </div>
      <div class="incident-actions">${actions.join('')}</div></article>`;
  }

  async function loadIncidents() {
    const items = await api('/api/incidents');
    const badge = $('#count-incidents');
    badge.hidden = !items.length;
    badge.textContent = items.length;
    render($('#incident-grid'), items.map(incidentCard).join('') ||
      '<p class="empty">No incidents recorded. When an alert fires, its snapshot and video appear here.</p>');
  }

  async function loadLogs() {
    const logs = await api('/api/login_logs?limit=100');
    render($('#log-rows'), logs.map((l) => `<tr>
      <td>${esc(l.created_at)}</td><td>${esc(l.username || '-')}</td>
      <td>${chip(l.success ? 'safe' : 'danger', sentence(l.event))}</td>
      <td>${esc(l.ip_address || '')}</td><td>${esc(l.detail || '')}</td></tr>`).join('') ||
      emptyState('No activity yet.', 5));
  }

  // Footage: one folder per day, shown for the retention period and then deleted by the server.
  const FOLDER_ICON = '<svg class="folder-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6a2 2 0 0 1 2-2h4.2a2 2 0 0 1 1.5.7L12 6h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z"/></svg>';
  let openDay = null;

  function folderCard(f) {
    const pretty = new Date(`${f.date}T00:00:00`)
      .toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
    const chips = [];
    if (f.today) chips.push(chip('safe', 'Today'));
    chips.push(chip(f.days_left <= 2 ? 'warn' : 'offline', `Deletes in ${plural(f.days_left, 'day')}`));
    return `<button type="button" class="folder" data-action="open-day" data-day="${esc(f.date)}" data-label="${esc(f.label)}">
      ${FOLDER_ICON}
      <span class="folder-text">
        <strong>${esc(f.label)}</strong>
        <span>${esc(pretty)}</span>
        <span>${plural(f.incidents, 'incident')}, ${formatBytes(f.bytes)}</span>
        <span class="folder-chips">${chips.join('')}</span>
      </span></button>`;
  }

  function showDay(day, label) {
    openDay = day;
    $('#footage-folders').hidden = Boolean(day);
    $('#footage-clips').hidden = !day;
    $('#crumb-root').disabled = !day;
    $('#crumb-day').hidden = !day;
    $('#crumb-day').textContent = label || day || '';
  }

  async function loadDay() {
    const clips = await api(`/api/footage/${openDay}`);
    render($('#footage-clips'), clips.map(incidentCard).join('') ||
      '<p class="empty">No incidents were recorded on this day.</p>');
  }

  async function loadFootage() {
    const data = await api('/api/footage');
    $('#footage-summary').textContent =
      `Kept for ${plural(data.retention_days, 'day')}, then deleted automatically. ` +
      `${formatBytes(data.total_bytes)} used, ${formatBytes(data.disk_free_bytes)} free on this computer.`;
    render($('#footage-folders'), data.folders.map(folderCard).join('') || '<p class="empty">No footage yet.</p>');
    if (openDay) await loadDay();
  }

  /** Load a panel; on failure show an inline message instead of leaving "Loading..." forever. */
  const guarded = (loader, target, cols) => () => loader().catch((err) => {
    if (err.status !== 401) render(target, loadFailed(cols, err.message));
    throw err;
  });

  const refreshAlerts = guarded(loadSecurityAlerts, $('#security-alerts'));
  const refreshUsers = guarded(loadUsers, $('#user-rows'), 5);
  const refreshIncidents = guarded(loadIncidents, $('#incident-grid'));
  const refreshLogs = guarded(loadLogs, $('#log-rows'), 5);
  const refreshFootage = guarded(loadFootage, $('#footage-folders'));

  poll(() => Promise.allSettled([refreshAlerts(), refreshUsers(), refreshIncidents(), refreshFootage(), refreshLogs()]), ADMIN_POLL_MS);

  // -------------------------------------------------------- admin: actions
  // A snapshot that can't be loaded (file removed, etc.) gets a neutral placeholder, not a broken image.
  document.addEventListener('error', (event) => {
    const img = event.target;
    if (img instanceof HTMLImageElement && img.classList.contains('incident-thumb')) {
      const box = document.createElement('div');
      box.className = 'incident-thumb-empty';
      box.textContent = 'Snapshot unavailable';
      img.replaceWith(box);
    }
  }, true);

  const videoDialog = $('#video-dialog');
  const player = $('#video-player');

  videoDialog.addEventListener('close', () => {
    player.pause();
    player.removeAttribute('src');
    player.load();
  });
  videoDialog.addEventListener('click', (e) => { if (e.target === videoDialog) videoDialog.close(); });

  const actions = {
    async 'approve-user'({ id }) {
      await api('/api/users/approve', { body: { userId: id } });
      toast('Account approved.', 'success');
      await refreshUsers();
    },
    async 'remove-user'({ id, name }) {
      if (!(await confirmDialog(`Remove the account "${name}"? They will lose access immediately.`, 'Remove account'))) return;
      await api('/api/users/reject', { body: { userId: id } });
      toast('Account removed.', 'success');
      await refreshUsers();
    },
    async 'unlock-user'({ username }) {
      await api('/api/users/unlock', { body: { username } });
      toast(`${username} can log in again.`, 'success');
      await Promise.allSettled([refreshUsers(), refreshLogs()]);
    },
    async 'dismiss-alert'({ id }) {
      await api('/api/security_alerts/ack', { body: { id: Number(id) } });
      await refreshAlerts();
    },
    'play-video'({ id }) {
      player.src = `/api/incidents/${Number(id)}/video`;
      $('#video-title').textContent = `Incident #${Number(id)}`;
      $('#video-download').href = `/api/incidents/${Number(id)}/video?download=1`;
      videoDialog.showModal();
      player.play().catch(() => { /* user can press play */ });
    },
    'close-video'() { videoDialog.close(); },
    async 'open-day'({ day, label }) { showDay(day, label); await loadDay(); },
    'close-day'() { showDay(null); },
    async 'delete-incident'({ id }) {
      if (!(await confirmDialog(`Delete incident #${id} and its video permanently?`, 'Delete incident'))) return;
      await api(`/api/incidents/${Number(id)}`, { method: 'DELETE' });
      toast('Incident deleted.', 'success');
      await Promise.allSettled([refreshIncidents(), refreshFootage()]);
    },
  };

  document.addEventListener('click', async (event) => {
    const el = event.target.closest('[data-action]');
    const handler = el && actions[el.dataset.action];
    if (!handler) return;
    el.disabled = true;
    try {
      await handler(el.dataset);
    } catch (err) {
      if (err.status !== 401) toast(err.message || 'Something went wrong.');
    } finally {
      el.disabled = false;
    }
  });
})();