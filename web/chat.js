'use strict';

// Combined chat dock. Twitch is read directly over Twitch's anonymous IRC
// WebSocket, YouTube comes from the plugin (/api/chat/youtube).
// Chat text is untrusted: it is only ever inserted as text nodes, never as HTML.

const $ = id => document.getElementById(id);
const feed = $('feed');
const MAX_MESSAGES = 300;
const BADGES = { owner: 'HOST', mod: 'MOD', vip: 'VIP', member: 'SUB', verified: '✓' };

let cfg = null;       // /api/chat/config
let emotes = {};      // 7TV/BTTV/FFZ: name -> url, for rendering messages
let emoteSets = [];   // picker groups in display order: {key, label, source, scope, emotes: [{name, url}]}
let emoteIndex = [];  // every name once (first set wins): {name, lower, url, label, rank}
let emoteNames = new Set();
let twitchEmoteNote = null;

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

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- tokens (text + emotes) ------------------------------------------------

function textTokens(text) {
  return text.split(/(\s+)/).filter(Boolean).map(w =>
    emotes[w] ? { emote: emotes[w], name: w } : { text: w });
}

// Twitch "emotes" tag: "25:0-4,12-16/1902:6-10", positions count code points.
function twitchTokens(text, tag) {
  const cps = Array.from(text);
  const ranges = [];
  if (tag) {
    for (const part of tag.split('/')) {
      const [id, pos] = part.split(':');
      if (!id || !pos) continue;
      for (const r of pos.split(',')) {
        const [a, b] = r.split('-').map(Number);
        ranges.push({ a, b, id });
      }
    }
  }
  ranges.sort((x, y) => x.a - y.a);
  const out = [];
  let i = 0;
  for (const r of ranges) {
    if (r.a < i) continue;
    if (r.a > i) out.push(...textTokens(cps.slice(i, r.a).join('')));
    out.push({ emote: `https://static-cdn.jtvnw.net/emoticons/v2/${encodeURIComponent(r.id)}/default/dark/1.0`,
               name: cps.slice(r.a, r.b + 1).join('') });
    i = r.b + 1;
  }
  if (i < cps.length) out.push(...textTokens(cps.slice(i).join('')));
  return out;
}

function renderTokens(parent, tokens) {
  for (const t of tokens) {
    if (t.emote && t.emote.startsWith('https://')) {
      const img = el('img', 'emote');
      img.src = t.emote;
      img.alt = t.name;
      img.title = t.name;
      parent.append(img);
    } else {
      parent.append(document.createTextNode(t.text ?? t.name ?? ''));
    }
  }
}

// ---- feed ------------------------------------------------------------------

function atBottom() {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
}

function scrollToBottom() {
  feed.scrollTop = feed.scrollHeight;
  $('more').hidden = true;
}

feed.addEventListener('scroll', () => { if (atBottom()) $('more').hidden = true; });
$('more').addEventListener('click', scrollToBottom);

// m: {platform, id, kind, author, author_id, color, badges[], tokens[], amount, notice, action}
function addMessage(m) {
  const stick = atBottom();
  const row = el('div', `msg ${m.platform} ${m.kind || 'text'}`);
  if (m.id) row.dataset.id = m.id;
  if (m.author_id) row.dataset.author = `${m.platform}:${m.author_id}`;

  row.append(el('span', `plat ${m.platform}`, m.platform === 'twitch' ? 'T' : 'YT'));
  if (m.notice) row.append(el('span', 'notice', m.notice));
  if (m.amount) row.append(el('span', 'amount', m.amount));
  for (const b of m.badges || []) if (BADGES[b]) row.append(el('span', `badge ${b}`, BADGES[b]));
  if (m.author) {
    const name = el('span', 'name', m.author);
    if (m.color) name.style.color = m.color;
    row.append(name, document.createTextNode(m.action ? ' ' : ': '));
  }
  const body = el('span', m.action ? 'text action' : 'text');
  if (m.action && m.color) body.style.color = m.color;
  renderTokens(body, m.tokens || []);
  row.append(body);
  row.msg = m;
  if (moderatable(m)) row.append(modActions(m));

  feed.append(row);
  while (feed.childElementCount > MAX_MESSAGES) feed.firstElementChild.remove();
  if (stick) scrollToBottom(); else $('more').hidden = false;
}

function removeById(id) {
  for (const r of feed.querySelectorAll('.msg')) if (r.dataset.id === id) r.classList.add('deleted');
}

function removeByAuthor(key) {
  for (const r of feed.querySelectorAll('.msg')) if (r.dataset.author === key) r.classList.add('deleted');
}

function system(platform, text) {
  addMessage({ platform, kind: 'system', tokens: [{ text }] });
}

// ---- Twitch (anonymous IRC over WebSocket) -----------------------------------

let ws = null;
let twBackoff = 1000;

function setTw(text, cls) { $('tw-state').textContent = text; $('tw-state').className = cls || 'muted'; }

function unescapeTag(v) {
  return v.replace(/\\(.)/g, (_, c) => ({ s: ' ', ':': ';', '\\': '\\', r: '\r', n: '\n' })[c] ?? c);
}

function parseIrc(line) {
  const msg = { tags: {}, prefix: '', command: '', params: [], trailing: '' };
  let rest = line;
  if (rest[0] === '@') {
    const i = rest.indexOf(' ');
    for (const kv of rest.slice(1, i).split(';')) {
      const j = kv.indexOf('=');
      msg.tags[j < 0 ? kv : kv.slice(0, j)] = j < 0 ? '' : unescapeTag(kv.slice(j + 1));
    }
    rest = rest.slice(i + 1);
  }
  if (rest[0] === ':') {
    const i = rest.indexOf(' ');
    msg.prefix = rest.slice(1, i);
    rest = rest.slice(i + 1);
  }
  const t = rest.indexOf(' :');
  if (t >= 0) { msg.trailing = rest.slice(t + 2); rest = rest.slice(0, t); }
  const parts = rest.split(' ');
  msg.command = parts[0];
  msg.params = parts.slice(1);
  return msg;
}

