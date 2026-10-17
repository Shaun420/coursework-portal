/* Coursework Portal client — vanilla JS, no build step. */

const CSRF_COOKIE = 'cp_csrf';

function cookie(name) {
  const hit = document.cookie.split('; ').find((r) => r.startsWith(name + '='));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : '';
}

function banner(message, kind = 'ok', timeout = 6000) {
  const el = document.getElementById('banner');
  if (!el) return;
  el.textContent = message;
  el.className = 'banner ' + kind;
  if (timeout) setTimeout(() => el.classList.add('hidden'), timeout);
}

async function api(path, opts = {}) {
  const options = { headers: {}, ...opts };
  const method = (options.method || 'GET').toUpperCase();
  if (!options.headers['Content-Type'] && typeof options.body === 'string') {
    options.headers['Content-Type'] = 'application/json';
  }
  if (method !== 'GET') options.headers['X-CSRF-Token'] = cookie(CSRF_COOKIE);

  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    banner('Network error: ' + err.message, 'err');
    return null;
  }

  if (res.status === 401) {
    if (location.pathname !== '/login') location.href = '/login';
    return null;
  }
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { message: text }; }

  if (!res.ok) {
    const message = (payload && (payload.message || payload.error)) || `Request failed (${res.status})`;
    banner(message, 'err', 9000);
    return null;
  }
  return payload;
}

function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return (value * 100).toFixed(digits) + '%';
}

function scoreCell(value, threshold) {
  if (value === null || value === undefined) return '<span class="muted">—</span>';
  const pctv = Math.max(0, Math.min(100, value * 100));
  const cls = value >= threshold ? 'flag' : (value >= threshold * 0.7 ? 'warn' : '');
  return `<span title="${pct(value)}">
    <span class="bar"><i class="${cls}" style="width:${pctv}%"></i><u style="left:${threshold * 100}%"></u></span>
    <span class="num">${pct(value)}</span></span>`;
}

function verdictPill(verdict, flagged, analysisStatus) {
  if (analysisStatus === 'pending' || analysisStatus === 'running') {
    return `<span class="pill pill-mute">${analysisStatus}</span>`;
  }
  if (analysisStatus === 'failed' || analysisStatus === 'skipped') {
    return `<span class="pill pill-warn">${analysisStatus}</span>`;
  }
  if (!verdict) return '<span class="pill pill-mute">—</span>';
  return flagged
    ? `<span class="pill pill-flag">${verdict.replace(/_/g, ' ')}</span>`
    : `<span class="pill pill-clean">${verdict.replace(/_/g, ' ')}</span>`;
}

async function loadWhoami() {
  const el = document.getElementById('whoami');
  if (!el) return null;
  const data = await api('/auth/me');
  if (!data) { el.textContent = 'signed out'; return null; }
  const role = data.user.role === 'teacher' || data.user.role === 'admin' ? 'teacher' : 'student';
  el.textContent = `${data.user.name} · ${role}${data.user.drive_linked ? '' : ' · Drive not linked'}`;
  window.PORTAL = data;
  return data;
}

function bindLogout() {
  const btn = document.getElementById('logout');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    await api('/auth/logout', { method: 'POST' });
    location.href = '/login';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  bindLogout();
  if (location.pathname !== '/login') loadWhoami();
});

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}

function debounce(fn, wait) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}
