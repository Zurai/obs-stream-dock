'use strict';

// Combined chat dock. Twitch is read directly over Twitch's anonymous IRC
// WebSocket, YouTube comes from the plugin (/api/chat/youtube).
// Chat text is untrusted: it is only ever inserted as text nodes, never as HTML.

const $ = id => document.getElementById(id);
const feed = $('feed');
const MAX_MESSAGES = 300;
const BADGES = { owner: 'HOST', mod: 'MOD', vip: 'VIP', member: 'SUB', verified: '✓' };

let cfg = null;       // /api/chat/config
let emotes = {};      // 7TV/BTTV/FFZ: name -> url

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
      break;
    case 'RECONNECT':
      ws.close();
      break;
    case 'PRIVMSG': {
      let text = m.trailing;
      const action = text.startsWith('\u0001ACTION ') && text.endsWith('\u0001');
      if (action) text = text.slice(8, -1);
      addMessage({
        platform: 'twitch', kind: 'text', id: m.tags.id, action,
        author: m.tags['display-name'] || m.prefix.split('!')[0],
        author_id: m.tags['user-id'], color: m.tags.color || null,
        badges: twitchBadges(m.tags.badges),
        tokens: action ? twitchTokens(text, null) : twitchTokens(text, m.tags.emotes),
      });
      break;
    }
    case 'USERNOTICE':
      addMessage({
        platform: 'twitch', kind: 'notice', id: m.tags.id,
        notice: m.tags['system-msg'] || m.tags['msg-id'] || 'notice',
        author: m.trailing ? (m.tags['display-name'] || '') : '',
        author_id: m.tags['user-id'], color: m.tags.color || null,
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
    badges, amount: m.amount || null,
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
  const limit = selectedTargets().includes('youtube') ? cfg.limits.youtube : cfg.limits.twitch;
  const n = $('text').value.length;
  $('count').textContent = n ? `${n}/${limit}` : '';
  $('count').className = n > limit ? 'err' : 'muted';
}

$('text').addEventListener('input', updateCount);
$('to-twitch').addEventListener('change', updateCount);
$('to-youtube').addEventListener('change', updateCount);

$('send').addEventListener('submit', async ev => {
  ev.preventDefault();
  const text = $('text').value.trim();
  const targets = selectedTargets();
  if (!text) return;
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
  if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('send').requestSubmit(); }
});

// ---- start -------------------------------------------------------------------

function loadEmotes() {
  api('GET', '/api/chat/emotes').then(r => { emotes = r.emotes || {}; }).catch(() => {});
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
  updateCount();

  loadEmotes();
  setInterval(loadEmotes, 5 * 60 * 1000);  // picks up new emotes and recovers from failed loads
  connectTwitch();
  pollYoutube();
}

start();