function twitchBadges(tag) {
  const out = [];
  for (const b of (tag || '').split(',')) {
    const name = b.split('/')[0];
    if (name === 'broadcaster') out.push('owner');
    else if (name === 'moderator') out.push('mod');
    else if (name === 'vip') out.push('vip');
    else if (name === 'subscriber' || name === 'founder') out.push('member');
  }
  return out;
}

function handleIrc(line) {
  const m = parseIrc(line);
  switch (m.command) {
    case 'PING':
      ws.send('PONG :' + m.trailing);
      break;
    case 'ROOMSTATE':
      setTw('connected', 'ok');
      twBackoff = 1000;
      updateRoomState(m.tags);
      break;
    case 'RECONNECT':
      ws.close();
      break;
    case 'PRIVMSG': {
      let text = m.trailing;
      const action = text.startsWith('\u0001ACTION ') && text.endsWith('\u0001');
      if (action) text = text.slice(8, -1);
      addMessage({
        platform: 'twitch', kind: 'text', id: m.tags.id, action, login: m.prefix.split('!')[0],
        author: m.tags['display-name'] || m.prefix.split('!')[0],
        author_id: m.tags['user-id'], color: m.tags.color || null,
        badges: twitchBadges(m.tags.badges), badgesRaw: m.tags.badges, badgeInfo: m.tags['badge-info'],
        ts: Number(m.tags['tmi-sent-ts']) || Date.now(),
        tokens: action ? twitchTokens(text, null) : twitchTokens(text, m.tags.emotes),
      });
      break;
    }
    case 'USERNOTICE':
      addMessage({
        platform: 'twitch', kind: 'notice', id: m.tags.id,
        notice: m.tags['system-msg'] || m.tags['msg-id'] || 'notice',
        author: m.trailing ? (m.tags['display-name'] || '') : '', login: m.tags.login,
        author_id: m.tags['user-id'], color: m.tags.color || null,
        badges: twitchBadges(m.tags.badges), badgesRaw: m.tags.badges, badgeInfo: m.tags['badge-info'],
        ts: Number(m.tags['tmi-sent-ts']) || Date.now(),
        tokens: m.trailing ? twitchTokens(m.trailing, m.tags.emotes) : [],
      });
      break;
    case 'CLEARMSG':
      removeById(m.tags['target-msg-id']);
      break;
    case 'CLEARCHAT':
      if (m.tags['target-user-id']) removeByAuthor('twitch:' + m.tags['target-user-id']);
      else system('twitch', 'Chat was cleared by a moderator');
      break;
  }
}

function connectTwitch() {
  if (!cfg || !cfg.twitch) { setTw('not logged in'); return; }
  setTw('connecting …');
  ws = new WebSocket('wss://irc-ws.chat.twitch.tv:443');
  ws.onopen = () => {
    ws.send('CAP REQ :twitch.tv/tags twitch.tv/commands');
    ws.send('PASS SCHMOOPIIE');
    ws.send('NICK justinfan' + Math.floor(10000 + Math.random() * 80000));
    ws.send('JOIN #' + cfg.twitch.login.toLowerCase());
  };
  ws.onmessage = ev => String(ev.data).split('\r\n').filter(Boolean).forEach(handleIrc);
  ws.onclose = () => {
    setTw(`reconnecting in ${Math.round(twBackoff / 1000)} s`, 'warn');
    setTimeout(connectTwitch, twBackoff);
    twBackoff = Math.min(twBackoff * 2, 30000);
  };
}

// ---- YouTube (via the plugin) ------------------------------------------------

let ytSeq = 0;
const YT_STATES = {
  idle: ['not connected', 'muted'], searching: ['waiting for the live broadcast …', 'warn'],
  connected: ['connected', 'ok'], retrying: ['reconnecting …', 'warn'],
  ended: ['broadcast ended', 'muted'], error: ['error', 'err'],
};

function renderYtState(st) {
  const [label, cls] = YT_STATES[st.state] || [st.state, 'muted'];
  let text = label;
  if (st.state === 'retrying' && st.retry_in) text = `reconnecting in ${st.retry_in} s`;
  if (st.error && st.state !== 'retrying') text += `: ${st.error}`;
  else if (st.note) text += ` (${st.note})`;
  $('yt-state').textContent = text;
  $('yt-state').className = cls;
  $('yt-connect').hidden = !(cfg && cfg.youtube.logged_in) || ['connected', 'searching'].includes(st.state);
}

function ytMessage(m) {
  if (m.kind === 'delete') return removeById(m.target);
  if (m.kind === 'ban') return m.target_author_id && removeByAuthor('youtube:' + m.target_author_id);
  const badges = [];
  if (m.owner) badges.push('owner');
  if (m.mod) badges.push('mod');
  if (m.member) badges.push('member');
  if (m.verified) badges.push('verified');
  addMessage({
    platform: 'youtube', kind: m.kind, id: m.id, author: m.author, author_id: m.author_id,
    badges, amount: m.amount || null, avatar: m.avatar || null, ts: Date.parse(m.ts) || Date.now(),
    tokens: textTokens(m.text || ''),
  });
}

