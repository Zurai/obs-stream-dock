'use strict';

const $ = id => document.getElementById(id);
const PLATFORMS = { twitch: 'Twitch', youtube: 'YouTube' };
const YT_TITLE_MAX = 100;

async function api(method, path, body) {
  const opts = { method, cache: 'no-store', headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res, data;
  try {
    res = await fetch(path, opts);
    data = await res.json();
  } catch (e) {
    throw new Error('Plugin not reachable (is the script loaded in OBS?)');
  }
  if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function show(el, visible) { (typeof el === 'string' ? $(el) : el).hidden = !visible; }

// ---- accounts ------------------------------------------------------------

const accountEls = {};
for (const [key, label] of Object.entries(PLATFORMS)) {
  const el = $('account-tpl').content.firstElementChild.cloneNode(true);
  el.querySelector('.name').textContent = label;
  el.querySelector('.login').textContent = `Log in with ${label}`;
  el.querySelector('.login').addEventListener('click', async ev => {
    ev.target.disabled = true;
    try { await api('POST', `/api/${key}/login`, {}); }
    catch (e) { setText(el, '.error', e.message); show(el.querySelector('.error'), true); }
    refresh();
  });
  el.querySelector('.logout').addEventListener('click', async ev => {
    ev.preventDefault();
    if (!confirm(`Log out of ${label}?`)) return;
    await api('POST', `/api/${key}/logout`, {}).catch(() => {});
    refresh();
  });
  $('accounts').append(el);
  accountEls[key] = el;
}

function setText(root, sel, text) { root.querySelector(sel).textContent = text; }

function renderAccount(key, st) {
  const el = accountEls[key];
  const pending = st.login.state === 'pending';
  show(el.querySelector('.not-configured'), !st.configured);
  const needsRelogin = st.logged_in && st.missing_scopes && st.missing_scopes.length > 0;
  show(el.querySelector('.login'), st.configured && (!st.logged_in || needsRelogin) && !pending);
  el.querySelector('.login').disabled = false;
  el.querySelector('.login').textContent = needsRelogin
    ? `Log in again (new permission: chat)` : `Log in with ${PLATFORMS[key]}`;
  show(el.querySelector('.logout'), st.logged_in);
  setText(el, '.who', st.user ? st.user.display_name : (st.configured ? 'not logged in' : ''));
  show(el.querySelector('.pending'), pending);
  if (pending) {
    setText(el, '.code', st.login.user_code);
    const a = el.querySelector('.link');
    a.href = st.login.verification_uri;
    a.textContent = st.login.verification_uri.replace(/^https?:\/\//, '').replace(/\?.*$/, '');
  }
  show(el.querySelector('.error'), st.login.state === 'error');
  if (st.login.state === 'error') setText(el, '.error', st.login.error);
  el.classList.toggle('compact', st.logged_in && !needsRelogin);
  return pending;
}

// ---- status polling ------------------------------------------------------------

let state = null;
let fastPoll = null;
let twitchLoaded = false;

function render(status) {
  state = status;
  $('server').textContent = status.streaming ? 'plugin online · streaming' : 'plugin online';
  $('server').className = 'ok';

  const pending = ['twitch', 'youtube'].map(k => renderAccount(k, status[k])).some(Boolean);
  const tw = status.twitch.logged_in, yt = status.youtube.logged_in;

  show('form', tw || yt);
  show('category-block', tw);
  show('description-block', yt);
  show('diag-link', yt);
  updateCount();
  renderYoutubePending(status.youtube);

  const wantFast = pending || !!status.youtube.pending;
  if (wantFast && !fastPoll) fastPoll = setInterval(refresh, 2000);
  if (!wantFast && fastPoll) { clearInterval(fastPoll); fastPoll = null; }

  if (tw && !twitchLoaded) { twitchLoaded = true; loadTwitchChannel(); }
  if (!tw) twitchLoaded = false;
}

async function refresh() {
  try {
    render(await api('GET', '/api/status'));
  } catch (e) {
    $('server').textContent = 'plugin offline';
    $('server').className = 'err';
  }
}

// ---- results ---------------------------------------------------------------------

const results = {};

function result(platform, kind, msg, action) {
  // kind: ok | err | wait
  let li = results[platform];
  if (!li) {
    li = document.createElement('li');
    results[platform] = li;
    $('results').append(li);
  }
  li.className = kind;
  const icon = { ok: '✓', err: '✗', wait: '⏳' }[kind];
  li.replaceChildren(document.createTextNode(`${icon} ${PLATFORMS[platform]}: ${msg} `));
  if (action) {
    const a = document.createElement('a');
    a.href = '#';
    a.textContent = action.label;
    a.addEventListener('click', ev => { ev.preventDefault(); action.run(); });
    li.append(a);
  }
}

function clearResults() {
  for (const k of Object.keys(results)) { results[k].remove(); delete results[k]; }
}

let lastAppliedShown = null;

function renderYoutubePending(yt) {
  if (yt.pending) {
    result('youtube', 'wait', 'no broadcast yet, the title will be set as soon as you are live.', {
      label: 'cancel',
      run: async () => { await api('POST', '/api/youtube/pending/clear', {}).catch(() => {}); clearResults(); refresh(); },
    });
  } else if (yt.last_result && yt.last_result.at !== lastAppliedShown) {
    lastAppliedShown = yt.last_result.at;
    if (yt.last_result.ok) result('youtube', 'ok', 'title set now that the stream is live');
    else result('youtube', 'err', yt.last_result.error);
  }
}

// ---- form ------------------------------------------------------------------------

let selected = null; // {id, name, box_art_url}

function setCategory(cat) {
  selected = cat;
  $('category').value = cat ? cat.name : '';
  $('category-art').src = cat && cat.box_art_url ? cat.box_art_url : '';
}

function updateCount() {
  const n = $('title').value.length;
  $('title-count').textContent = `${n}/140`;
  const ytTooLong = state && state.youtube.logged_in && n > YT_TITLE_MAX;
  show('title-warn', ytTooLong);
  if (ytTooLong) $('title-warn').textContent = `YouTube allows only ${YT_TITLE_MAX} characters.`;
}
$('title').addEventListener('input', updateCount);

function artUrl(gameId) {
  return `https://static-cdn.jtvnw.net/ttv-boxart/${gameId}-52x72.jpg`;
}

async function loadTwitchChannel() {
  try {
    const ch = await api('GET', '/api/twitch/channel');
    if (!$('title').value) $('title').value = ch.title;
    updateCount();
    setCategory(ch.game_id ? { id: ch.game_id, name: ch.game_name, box_art_url: artUrl(ch.game_id) } : null);
  } catch (e) {
    result('twitch', 'err', e.message);
  }
}

async function applyTwitch(title) {
  const catText = $('category').value.trim();
  if (catText && (!selected || selected.name !== catText)) {
    return result('twitch', 'err', 'pick a category from the list');
  }
  try {
    const ch = await api('POST', '/api/twitch/channel', { title, game_id: selected ? selected.id : null });
    result('twitch', 'ok', `updated (${ch.game_name || 'no category'})`);
  } catch (e) {
    result('twitch', 'err', e.message);
  }
}

async function applyYoutube(title) {
  const desc = $('description').value;
  try {
    const r = await api('POST', '/api/youtube/broadcast', { title, description: desc.trim() ? desc : null });
    if (r.applied) result('youtube', 'ok', r.status === 'live' ? 'updated (live)' : `updated (${r.status || 'upcoming'})`);
  } catch (e) {
    result('youtube', 'err', e.message);
  }
}

$('form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const title = $('title').value.trim();
  if (!title) return;
  clearResults();
  $('apply').disabled = true;
  const jobs = [];
  if (state.twitch.logged_in) jobs.push(applyTwitch(title));
  if (state.youtube.logged_in) jobs.push(applyYoutube(title));
  await Promise.all(jobs);
  $('apply').disabled = false;
  refresh();
});

// ---- category autocomplete ---------------------------------------------------

let searchTimer = null;
let searchSeq = 0;
let items = [];
let active = -1;

function closeSuggestions() { show('suggestions', false); items = []; active = -1; }

function renderSuggestions() {
  $('suggestions').replaceChildren(...items.map((c, i) => {
    const li = document.createElement('li');
    if (i === active) li.className = 'active';
    const img = document.createElement('img');
    img.src = c.box_art_url;
    img.alt = '';
    li.append(img, document.createTextNode(c.name));
    li.addEventListener('mousedown', ev => { ev.preventDefault(); pick(i); });
    return li;
  }));
  show('suggestions', items.length > 0);
}

function pick(i) {
  if (items[i]) setCategory(items[i]);
  closeSuggestions();
}

$('category').addEventListener('input', () => {
  const q = $('category').value.trim();
  if (selected && selected.name !== $('category').value) { selected = null; $('category-art').src = ''; }
  clearTimeout(searchTimer);
  if (!q) return closeSuggestions();
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const res = await api('GET', '/api/twitch/categories?q=' + encodeURIComponent(q));
      if (seq !== searchSeq) return;
      items = res.data;
      active = items.length ? 0 : -1;
      renderSuggestions();
    } catch (e) {
      closeSuggestions();
      result('twitch', 'err', e.message);
    }
  }, 300);
});

$('category').addEventListener('keydown', ev => {
  if ($('suggestions').hidden) return;
  if (ev.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); renderSuggestions(); ev.preventDefault(); }
  else if (ev.key === 'ArrowUp') { active = Math.max(active - 1, 0); renderSuggestions(); ev.preventDefault(); }
  else if (ev.key === 'Enter') { pick(active); ev.preventDefault(); }
  else if (ev.key === 'Escape') { closeSuggestions(); }
});

$('category').addEventListener('blur', () => setTimeout(closeSuggestions, 100));

// ---- YouTube diagnostics -----------------------------------------------------------

$('diag-link').addEventListener('click', async ev => {
  ev.preventDefault();
  if (!$('diag').hidden) return show('diag', false);
  $('diag').textContent = 'Loading …';
  show('diag', true);
  try {
    const d = await api('GET', '/api/youtube/diagnostics');
    delete d.ok;
    $('diag').textContent = JSON.stringify(d, null, 2);
  } catch (e) {
    $('diag').textContent = e.message;
  }
});

// ---- start -----------------------------------------------------------------------

refresh();
setInterval(refresh, 10000);
