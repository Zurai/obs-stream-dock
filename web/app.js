'use strict';

const $ = id => document.getElementById(id);

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

// ---- state / view --------------------------------------------------------

let loginPoll = null;
let channelLoaded = false;

function show(id, visible) { $(id).hidden = !visible; }

function render(status) {
  const tw = status.twitch;
  $('server').textContent = 'plugin online';
  $('server').className = 'ok';

  show('not-configured', !tw.configured);
  show('login', tw.configured && !tw.logged_in);
  show('form', tw.configured && tw.logged_in);
  show('logout', tw.logged_in);
  $('account').textContent = tw.user ? `Twitch: ${tw.user.display_name}` : '';

  const pending = tw.login.state === 'pending';
  show('login-pending', pending);
  $('login-btn').disabled = pending;
  if (pending) {
    $('login-code').textContent = tw.login.user_code;
    $('login-link').href = tw.login.verification_uri;
    $('login-link').textContent = tw.login.verification_uri.replace(/\?.*$/, '');
  }
  show('login-error', tw.login.state === 'error');
  $('login-error').textContent = tw.login.error || '';

  if (pending && !loginPoll) loginPoll = setInterval(refresh, 2000);
  if (!pending && loginPoll) { clearInterval(loginPoll); loginPoll = null; }

  if (tw.logged_in && !channelLoaded) { channelLoaded = true; loadChannel(); }
  if (!tw.logged_in) channelLoaded = false;
}

async function refresh() {
  try {
    render(await api('GET', '/api/status'));
  } catch (e) {
    $('server').textContent = 'plugin offline';
    $('server').className = 'err';
  }
}

// ---- login -----------------------------------------------------------------

$('login-btn').addEventListener('click', async () => {
  $('login-btn').disabled = true;
  try {
    await api('POST', '/api/twitch/login', {});
  } catch (e) {
    $('login-error').textContent = e.message;
    show('login-error', true);
  }
  refresh();
});

$('logout').addEventListener('click', async ev => {
  ev.preventDefault();
  if (!confirm('Log out of Twitch?')) return;
  await api('POST', '/api/twitch/logout', {}).catch(() => {});
  refresh();
});

// ---- channel form ------------------------------------------------------------

let selected = null; // {id, name, box_art_url}

function setCategory(cat) {
  selected = cat;
  $('category').value = cat ? cat.name : '';
  $('category-art').src = cat && cat.box_art_url ? cat.box_art_url : '';
}

function updateCount() {
  $('title-count').textContent = `${$('title').value.length}/140`;
}
$('title').addEventListener('input', updateCount);

async function loadChannel() {
  try {
    const ch = await api('GET', '/api/twitch/channel');
    $('title').value = ch.title;
    updateCount();
    setCategory(ch.game_id ? { id: ch.game_id, name: ch.game_name, box_art_url: artUrl(ch.game_id) } : null);
  } catch (e) {
    result('Twitch', false, e.message);
  }
}

// Box art for a known game id, same CDN pattern Twitch uses in search results.
function artUrl(gameId) {
  return `https://static-cdn.jtvnw.net/ttv-boxart/${gameId}-52x72.jpg`;
}

function result(platform, ok, msg) {
  const li = document.createElement('li');
  li.className = ok ? 'ok' : 'err';
  li.textContent = `${ok ? '✓' : '✗'} ${platform}: ${msg}`;
  $('results').replaceChildren(li);
}

$('form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const title = $('title').value.trim();
  const catText = $('category').value.trim();
  if (catText && (!selected || selected.name !== catText)) {
    return result('Twitch', false, 'Pick a category from the list');
  }
  $('apply').disabled = true;
  try {
    const ch = await api('POST', '/api/twitch/channel', { title, game_id: selected ? selected.id : null });
    result('Twitch', true, `updated (${ch.game_name || 'no category'})`);
  } catch (e) {
    result('Twitch', false, e.message);
  } finally {
    $('apply').disabled = false;
  }
});

// ---- category autocomplete ---------------------------------------------------

let searchTimer = null;
let searchSeq = 0;
let items = [];
let active = -1;

function closeSuggestions() { show('suggestions', false); items = []; active = -1; }

function renderSuggestions() {
  const ul = $('suggestions');
  ul.replaceChildren(...items.map((c, i) => {
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
      if (seq !== searchSeq) return; // a newer search is on its way
      items = res.data;
      active = items.length ? 0 : -1;
      renderSuggestions();
    } catch (e) {
      closeSuggestions();
      result('Twitch', false, e.message);
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

// ---- start -----------------------------------------------------------------------

refresh();
setInterval(refresh, 10000);