async function pollYoutube() {
  try {
    const r = await api('GET', '/api/chat/youtube?since=' + ytSeq);
    renderYtState(r.state);
    if (r.seq < ytSeq) ytSeq = 0;           // plugin was reloaded, start over
    for (const m of r.messages) ytMessage(m);
    ytSeq = r.seq;
  } catch (e) {
    $('yt-state').textContent = e.message;
    $('yt-state').className = 'err';
  }
  setTimeout(pollYoutube, 1000);
}

$('yt-connect').addEventListener('click', async ev => {
  ev.preventDefault();
  try { await api('POST', '/api/chat/youtube/connect', {}); } catch (e) { showError(e.message); }
});

// ---- sending -----------------------------------------------------------------

let errorTimer = null;
function showError(text) {
  $('send-error').textContent = text;
  $('send-error').hidden = false;
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => { $('send-error').hidden = true; }, 8000);
}

function selectedTargets() {
  const t = [];
  if ($('to-twitch').checked && !$('to-twitch').disabled) t.push('twitch');
  if ($('to-youtube').checked && !$('to-youtube').disabled) t.push('youtube');
  return t;
}

function updateCount() {
  const toYt = selectedTargets().includes('youtube');
  const limit = toYt ? cfg.limits.youtube : cfg.limits.twitch;
  const n = $('text').value.length;
  $('count').textContent = n ? `${n}/${limit}` : '';
  $('count').className = n > limit ? 'err' : 'muted';
  // YouTube has no 7TV/BTTV/FFZ or Twitch emotes: viewers there just see the name.
  $('send-hint').hidden = !(toYt && $('text').value.split(/\s+/).some(w => emoteNames.has(w)));
}

$('text').addEventListener('input', updateCount);
$('to-twitch').addEventListener('change', updateCount);
$('to-youtube').addEventListener('change', updateCount);

$('send').addEventListener('submit', async ev => {
  ev.preventDefault();
  const text = $('text').value.trim();
  const targets = selectedTargets();
  if (!text) return;
  if (text.startsWith('/')) return runCommand(text);
  if (!targets.length) return showError('Pick at least one platform');
  $('text').disabled = true;
  try {
    const r = await api('POST', '/api/chat/send', { text, targets });
    const failed = Object.entries(r.results).filter(([, v]) => !v.ok);
    if (failed.length) showError(failed.map(([k, v]) => `${k === 'twitch' ? 'Twitch' : 'YouTube'}: ${v.error}`).join(' · '));
    if (failed.length < targets.length) $('text').value = '';
  } catch (e) {
    showError(e.message);
  } finally {
    $('text').disabled = false;
    $('text').focus();
    updateCount();
  }
});

$('text').addEventListener('keydown', ev => {
  if (!$('ac').hidden) {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const n = ac.items.length;
      setActive((ac.active + (ev.key === 'ArrowDown' ? 1 : n - 1)) % n);
      return;
    }
    if (ev.key === 'Tab' || ev.key === 'Enter') { ev.preventDefault(); acceptSuggestion(ac.active); return; }
    if (ev.key === 'Escape') { ev.preventDefault(); closeAc(); return; }
  }
  if (ev.key === 'Escape' && !$('picker').hidden) { ev.preventDefault(); togglePicker(false); return; }
  if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('send').requestSubmit(); }
});

// ---- emotes: index, picker, ":" autocomplete ------------------------------------

const SOURCE_SHORT = { twitch: 'Twitch', '7tv': '7TV', bttv: 'BTTV', ffz: 'FFZ' };

function buildEmoteIndex() {
  emoteIndex = [];
  emoteNames = new Set();
  emoteSets.forEach((set, rank) => {
    for (const e of set.emotes) {
      if (emoteNames.has(e.name)) continue;  // same name in a later set: the earlier one wins
      emoteNames.add(e.name);
      emoteIndex.push({ name: e.name, lower: e.name.toLowerCase(), url: e.url, label: set.label, rank });
    }
  });
}

// Exact match, then start of the name, then anywhere in it (":gers" finds POGGERS).
// Within each: channel sets before global ones, then shorter names.
function searchEmotes(query, limit) {
  const q = query.toLowerCase();
  const hits = [];
  for (const e of emoteIndex) {
    const at = e.lower.indexOf(q);
    if (at < 0) continue;
    hits.push({ e, kind: e.lower === q ? 0 : at === 0 ? 1 : 2, at });
  }
  hits.sort((a, b) => a.kind - b.kind || a.e.rank - b.e.rank || a.e.name.length - b.e.name.length
    || a.e.lower.localeCompare(b.e.lower));
  return (limit ? hits.slice(0, limit) : hits);
}

function insertAt(start, end, name) {
  const input = $('text');
  const v = input.value;
  const before = v.slice(0, start);
  const after = v.slice(end);
  const lead = before && !/\s$/.test(before) ? ' ' : '';
  const trail = /^\s/.test(after) ? '' : ' ';
  input.value = before + lead + name + trail + after;
  const pos = before.length + lead.length + name.length + trail.length;
  input.focus();
  input.setSelectionRange(pos, pos);
  updateCount();
}

// -- picker

function emoteButton(name, url) {
  const b = el('button');
  b.type = 'button';
  b.dataset.name = name;
  b.title = name;
  const img = el('img');
  img.src = url;
  img.alt = name;
  img.loading = 'lazy';
  b.append(img);
  return b;
}

function renderPicker() {
  const list = $('picker-list');
  list.replaceChildren();
  const q = $('picker-search').value.trim();
  if (q) {
    const hits = searchEmotes(q);
    const grid = el('div', 'emote-grid');
    for (const { e } of hits) grid.append(emoteButton(e.name, e.url));
    list.append(hits.length ? grid : el('div', 'muted', 'No emote found'));
  } else {
    for (const set of emoteSets) {
      const group = el('div', 'emote-group');
      group.append(el('h4', null, `${set.label} (${set.emotes.length})`));
      const grid = el('div', 'emote-grid');
      for (const e of set.emotes) grid.append(emoteButton(e.name, e.url));
      group.append(grid);
      list.append(group);
    }
    if (!emoteSets.length) list.append(el('div', 'muted', 'Loading emotes …'));
  }
  $('picker-note').textContent = twitchEmoteNote || '';
  $('picker-note').hidden = !twitchEmoteNote;
}

function togglePicker(open = $('picker').hidden) {
  $('picker').hidden = !open;
  $('emote-btn').setAttribute('aria-expanded', String(open));
  if (open) {
    closeAc();
    renderPicker();
    $('picker-search').focus();
  } else {
    $('text').focus();
  }
}

$('emote-btn').addEventListener('click', () => togglePicker());
$('picker-search').addEventListener('input', renderPicker);
$('picker-search').addEventListener('keydown', ev => {
  if (ev.key === 'Escape') { ev.preventDefault(); togglePicker(false); }
  if (ev.key === 'Enter') {  // Enter in the search takes the first hit
    ev.preventDefault();
    const first = $('picker-list').querySelector('button[data-name]');
    if (first) first.click();
  }
});
// Inserting keeps the picker open, like on Twitch, so several emotes are quick.
$('picker-list').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-name]');
  if (!b) return;
  const input = $('text');
  insertAt(input.selectionStart ?? input.value.length, input.selectionEnd ?? input.value.length, b.dataset.name);
});
document.addEventListener('mousedown', ev => {
  if (!$('picker').hidden && !ev.target.closest('#picker, #emote-btn')) togglePicker(false);
});

// -- ":" autocomplete

const ac = { items: [], active: 0, start: 0, end: 0 };

function closeAc() { $('ac').hidden = true; ac.items = []; }

function setActive(i) {
  ac.active = i;
  [...$('ac').children].forEach((li, j) => li.classList.toggle('active', j === i));
  const li = $('ac').children[i];
  if (li) li.scrollIntoView({ block: 'nearest' });
}

function updateAc() {
  const input = $('text');
  const pos = input.selectionStart;
  // ":" at the start or after a space, then at least 2 characters. So ":D", URLs
  // and times like 12:30 don't open the list.
  const m = pos === input.selectionEnd && input.value.slice(0, pos).match(/(?:^|\s):([^\s:]{2,})$/);
  const hits = m ? searchEmotes(m[1], 8) : [];
  if (!hits.length) return closeAc();
  ac.items = hits.map(h => h.e);
  ac.start = pos - m[1].length - 1;
  ac.end = pos;
  const list = $('ac');
  list.replaceChildren();
  hits.forEach(({ e, at }, i) => {
    const li = el('li');
    li.setAttribute('role', 'option');
    const img = el('img');
    img.src = e.url;
    img.alt = '';
    const name = el('span');
    const len = m[1].length;
    name.append(document.createTextNode(e.name.slice(0, at)), el('mark', null, e.name.slice(at, at + len)),
      document.createTextNode(e.name.slice(at + len)));
    li.append(img, name, el('span', 'src', e.label));
    li.addEventListener('mousedown', ev => { ev.preventDefault(); acceptSuggestion(i); });
    list.append(li);
  });
  list.hidden = false;
  setActive(0);
}

function acceptSuggestion(i) {
  const e = ac.items[i];
  if (!e) return closeAc();
  const { start, end } = ac;
  closeAc();
  insertAt(start, end, e.name);
}

$('text').addEventListener('input', updateAc);
$('text').addEventListener('blur', () => setTimeout(closeAc, 100));

// ---- moderation ----------------------------------------------------------------
// Hover icons on messages, a user card (click a name), the Twitch chat-mode panel
// (🛡 in the header) and Twitch-style slash commands. Everything goes through
// POST /api/mod. Bans and clearing the chat need a second click to confirm.

const PNAME = { twitch: 'Twitch', youtube: 'YouTube' };

function canMod(platform) {
  if (!cfg) return false;
  return platform === 'twitch' ? !!(cfg.twitch && cfg.twitch.can_moderate) : !!cfg.youtube.can_moderate;
}

function hasUser(m) {
  return !!(m && m.author && m.author_id && m.kind !== 'system');
}

function moderatable(m) {
  return hasUser(m) && !(m.badges || []).includes('owner') && canMod(m.platform);
}

function targetOf(m) {
  return { name: m.author, user_id: m.author_id, login: m.login, message_id: m.id };
}

function fmtDur(s) {
  for (const [n, u] of [[604800, 'w'], [86400, 'd'], [3600, 'h'], [60, 'min']]) {
    if (s >= n && s % n === 0) return `${s / n} ${u}`;
  }
  return `${s} s`;
}

// "600", "30s", "10m", "1h", "1d", "2w" -> seconds
function parseDur(s) {
  const m = /^(\d+)([smhdw]?)$/i.exec(s || '');
  return m ? +m[1] * { '': 1, s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[m[2].toLowerCase()] : null;
}

// First click arms the button for 3 s, the second one confirms.
function armed(btn, label) {
  if (btn.classList.contains('arming')) {
    clearTimeout(btn._disarm);
    btn.classList.remove('arming');
    btn.textContent = btn._label;
    return true;
  }
  btn._label = btn.textContent;
  btn.textContent = label;
  btn.classList.add('arming');
  btn._disarm = setTimeout(() => { btn.classList.remove('arming'); btn.textContent = btn._label; }, 3000);
  return false;
}

// Runs one action, reports success in the feed and errors under the input.
// Rejects on error so callers can keep a card open.
async function act(platform, action, target, opts = {}) {
  const payload = { platform, action, ...opts };
  if (target.user_id) payload[platform === 'twitch' ? 'user_id' : 'channel_id'] = target.user_id;
  if (target.login) payload.login = target.login;
  if (target.name) payload.name = target.name;
  if (target.message_id) payload.message_id = target.message_id;
  let r;
  try {
    r = await api('POST', '/api/mod', payload);
  } catch (e) {
    showError(`${PNAME[platform]}: ${e.message.replace(/^(Twitch|YouTube): /, '')}`);
    throw e;
  }
  const who = (r.user && r.user.display_name) || target.name || target.login || 'user';
  const uid = target.user_id || (r.user && r.user.id);
  switch (action) {
    case 'delete': removeById(target.message_id); break;
    case 'timeout':
      if (uid) removeByAuthor(`${platform}:${uid}`);
      system(platform, `✓ ${who} timed out for ${fmtDur(opts.duration)}`);
      break;
    case 'ban':
      if (uid) removeByAuthor(`${platform}:${uid}`);
      system(platform, `✓ ${who} banned`);
      break;
    case 'unban': system(platform, `✓ ${who} unbanned`); break;
    case 'warn': system(platform, `✓ ${who} warned: ${opts.reason}`); break;
    case 'shoutout': system(platform, `✓ Shoutout to ${who}`); break;
    case 'settings': if (r.settings) applySettings(r.settings); break;
    case 'shield':
      shieldOn = r.shield;
      renderModes();
      system('twitch', r.shield ? '🛡 Shield Mode on' : 'Shield Mode off');
      break;
  }
  return r;
}

// -- hover icons

function modActions(m) {
  const box = el('span', 'mod-actions');
  const t = targetOf(m);
  const yt = m.platform === 'youtube';
  const add = (icon, title, fn) => {
    const b = el('button', null, icon);
    b.type = 'button';
    b.title = title;
    b.addEventListener('click', ev => { ev.stopPropagation(); fn(b); });
    box.append(b);
  };
  if (m.id) add('🗑', yt ? 'Delete message (20 quota units)' : 'Delete message',
    () => act(m.platform, 'delete', t).catch(() => {}));
  add('⏱', yt ? 'Timeout 10 min (200 quota units)' : 'Timeout 10 min',
    () => act(m.platform, 'timeout', t, { duration: 600 }).catch(() => {}));
  add('⛔', 'Ban (click twice)', b => { if (armed(b, '✓?')) act(m.platform, 'ban', t).catch(() => {}); });
  return box;
}

// -- user card, laid out like Twitch's own viewer card

let badgeMap = null;   // Twitch 'set/version' -> {url, title}
let badgeLoad = null;
function loadBadges() {
  if (!badgeLoad) {
    badgeLoad = api('GET', '/api/twitch/badges')
      .then(r => (badgeMap = r.badges), () => { badgeLoad = null; return null; });
  }
  return badgeLoad;
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
  'October', 'November', 'December'];
function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
function longDate(iso) {
  const d = new Date(iso);
  return `${MONTHS[d.getMonth()]} ${ordinal(d.getDate())}, ${d.getFullYear()}`;
}
function hhmm(ts) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// Badge images from Twitch's badge list, text chips as fallback (YouTube, list not loaded).
function badgeEls(m) {
  if (m.platform === 'twitch' && badgeMap && m.badgesRaw) {
    const out = [];
    for (const b of m.badgesRaw.split(',')) {
      const info = badgeMap[b];
      if (!info) continue;
      const img = el('img', 'badge-img');
      img.src = info.url;
      img.alt = img.title = info.title;
      out.push(img);
    }
    return out;
  }
  return (m.badges || []).filter(b => BADGES[b]).map(b => el('span', `badge ${b}`, BADGES[b]));
}

function closeCard() { $('usercard').hidden = true; }

function openCard(m) {
  toggleModPanel(false);
  const card = $('usercard');
  card.replaceChildren();
  const p = m.platform;
  const t = targetOf(m);
  const key = `${p}:${m.author_id}`;
  card.dataset.key = key;
  const stillOpen = () => !card.hidden && card.dataset.key === key;
  const color = m.color || (p === 'youtube' ? '#f5c2e7' : '#cba6f7');

  // banner: avatar, name in the user's colour, badges
  const banner = el('div', 'uc-banner');
  banner.style.setProperty('--uc-color', color);
  const avatar = el('div', 'uc-avatar', (m.author[0] || '?').toUpperCase());
  const setAvatar = url => {
    if (!url) return;
    const img = el('img');
    img.alt = '';
    img.onload = () => avatar.replaceChildren(img);  // keep the initial until the picture is there
    img.src = url;
  };
  setAvatar(m.avatar);
  const ident = el('div', 'uc-id');
  const name = el('div', 'uc-name', m.author);
  name.style.color = color;
  const badges = el('div', 'uc-badges');
  badges.append(...badgeEls(m));
  ident.append(name, badges);
  const tools = el('div', 'uc-tools');
  const close = el('button', 'pop-close', '×');
  close.type = 'button';
  close.title = 'Close';
  close.addEventListener('click', closeCard);
  tools.append(el('span', `plat ${p}`, p === 'twitch' ? 'T' : 'YT'), close);
  banner.append(avatar, ident, tools);
  card.append(banner);

  // info lines
  const info = el('div', 'uc-info');
  const line = (icon, text) => {
    const d = el('div', 'uc-line');
    d.append(el('span', 'uc-ico', icon), el('span', null, text));
    info.append(d);
    return d.lastChild;
  };
  if (p === 'twitch') {
    if (!badgeMap) loadBadges().then(() => { if (stillOpen()) badges.replaceChildren(...badgeEls(m)); });
    const created = line('🎂', 'Loading profile …');
    api('GET', '/api/twitch/user?id=' + encodeURIComponent(m.author_id)).then(u => {
      if (!stillOpen()) return;
      setAvatar(u.profile_image_url);
      created.textContent = `Account created on ${longDate(u.created_at)}`;
      if (u.broadcaster_type) line('✔', u.broadcaster_type === 'partner' ? 'Twitch Partner' : 'Twitch Affiliate');
    }).catch(e => { if (stillOpen()) created.textContent = e.message; });
    const sub = /(?:^|,)(subscriber|founder)\/(\d+)/.exec(m.badgeInfo || '');
    if (sub) line('★', `${sub[1] === 'founder' ? 'Founder, subscribed' : 'Subscribed'} for ${sub[2]} month${sub[2] === '1' ? '' : 's'}`);
    if (m.login && m.login !== m.author.toLowerCase()) line('@', m.login);
  } else {
    line('▶', 'YouTube channel');
    if ((m.badges || []).includes('member')) line('★', 'Channel member');
  }
  card.append(info);

  // moderation
  const actions = el('div', 'uc-actions');
  if ((m.badges || []).includes('owner')) {
    actions.append(el('div', 'muted small', 'That is you.'));
  } else if (!canMod(p)) {
    actions.append(el('div', 'err small', p === 'twitch'
      ? 'Log in to Twitch again in the Stream Info dock to allow moderation.'
      : 'Log in to YouTube in the Stream Info dock to moderate.'));
  } else {
    const reason = el('input', 'uc-reason');
    reason.placeholder = p === 'twitch' ? 'Reason (optional, needed for warn)' : 'YouTube takes no reasons';
    reason.maxLength = 500;
    reason.disabled = p === 'youtube';
    actions.append(reason);
    const R = () => reason.value.trim() || undefined;
    const row = label => { const r = el('div', 'uc-row'); r.append(el('span', null, label)); actions.append(r); return r; };
    const btn = (parent, text, fn, cls) => {
      const b = el('button', cls, text);
      b.type = 'button';
      b.addEventListener('click', () => fn(b));
      parent.append(b);
    };
    const done = promise => promise.then(closeCard, () => {});  // stays open on error

    const to = row('Timeout');
    const durations = [60, 600, 3600, 86400].concat(p === 'twitch' ? [604800] : []);
    for (const s of durations) btn(to, fmtDur(s), () => done(act(p, 'timeout', t, { duration: s, reason: R() })));
    const ban = row('Ban');
    btn(ban, 'Permanent', b => { if (armed(b, 'Really ban?')) done(act(p, 'ban', t, { reason: R() })); }, 'danger');
    btn(ban, 'Unban', () => done(act(p, 'unban', t)));
    if (p === 'twitch') {
      const more = row('More');
      btn(more, 'Warn', () => {
        if (!R()) { reason.focus(); return showError('A warning needs a reason'); }
        done(act(p, 'warn', t, { reason: R() }));
      });
      btn(more, 'Shoutout', () => done(act(p, 'shoutout', t)));
    }
    const msgRow = [...feed.querySelectorAll('.msg')].find(r => r.dataset.id === m.id);
    if (m.id && msgRow && !msgRow.classList.contains('deleted')) {
      btn(row('Message'), 'Delete this message', () => done(act(p, 'delete', t)));
    }
    if (p === 'youtube') {
      actions.append(el('div', 'muted small', 'Quota: delete 20, timeout/ban/unban 200 units each (10,000 per day). '
        + 'Unban works for bans made from this dock.'));
    }
  }
  card.append(actions);

  // this user's messages in the feed, with time and badges
  const rows = [...feed.querySelectorAll('.msg')].filter(r => r.dataset.author === key && r.msg);
  card.append(el('div', 'uc-divider', `${rows.length} message${rows.length === 1 ? '' : 's'} in this feed`));
  const log = el('div', 'uc-log');
  for (const r of rows.slice(-30)) {
    const mm = r.msg;
    const ln = el('div', r.classList.contains('deleted') ? 'deleted' : null);
    ln.append(el('time', null, mm.ts ? hhmm(mm.ts) : ''), ...badgeEls(mm));
    const nm = el('span', 'name', mm.author);
    nm.style.color = color;
    ln.append(nm, document.createTextNode(mm.action ? ' ' : ': '));
    const text = r.querySelector('.text');
    if (text) ln.append(text.cloneNode(true));  // our own rendered nodes: text and emote images
    log.append(ln);
  }
  if (rows.length) card.append(log);

  card.hidden = false;
  log.scrollTop = log.scrollHeight;
}

feed.addEventListener('click', ev => {
  const n = ev.target.closest('.name');
  const row = n && n.closest('.msg');
  if (row && hasUser(row.msg)) openCard(row.msg);
});

// -- Twitch chat modes (ROOMSTATE from IRC) and Shield Mode

let roomState = null;
let shieldOn = null;

function updateRoomState(tags) {
  const prev = roomState;
  const s = { ...(roomState || {}) };
  if ('slow' in tags) s.slow = Number(tags.slow);
  if ('followers-only' in tags) s.followers = Number(tags['followers-only']);
  if ('subs-only' in tags) s.subs = tags['subs-only'] === '1';
  if ('emote-only' in tags) s.emote = tags['emote-only'] === '1';
  if ('r9k' in tags) s.unique = tags.r9k === '1';
  roomState = s;
  if (prev) {
    const on = (a, b, label) => { if (!!a !== !!b) system('twitch', `${label} ${b ? 'on' : 'off'}`); };
    if (prev.slow !== s.slow) system('twitch', s.slow ? `Slow mode on (${s.slow} s)` : 'Slow mode off');
    if (prev.followers !== s.followers) {
      system('twitch', s.followers >= 0 ? `Followers-only on${s.followers ? ` (${fmtDur(s.followers * 60)})` : ''}`
        : 'Followers-only off');
    }
    on(prev.subs, s.subs, 'Subscribers-only');
    on(prev.emote, s.emote, 'Emote-only');
    on(prev.unique, s.unique, 'Unique chat');
  }
  renderModes();
}

// Helix chat settings -> the same shape as ROOMSTATE, so both paths agree.
function applySettings(st) {
  updateRoomState({
    slow: st.slow_mode ? String(st.slow_mode_wait_time || 0) : '0',
    'followers-only': st.follower_mode ? String(st.follower_mode_duration || 0) : '-1',
    'subs-only': st.subscriber_mode ? '1' : '0',
    'emote-only': st.emote_mode ? '1' : '0',
    r9k: st.unique_chat_mode ? '1' : '0',
  });
}

function renderModes() {
  const box = $('modes');
  box.replaceChildren();
  const s = roomState || {};
  const chip = (text, cls, title) => {
    const c = el('span', `mode-chip ${cls || ''}`, text);
    if (title) c.title = title;
    box.append(c);
  };
  if (shieldOn) chip('SHIELD', 'shield', 'Shield Mode is on');
  if (s.slow) chip(`SLOW ${s.slow}s`, '', 'Slow mode');
  if (s.followers >= 0) chip(s.followers ? `FOLLOW ${fmtDur(s.followers * 60).replace(' ', '')}` : 'FOLLOW', '', 'Followers-only');
  if (s.subs) chip('SUB', '', 'Subscribers-only');
  if (s.emote) chip('EMOTE', '', 'Emote-only');
  if (s.unique) chip('UNIQUE', '', 'Unique chat');
  syncPanel();
}

function setSelect(sel, value, label) {
  const v = String(value);
  if (![...sel.options].some(o => o.value === v)) sel.append(new Option(label(value), v));
  sel.value = v;
}

function syncPanel() {
  const s = roomState || {};
  setSelect($('mp-slow'), s.slow || 0, v => `${v} s`);
  setSelect($('mp-followers'), s.followers ?? -1, v => fmtDur(v * 60));
  $('mp-subs').checked = !!s.subs;
  $('mp-emote').checked = !!s.emote;
  $('mp-unique').checked = !!s.unique;
  $('mp-shield').textContent = `Shield Mode: ${shieldOn == null ? '?' : shieldOn ? 'ON' : 'off'}`;
  $('mp-shield').classList.toggle('on', !!shieldOn);
}

async function refreshShield() {
  try {
    const r = await api('GET', '/api/mod/twitch/state');
    shieldOn = r.shield;
    if (r.settings && !roomState) applySettings(r.settings);
    renderModes();
  } catch (e) { /* the panel shows '?' */ }
}
setInterval(() => { if (cfg && cfg.twitch && cfg.twitch.can_moderate) refreshShield(); }, 60 * 1000);

function setModes(settings) {
  act('twitch', 'settings', {}, { settings }).catch(syncPanel);  // on error: show the real state again
}

function toggleModPanel(open = $('modpanel').hidden) {
  if (open) {
    closeCard();
    if (!$('picker').hidden) togglePicker(false);
    const can = canMod('twitch');
    $('mp-note').textContent = can ? '' : 'Log in to Twitch again in the Stream Info dock to allow moderation.';
    $('mp-note').hidden = can;
    for (const c of $('modpanel').querySelectorAll('select, input, .mp-actions button')) c.disabled = !can;
    if (can) refreshShield();
    syncPanel();
  }
  $('modpanel').hidden = !open;
  $('mod-btn').setAttribute('aria-expanded', String(open));
}

$('mod-btn').addEventListener('click', () => toggleModPanel());
$('modpanel').querySelector('.pop-close').addEventListener('click', () => toggleModPanel(false));
$('mp-slow').addEventListener('change', e => {
  const v = +e.target.value;
  setModes(v ? { slow_mode: true, slow_mode_wait_time: v } : { slow_mode: false });
});
$('mp-followers').addEventListener('change', e => {
  const v = +e.target.value;
  setModes(v >= 0 ? { follower_mode: true, follower_mode_duration: v } : { follower_mode: false });
});
$('mp-subs').addEventListener('change', e => setModes({ subscriber_mode: e.target.checked }));
$('mp-emote').addEventListener('change', e => setModes({ emote_mode: e.target.checked }));
$('mp-unique').addEventListener('change', e => setModes({ unique_chat_mode: e.target.checked }));
$('mp-shield').addEventListener('click', () => act('twitch', 'shield', {}, { active: !shieldOn }).catch(() => {}));
$('mp-clear').addEventListener('click', e => {
  if (armed(e.currentTarget, 'Really clear?')) act('twitch', 'clear', {}).catch(() => {});
});

document.addEventListener('mousedown', ev => {
  if (!$('usercard').hidden && !ev.target.closest('#usercard, .msg .name')) closeCard();
  if (!$('modpanel').hidden && !ev.target.closest('#modpanel, #mod-btn')) toggleModPanel(false);
});
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  if (!$('usercard').hidden) closeCard();
  if (!$('modpanel').hidden) toggleModPanel(false);
});

// -- slash commands (act on Twitch, like in Twitch's own chat)

class UsageError extends Error {}

function user(u) {
  if (!u) throw new UsageError();
  const login = u.replace(/^@/, '');
  return { login, name: login };
}

function modes(settings) { return act('twitch', 'settings', {}, { settings }); }

const COMMANDS = {
  ban: ['<user> [reason]', ([u, ...r]) => act('twitch', 'ban', user(u), { reason: r.join(' ') || undefined })],
  unban: ['<user>', ([u]) => act('twitch', 'unban', user(u))],
  timeout: ['<user> [duration: 600, 10m, 1h, 1d, 2w] [reason]', ([u, d, ...r]) => {
    let duration = 600;
    if (d !== undefined) {
      const p = parseDur(d);
      if (p === null) r.unshift(d); else duration = p;
    }
    return act('twitch', 'timeout', user(u), { duration, reason: r.join(' ') || undefined });
  }],
  untimeout: ['<user>', ([u]) => act('twitch', 'unban', user(u))],
  warn: ['<user> <reason>', ([u, ...r]) => {
    if (!r.length) throw new UsageError();
    return act('twitch', 'warn', user(u), { reason: r.join(' ') });
  }],
  shoutout: ['<user>', ([u]) => act('twitch', 'shoutout', user(u))],
  clear: ['', () => act('twitch', 'clear', {})],
  slow: ['[seconds, 3-120]', ([s]) => {
    const v = s === undefined ? 30 : parseDur(s);
    if (v === null || v < 3 || v > 120) throw new UsageError();
    return modes({ slow_mode: true, slow_mode_wait_time: v });
  }],
  slowoff: ['', () => modes({ slow_mode: false })],
  followers: ['[minutes or 10m, 1h, 1d, 1w; default 0]', ([d]) => {
    const v = d === undefined ? 0 : /^\d+$/.test(d) ? +d : (parseDur(d) ?? NaN) / 60;
    if (!Number.isInteger(v) || v < 0 || v > 129600) throw new UsageError();
    return modes({ follower_mode: true, follower_mode_duration: v });
  }],
  followersoff: ['', () => modes({ follower_mode: false })],
  subscribers: ['', () => modes({ subscriber_mode: true })],
  subscribersoff: ['', () => modes({ subscriber_mode: false })],
  emoteonly: ['', () => modes({ emote_mode: true })],
  emoteonlyoff: ['', () => modes({ emote_mode: false })],
  uniquechat: ['', () => modes({ unique_chat_mode: true })],
  uniquechatoff: ['', () => modes({ unique_chat_mode: false })],
  shield: ['', () => act('twitch', 'shield', {}, { active: true })],
  shieldoff: ['', () => act('twitch', 'shield', {}, { active: false })],
  help: ['', () => {
    system('twitch', 'Commands act on Twitch. Moderate YouTube with the icons on a message or by clicking a name.');
    for (const [name, [usage]] of Object.entries(COMMANDS)) if (name !== 'help') system('twitch', `/${name} ${usage}`);
    system('twitch', 'Aliases: /so = /shoutout, /r9kbeta = /uniquechat');
  }],
};
const ALIASES = { so: 'shoutout', r9kbeta: 'uniquechat', r9kbetaoff: 'uniquechatoff', followersonly: 'followers',
  followersonlyoff: 'followersoff', subs: 'subscribers', subsoff: 'subscribersoff', untimeoutuser: 'untimeout' };

async function runCommand(text) {
  const [cmd, ...args] = text.slice(1).split(/\s+/);
  const name = ALIASES[cmd.toLowerCase()] || cmd.toLowerCase();
  const c = COMMANDS[name];
  if (!c) return showError(`Unknown command /${cmd}. /help lists the commands`);
  if (name !== 'help' && !canMod('twitch')) {
    return showError('Log in to Twitch again in the Stream Info dock to allow moderation');
  }
  $('text').disabled = true;
  try {
    await c[1](args);
    $('text').value = '';
  } catch (e) {
    if (e instanceof UsageError) showError(`Usage: /${name} ${c[0]}`);
    // API errors were already shown by act()
  } finally {
    $('text').disabled = false;
    $('text').focus();
    updateCount();
  }
}

// ---- start -------------------------------------------------------------------

let emoteRetry = null;
function loadEmotes() {
  clearTimeout(emoteRetry);
  api('GET', '/api/chat/emotes').then(r => {
    emotes = r.emotes || {};
    emoteSets = r.sets || [];
    twitchEmoteNote = r.twitch_note || null;
    buildEmoteIndex();
    if (!$('picker').hidden) renderPicker();
    if (cfg) updateCount();
    // Still loading in the plugin (a slow service): ask again soon.
    // A service didn't answer: the plugin retries after a minute, so ask again then.
    if (r.loading) emoteRetry = setTimeout(loadEmotes, 2000);
    else if (!r.complete) emoteRetry = setTimeout(loadEmotes, 65 * 1000);
  }).catch(() => { emoteRetry = setTimeout(loadEmotes, 5000); });
}

async function start() {
  try {
    cfg = await api('GET', '/api/chat/config');
  } catch (e) {
    setTw(e.message, 'err');
    setTimeout(start, 3000);
    return;
  }
  const tw = cfg.twitch;
  $('to-twitch').disabled = !(tw && tw.can_send);
  $('to-twitch').parentElement.title = !tw ? 'Not logged in to Twitch'
    : tw.can_send ? 'Send to Twitch' : 'Log in to Twitch again in the Stream Info dock to allow sending';
  $('to-youtube').disabled = !cfg.youtube.logged_in;
  $('mod-btn').hidden = !tw;
  if (tw && tw.can_moderate) refreshShield();
  if (tw) loadBadges();  // so the user card opens with badge images right away
  updateCount();

  loadEmotes();
  setInterval(loadEmotes, 5 * 60 * 1000);  // picks up new emotes and recovers from failed loads
  connectTwitch();
  pollYoutube();
}

start();
