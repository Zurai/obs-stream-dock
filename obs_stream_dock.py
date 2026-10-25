"""
OBS Stream Dock

An OBS Python script that serves a small web UI on 127.0.0.1 for use as a
Custom Browser Dock. From the dock you set the stream title and category on
Twitch and the title (and optionally description) on YouTube.

The HTTP server runs in a background thread inside the OBS process. It starts
when the script is loaded and shuts down cleanly when the script is unloaded
(script reload, OBS exit).

Local, never committed:
  config.local.json         client IDs (and the Google client secret)
  data/twitch_tokens.json   Twitch access + refresh token
  data/google_tokens.json   Google access + refresh token
  data/youtube_pending.json title waiting for the YouTube broadcast to exist

The chat dock (web/chat.html) reads Twitch chat directly over Twitch's
anonymous IRC WebSocket and YouTube chat through youtube_chat/helper.py,
a separate process that holds the gRPC streamList connection.
"""

import json
import os
import socket
import subprocess
import sys
import threading
from collections import deque
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import obspython as obs

HOST = "127.0.0.1"
PORT = 47823
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(SCRIPT_DIR, "web")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.local.json")
TWITCH_TOKEN_PATH = os.path.join(DATA_DIR, "twitch_tokens.json")
GOOGLE_TOKEN_PATH = os.path.join(DATA_DIR, "google_tokens.json")
YOUTUBE_PENDING_PATH = os.path.join(DATA_DIR, "youtube_pending.json")

TWITCH_HELIX = "https://api.twitch.tv/helix"
TWITCH_TITLE_MAX = 140
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_GAMING_CATEGORY = "20"
# How often to look for the YouTube broadcast while a title is pending and
# OBS is streaming. liveBroadcasts.list costs 1 quota unit (10,000/day).
YOUTUBE_PENDING_POLL_S = 30
YOUTUBE_PENDING_MAX_AGE_S = 12 * 3600

# Chat
CHAT_HELPER = os.path.join(SCRIPT_DIR, "youtube_chat", "helper.py")
CHAT_BUFFER = 500                     # YouTube messages kept for the dock
CHAT_FIND_POLL_S = 30                 # look for the live broadcast (1 unit per check)
CHAT_FIND_MAX_S = 15 * 60             # give up looking after this long
CHAT_RETRY_MAX_S = 300                # max wait between reconnects (5 units each)
TWITCH_CHAT_MAX = 500
YOUTUBE_CHAT_MAX = 200
EMOTE_CACHE_S = 30 * 60

_server = None
_thread = None
_started_at = None
_stop = threading.Event()  # tells background workers to quit on unload
_streaming = threading.Event()  # mirrors the OBS main stream state
_twitch = None
_youtube = None
_ytchat = None
_emotes = {"at": 0.0, "data": None, "complete": False, "loading": False, "gen": 0, "key": None,
           "lock": threading.Lock()}


def _log(level, msg):
    # OBS only knows the current script inside its own callbacks. Messages
    # from our worker threads would show up as "[Unknown Script]", so tag them.
    if threading.current_thread() is not threading.main_thread():
        msg = f"[{os.path.basename(__file__)}] {msg}"
    obs.script_log(level, msg)


# --- small helpers ------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _log(obs.LOG_WARNING, f"Could not read {os.path.basename(path)}: {e}")
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _delete(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _message(body, fallback="Unknown error"):
    """Error text from Twitch ({"message"}) or Google ({"error"} / {"error": {"message"}})."""
    if not isinstance(body, dict):
        return fallback
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or fallback
    return body.get("message") or err or body.get("error_description") or fallback


def _http(method, url, *, headers=None, form=None, json_body=None, timeout=10):
    """Small urllib wrapper. Returns (status, parsed_json_or_None)."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"message": raw.decode("utf-8", "replace")[:200]}
        return e.code, body
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(502, f"Network error: {getattr(e, 'reason', e)}")


# --- OAuth device flow (shared by Twitch and Google) ----------------------------

class DeviceFlowAccount:
    """Device Code Flow login with token storage and automatic refresh.

    Subclasses set the class attributes and implement _identity().
    """

    label = "?"
    device_url = token_url = revoke_url = ""
    scope_param = "scope"  # Twitch calls it "scopes"
    scopes = ""

    def __init__(self, client_id, client_secret, token_path):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.lock = threading.RLock()
        self.tokens = _read_json(token_path)
        self.login = {"state": "idle"}

    # hooks
    def _identity(self, access_token):
        """Return a dict with at least display_name, stored with the tokens."""
        raise NotImplementedError

    def _check_session(self):
        """Optional periodic check, called with the lock held."""

    def configured(self):
        return bool(self.client_id)

    def _client_form(self):
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form

    def missing_scopes(self):
        """Scopes we ask for now but the stored login doesn't have (older login)."""
        with self.lock:
            if not self.tokens:
                return []
            granted = self.tokens.get("scope") or []
            if isinstance(granted, str):
                granted = granted.split()
            return [s for s in self.scopes.split() if s not in granted]

    # state for the UI
    def public_state(self):
        with self.lock:
            t = self.tokens
            return {
                "configured": self.configured(),
                "logged_in": bool(t),
                "user": {"display_name": t.get("display_name")} if t else None,
                "login": dict(self.login),
                "missing_scopes": self.missing_scopes(),
            }

    # login
    def start_login(self):
        if not self.configured():
            raise ApiError(400, f"{self.label} is not configured (config.local.json)")
        with self.lock:
            if self.login.get("state") == "pending" and time.time() < self.login.get("expires_at", 0):
                return dict(self.login)
        status, body = _http("POST", self.device_url,
                             form={"client_id": self.client_id, self.scope_param: self.scopes})
        if status != 200:
            raise ApiError(status, f"{self.label}: {_message(body)}")
        # Twitch says verification_uri, Google says verification_url.
        uri = body.get("verification_uri") or body.get("verification_url")
        state = {
            "state": "pending",
            "user_code": body["user_code"],
            "verification_uri": uri,
            "expires_at": time.time() + int(body.get("expires_in", 1800)),
        }
        with self.lock:
            self.login = state
        threading.Thread(
            target=self._poll_login,
            args=(body["device_code"], max(int(body.get("interval", 5)), 1), state["expires_at"]),
            name=f"obs-stream-dock-{self.label.lower()}-login",
            daemon=True,
        ).start()
        try:
            webbrowser.open(uri)
        except Exception:
            pass  # the dock shows the link and code as a fallback
        return dict(state)

    def _set_login_error(self, msg):
        with self.lock:
            self.login = {"state": "error", "error": msg}
        _log(obs.LOG_WARNING, f"{self.label} login failed: {msg}")

    def _poll_login(self, device_code, interval, expires_at):
        form = {
            **self._client_form(),
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        if self.scope_param == "scopes":  # Twitch wants the scopes again
            form["scopes"] = self.scopes
        while not _stop.wait(interval):
            if time.time() > expires_at:
                return self._set_login_error("Code expired, please try again")
            try:
                status, body = _http("POST", self.token_url, form=form)
            except ApiError:
                continue  # transient network issue, keep polling
            if status == 200:
                try:
                    tokens = self._tokens_from(body)
                    tokens.update(self._identity(tokens["access_token"]))
                except ApiError as e:
                    return self._set_login_error(e.message)
                with self.lock:
                    self.tokens = tokens
                    self.tokens["checked_at"] = time.time()
                    _write_json(self.token_path, tokens)
                    self.login = {"state": "ok"}
                _log(obs.LOG_INFO, f"{self.label}: logged in as {tokens.get('display_name')}")
                return
            msg = _message(body)
            if msg == "authorization_pending":
                continue
            if msg == "slow_down":
                interval += 5
                continue
            if msg == "access_denied":
                msg = "Access was denied in the browser"
            return self._set_login_error(msg)

    def logout(self):
        with self.lock:
            t = self.tokens
            self.tokens = None
            self.login = {"state": "idle"}
            _delete(self.token_path)
        if t and self.revoke_url:
            try:
                _http("POST", self.revoke_url, form={"client_id": self.client_id, "token": t["access_token"]})
            except ApiError:
                pass

    # tokens
    def _tokens_from(self, body, previous=None):
        return {
            "access_token": body["access_token"],
            # Google does not send a new refresh token on refresh, Twitch does.
            "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": time.time() + int(body.get("expires_in", 0)),
            "scope": body.get("scope", []),
        }

    def _drop_session(self, reason):
        self.tokens = None
        _delete(self.token_path)
        _log(obs.LOG_WARNING, f"{self.label} session dropped: {reason}")
        raise ApiError(401, f"{self.label} login expired, please log in again")

    def _refresh(self):
        # Caller holds self.lock. Twitch refresh tokens are single use, so the
        # lock also makes sure two requests never spend the same one.
        status, body = _http("POST", self.token_url, form={
            **self._client_form(),
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
        })
        if status in (400, 401):
            self._drop_session(_message(body))
        if status != 200:
            raise ApiError(status, f"{self.label}: {_message(body)}")
        self.tokens.update(self._tokens_from(body, self.tokens))
        self.tokens["checked_at"] = time.time()
        _write_json(self.token_path, self.tokens)

    def access_token(self, force_refresh=False):
        with self.lock:
            if not self.tokens:
                raise ApiError(401, f"Not logged in to {self.label}")
            if force_refresh or time.time() > self.tokens.get("expires_at", 0) - 60:
                self._refresh()
            else:
                self._check_session()
            return self.tokens["access_token"]

    def request(self, method, url, params=None, json_body=None, extra_headers=None):
        """Authorized API call, retried once with a fresh token on 401."""
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in (0, 1):
            token = self.access_token(force_refresh=(attempt == 1))
            headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
            status, body = _http(method, url, json_body=json_body, headers=headers)
            if status == 401 and attempt == 0:
                continue
            if status >= 400:
                raise ApiError(status, f"{self.label}: {_message(body)}")
            return body


# --- Twitch -------------------------------------------------------------------

class Twitch(DeviceFlowAccount):
    label = "Twitch"
    device_url = "https://id.twitch.tv/oauth2/device"
    token_url = "https://id.twitch.tv/oauth2/token"
    revoke_url = "https://id.twitch.tv/oauth2/revoke"
    validate_url = "https://id.twitch.tv/oauth2/validate"
    scope_param = "scopes"
    # user:write:chat lets the chat dock send messages as you,
    # user:read:emotes fills its emote picker with every Twitch emote you may use.
    scopes = "channel:manage:broadcast user:write:chat user:read:emotes"

    def __init__(self, client_id):
        super().__init__(client_id, None, TWITCH_TOKEN_PATH)

    def _identity(self, access_token):
        status, body = _http("GET", f"{TWITCH_HELIX}/users",
                             headers={"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id})
        if status != 200 or not body or not body.get("data"):
            raise ApiError(status, _message(body, "Could not read Twitch user"))
        u = body["data"][0]
        return {"user_id": u["id"], "login": u["login"], "display_name": u["display_name"]}

    def _check_session(self):
        # Twitch asks apps to validate tokens at least hourly.
        if time.time() - self.tokens.get("checked_at", 0) < 3600:
            return
        status, _ = _http("GET", self.validate_url,
                          headers={"Authorization": f"OAuth {self.tokens['access_token']}"})
        if status == 401:
            self._refresh()
        self.tokens["checked_at"] = time.time()

    def helix(self, method, path, params=None, json_body=None):
        return self.request(method, f"{TWITCH_HELIX}{path}", params, json_body,
                            extra_headers={"Client-Id": self.client_id})

    def _user_id(self):
        with self.lock:
            if not self.tokens:
                raise ApiError(401, "Not logged in to Twitch")
            return self.tokens["user_id"]

    def get_channel(self):
        body = self.helix("GET", "/channels", {"broadcaster_id": self._user_id()})
        d = body["data"][0]
        return {"title": d["title"], "game_id": d["game_id"], "game_name": d["game_name"]}

    def search_categories(self, query):
        query = query.strip()
        if not query:
            return []
        body = self.helix("GET", "/search/categories", {"query": query, "first": 10})
        return [
            {"id": c["id"], "name": c["name"], "box_art_url": c.get("box_art_url", "")}
            for c in body.get("data", [])
        ]

    def update_channel(self, title=None, game_id=None):
        payload = {}
        if title is not None:
            title = title.strip()
            if not title:
                raise ApiError(400, "Title must not be empty")
            if len(title) > TWITCH_TITLE_MAX:
                raise ApiError(400, f"Title is longer than {TWITCH_TITLE_MAX} characters")
            payload["title"] = title
        if game_id:
            payload["game_id"] = str(game_id)
        if not payload:
            raise ApiError(400, "Nothing to update")
        self.helix("PATCH", "/channels", {"broadcaster_id": self._user_id()}, json_body=payload)
        return self.get_channel()

    def chat_identity(self):
        with self.lock:
            if not self.tokens:
                return None
            return {"login": self.tokens.get("login"), "user_id": self.tokens.get("user_id"),
                    "display_name": self.tokens.get("display_name"),
                    "can_send": "user:write:chat" not in self.missing_scopes()}

    def send_chat(self, text):
        if "user:write:chat" in self.missing_scopes():
            raise ApiError(403, "Twitch: please log in again to allow sending chat messages")
        uid = self._user_id()
        body = self.helix("POST", "/chat/messages", json_body={
            "broadcaster_id": uid, "sender_id": uid, "message": text})
        d = (body or {}).get("data", [{}])[0]
        if not d.get("is_sent"):
            reason = (d.get("drop_reason") or {}).get("message") or "message was dropped"
            raise ApiError(400, f"Twitch: {reason}")
        return d.get("message_id")

    def user_emotes(self):
        """Every Twitch emote you may use, grouped for the picker.

        Returns a list of sets ({key, label, source, emotes: [{name, url}]}):
        your channel first, then other channels (sub emotes etc.), then global.
        Raises ApiError(403) if the login predates the user:read:emotes scope.
        """
        if "user:read:emotes" in self.missing_scopes():
            raise ApiError(403, "Twitch: log in again to load your Twitch emotes")
        uid = self._user_id()
        items, template, after = [], None, None
        for _ in range(30):  # pages of ~100; a hard stop in case the cursor never ends
            params = {"user_id": uid, "broadcaster_id": uid}
            if after:
                params["after"] = after
            body = self.helix("GET", "/chat/emotes/user", params) or {}
            items += body.get("data") or []
            template = body.get("template") or template
            after = (body.get("pagination") or {}).get("cursor")
            if not after:
                break
        template = template or "https://static-cdn.jtvnw.net/emoticons/v2/{{id}}/{{format}}/{{theme_mode}}/{{scale}}"

        channel_types = {"subscriptions", "follower", "bitstier", "channelpoints"}
        own, others, glob = [], {}, []
        seen = set()
        for e in items:
            name, eid = e.get("name"), e.get("id")
            # Smilies like ":)" come as regex patterns (R-?\)) and can't be typed as named.
            if not name or not eid or "\\" in name or (name, eid) in seen:
                continue
            seen.add((name, eid))
            fmt = "animated" if "animated" in (e.get("format") or []) else "static"
            url = (template.replace("{{id}}", urllib.parse.quote(eid)).replace("{{format}}", fmt)
                   .replace("{{theme_mode}}", "dark").replace("{{scale}}", "1.0"))
            emote = {"name": name, "url": url}
            owner = e.get("owner_id") or ""
            if owner == uid:
                own.append(emote)
            elif e.get("emote_type") in channel_types and owner not in ("", "0"):
                others.setdefault(owner, []).append(emote)
            else:
                glob.append(emote)

        names = {}
        ids = list(others)
        for i in range(0, len(ids), 100):
            try:
                body = self.helix("GET", "/users", [("id", x) for x in ids[i:i + 100]]) or {}
                names.update({u["id"]: u["display_name"] for u in body.get("data") or []})
            except ApiError as e:
                _log(obs.LOG_WARNING, f"Emotes: could not read channel names ({e.message})")

        with self.lock:
            me = (self.tokens or {}).get("display_name") or "your channel"
        sets = [{"key": "twitch-channel", "label": f"Twitch · {me}", "source": "twitch",
                 "scope": "channel", "emotes": own}]
        for owner in sorted(others, key=lambda o: names.get(o, o).lower()):
            sets.append({"key": f"twitch-{owner}", "label": f"Twitch · {names.get(owner, owner)}",
                         "source": "twitch", "scope": "other", "emotes": others[owner]})
        sets.append({"key": "twitch-global", "label": "Twitch · global", "source": "twitch",
                     "scope": "global", "emotes": glob})
        return [s for s in sets if s["emotes"]]


# --- YouTube ------------------------------------------------------------------

class YouTube(DeviceFlowAccount):
    label = "YouTube"
    device_url = "https://oauth2.googleapis.com/device/code"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"
    scopes = "https://www.googleapis.com/auth/youtube"

    def __init__(self, client_id, client_secret):
        super().__init__(client_id, client_secret, GOOGLE_TOKEN_PATH)
        self.pending = _read_json(YOUTUBE_PENDING_PATH)
        self.last_result = None  # shown in the dock after a pending title was applied
        self._watcher = None

    def configured(self):
        return bool(self.client_id and self.client_secret)

    def public_state(self):
        state = super().public_state()
        with self.lock:
            state["pending"] = dict(self.pending) if self.pending else None
            state["last_result"] = dict(self.last_result) if self.last_result else None
        return state

    def _identity(self, access_token):
        status, body = _http("GET", f"{YOUTUBE_API}/channels?part=snippet&mine=true",
                             headers={"Authorization": f"Bearer {access_token}"})
        if status != 200 or not body or not body.get("items"):
            raise ApiError(status, _message(body, "This Google account has no YouTube channel"))
        ch = body["items"][0]
        return {"channel_id": ch["id"], "display_name": ch["snippet"]["title"]}

    def api(self, method, path, params=None, json_body=None):
        return self.request(method, f"{YOUTUBE_API}{path}", params, json_body)

    # broadcasts
    def list_broadcasts(self, status):
        body = self.api("GET", "/liveBroadcasts", {
            "part": "snippet,status",
            "broadcastStatus": status,
            "broadcastType": "all",
            "maxResults": 10,
        })
        return body.get("items", [])

    def find_target(self):
        """The broadcast a new title should go to: live first, else the next upcoming one."""
        active = self.list_broadcasts("active")
        if active:
            return active[0]
        upcoming = self.list_broadcasts("upcoming")
        upcoming.sort(key=lambda b: b["snippet"].get("scheduledStartTime") or "")
        return upcoming[0] if upcoming else None

    def diagnostics(self):
        def short(b):
            s, st = b["snippet"], b.get("status", {})
            return {
                "id": b["id"],
                "title": s.get("title"),
                "lifeCycleStatus": st.get("lifeCycleStatus"),
                "privacyStatus": st.get("privacyStatus"),
                "scheduledStartTime": s.get("scheduledStartTime"),
                "actualStartTime": s.get("actualStartTime"),
            }
        return {
            "streaming_in_obs": _streaming.is_set(),
            "active": [short(b) for b in self.list_broadcasts("active")],
            "upcoming": [short(b) for b in self.list_broadcasts("upcoming")],
            "recent": [short(b) for b in self.list_broadcasts("all")[:5]],
        }

    def _apply_to(self, broadcast, title, description):
        """Set title (and optionally description) on one broadcast."""
        s = broadcast["snippet"]
        # liveBroadcasts.update requires scheduledStartTime. Broadcasts that
        # YouTube creates on its own when you stream with a stream key from
        # YouTube Studio don't have one, so those go straight to videos.update.
        if s.get("scheduledStartTime"):
            snippet = {
                "title": title,
                # Omitted fields get cleared by YouTube, so always send the description.
                "description": s.get("description", "") if description is None else description,
                "scheduledStartTime": s["scheduledStartTime"],
            }
            if s.get("scheduledEndTime"):
                snippet["scheduledEndTime"] = s["scheduledEndTime"]
            try:
                # 1 quota unit
                self.api("PUT", "/liveBroadcasts", {"part": "snippet"},
                         json_body={"id": broadcast["id"], "snippet": snippet})
                return "liveBroadcasts.update"
            except ApiError as e:
                if e.status != 400:
                    raise
                _log(obs.LOG_INFO, f"liveBroadcasts.update refused ({e.message}), trying videos.update")
        # Fallback, 50 quota units: the broadcast is also a video.
        items = self.api("GET", "/videos", {"part": "snippet", "id": broadcast["id"]}).get("items", [])
        if not items:
            raise ApiError(404, "YouTube: video for the broadcast not found")
        vs = items[0]["snippet"]
        vsnip = {
            "title": title,
            "description": vs.get("description", "") if description is None else description,
            "categoryId": vs.get("categoryId") or YOUTUBE_GAMING_CATEGORY,
        }
        for key in ("tags", "defaultLanguage", "defaultAudioLanguage"):
            if vs.get(key):
                vsnip[key] = vs[key]
        self.api("PUT", "/videos", {"part": "snippet"}, json_body={"id": broadcast["id"], "snippet": vsnip})
        return "videos.update"

    def update(self, title, description=None):
        title = (title or "").strip()
        if not title:
            raise ApiError(400, "Title must not be empty")
        if len(title) > YOUTUBE_TITLE_MAX:
            raise ApiError(400, f"YouTube titles are limited to {YOUTUBE_TITLE_MAX} characters")
        if "<" in title or ">" in title:
            raise ApiError(400, "YouTube titles must not contain < or >")
        if description is not None and len(description) > YOUTUBE_DESCRIPTION_MAX:
            raise ApiError(400, f"Description is longer than {YOUTUBE_DESCRIPTION_MAX} characters")

        target = self.find_target()
        if target is None:
            pending = {"title": title, "description": description, "since": time.time()}
            with self.lock:
                self.pending = pending
                self.last_result = None
                _write_json(YOUTUBE_PENDING_PATH, pending)
            self.ensure_watcher()
            return {"applied": False, "pending": True}

        method = self._apply_to(target, title, description)
        with self.lock:
            self.pending = None
            _delete(YOUTUBE_PENDING_PATH)
        _log(obs.LOG_INFO, f"YouTube title set via {method} on {target['id']}")
        return {"applied": True, "broadcast_id": target["id"],
                "status": target.get("status", {}).get("lifeCycleStatus")}

    def clear_pending(self):
        with self.lock:
            self.pending = None
            _delete(YOUTUBE_PENDING_PATH)

    # pending title watcher
    def ensure_watcher(self):
        with self.lock:
            if self.pending and (self._watcher is None or not self._watcher.is_alive()):
                self._watcher = threading.Thread(target=self._watch, name="obs-stream-dock-youtube-watch", daemon=True)
                self._watcher.start()

    def _watch(self):
        """While a title is pending and OBS streams, look for the live broadcast."""
        while not _stop.wait(YOUTUBE_PENDING_POLL_S):
            with self.lock:
                pending = dict(self.pending) if self.pending else None
                logged_in = bool(self.tokens)
            if not pending or not logged_in:
                return
            if time.time() - pending["since"] > YOUTUBE_PENDING_MAX_AGE_S:
                self.clear_pending()
                _log(obs.LOG_INFO, "Pending YouTube title dropped after 12 h")
                return
            if not _streaming.is_set():
                continue  # nothing can be live yet, don't spend quota
            try:
                active = self.list_broadcasts("active")
                if not active:
                    continue
                self._apply_to(active[0], pending["title"], pending.get("description"))
            except ApiError as e:
                _log(obs.LOG_WARNING, f"Applying pending YouTube title failed: {e.message}")
                with self.lock:
                    self.last_result = {"ok": False, "error": e.message, "at": time.time()}
                continue
            with self.lock:
                if self.pending and self.pending.get("since") == pending["since"]:
                    self.pending = None
                    _delete(YOUTUBE_PENDING_PATH)
                self.last_result = {"ok": True, "title": pending["title"], "at": time.time()}
            _log(obs.LOG_INFO, f"Pending YouTube title applied to {active[0]['id']}")
            return


# --- YouTube chat ---------------------------------------------------------------

def _python_exe():
    """The real python.exe. Inside OBS, sys.executable is obs64.exe."""
    for base in (sys.base_prefix, sys.exec_prefix, sys.prefix):
        exe = os.path.join(base or "", "python.exe")
        if base and os.path.isfile(exe):
            return exe
    return None


_YT_KINDS = {
    "textmessageevent": "text",
    "superchatevent": "superchat",
    "superstickerevent": "supersticker",
    "newsponsorevent": "member",
    "membermilestonechatevent": "member",
    "membershipgiftingevent": "gift",
    "giftmembershipreceivedevent": "gift",
    "giftevent": "gift",
    "tombstone": "delete",
    "messagedeletedevent": "delete",
    "userbannedevent": "ban",
    "chatendedevent": "system",
}


def _normalize_yt(item):
    """streamList item (gRPC, camelCase) -> message dict for the dock."""
    s = item.get("snippet", {})
    a = item.get("authorDetails", {})
    kind = _YT_KINDS.get((s.get("type") or "").replace("_", "").lower())
    if kind is None:
        return None  # polls etc. are not shown
    msg = {
        "platform": "youtube",
        "id": item.get("id"),
        "kind": kind,
        "ts": s.get("publishedAt"),
        "author": a.get("displayName") or "",
        "author_id": a.get("channelId") or s.get("authorChannelId"),
        "owner": bool(a.get("isChatOwner")),
        "mod": bool(a.get("isChatModerator")),
        "member": bool(a.get("isChatSponsor")),
        "verified": bool(a.get("isVerified")),
        "text": (s.get("textMessageDetails") or {}).get("messageText") or s.get("displayMessage") or "",
    }
    if kind == "superchat":
        d = s.get("superChatDetails") or {}
        msg["amount"] = d.get("amountDisplayString")
        msg["text"] = d.get("userComment") or ""
    elif kind == "supersticker":
        msg["amount"] = (s.get("superStickerDetails") or {}).get("amountDisplayString")
    elif kind == "delete":
        msg["target"] = (s.get("messageDeletedDetails") or {}).get("deletedMessageId") or item.get("id")
    elif kind == "ban":
        msg["target_author_id"] = ((s.get("userBannedDetails") or {}).get("bannedUserDetails") or {}).get("channelId")
    return msg


class YouTubeChat:
    """Keeps the helper process running while there is a live chat to read.

    States: idle, searching (looking for the live broadcast), connected,
    retrying, ended, error.
    """

    def __init__(self, yt):
        self.yt = yt
        self.lock = threading.Lock()
        self.messages = deque(maxlen=CHAT_BUFFER)  # (seq, msg)
        self.seen = deque(maxlen=2000)
        self.seq = 0
        self.state = {"state": "idle"}
        self.live_chat_id = None
        self.page_token = None
        self.ended_chats = set()  # chats YouTube reported as offline, never reconnect to them
        self.want = False
        self.thread = None
        self.proc = None

    def public_state(self):
        with self.lock:
            return dict(self.state)

    def _set(self, state, **extra):
        with self.lock:
            self.state = {"state": state, "at": time.time(), **extra}

    def since(self, seq):
        with self.lock:
            return self.seq, [m for s, m in self.messages if s > seq]

    def _add(self, items):
        with self.lock:
            for it in items:
                msg = _normalize_yt(it)
                if msg is None:
                    continue
                key = (msg["kind"], msg["id"])
                if key in self.seen:
                    continue
                self.seen.append(key)
                self.seq += 1
                self.messages.append((self.seq, msg))

    def start(self):
        with self.lock:
            self.want = True
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._run, name="obs-stream-dock-ytchat", daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            self.want = False
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _wait(self, seconds):
        """Sleep, but wake up early on unload or stop. Returns False if we should quit."""
        end = time.time() + seconds
        while time.time() < end:
            if _stop.wait(min(1.0, end - time.time())) or not self.want:
                return False
        return True

    def _find_chat(self):
        started = time.time()
        while self.want and not _stop.is_set():
            self._set("searching")
            try:
                active = self.yt.list_broadcasts("active")
            except ApiError as e:
                self._set("error", error=e.message)
                return None
            for b in active:
                chat_id = b["snippet"].get("liveChatId")
                if chat_id and chat_id not in self.ended_chats:
                    return chat_id
            # Nothing live. Keep looking only while OBS streams (the YouTube
            # output may start a little later), never forever.
            if not _streaming.is_set() or time.time() - started > CHAT_FIND_MAX_S:
                self._set("idle", note="No live YouTube broadcast")
                return None
            if not self._wait(CHAT_FIND_POLL_S):
                return None
        return None

    def _run(self):
        failures = 0
        try:
            while self.want and not _stop.is_set():
                if not self.yt.tokens:
                    self._set("error", error="Not logged in to YouTube")
                    return
                python = _python_exe()
                if python is None:
                    self._set("error", error="python.exe not found next to the OBS Python runtime")
                    return
                if not self.live_chat_id:
                    self.live_chat_id = self._find_chat()
                    self.page_token = None
                    if not self.live_chat_id:
                        return
                try:
                    token = self.yt.access_token()
                except ApiError as e:
                    self._set("error", error=e.message)
                    return
                result = self._run_helper(python, token)
                if not self.want or _stop.is_set():
                    return
                kind, code = result.get("type"), result.get("code")
                if result.get("offline"):
                    self._set("ended", note="The broadcast has ended")
                    self.ended_chats.add(self.live_chat_id)
                    self.live_chat_id = None
                    if not _streaming.is_set():
                        return
                    continue  # OBS still streams: look for the next broadcast
                if kind == "error" and code == "UNAUTHENTICATED":
                    try:
                        self.yt.access_token(force_refresh=True)
                    except ApiError as e:
                        self._set("error", error=e.message)
                        return
                    continue
                if kind == "error" and code in ("NOT_FOUND", "FAILED_PRECONDITION", "PERMISSION_DENIED", "INVALID_ARGUMENT"):
                    # Chat is gone or not accessible: look for the broadcast again.
                    self.live_chat_id = None
                failures = failures + 1 if kind != "page-end" else 0
                wait = min(CHAT_RETRY_MAX_S, 10 * (2 ** min(failures, 5)))
                self._set("retrying", error=result.get("message") or code or "connection closed", retry_in=wait)
                _log(obs.LOG_INFO, f"YouTube chat: {code or kind}, reconnecting in {wait} s")
                if not self._wait(wait):
                    return
        finally:
            with self.lock:
                self.proc = None
                if self.state.get("state") in ("connected", "searching", "retrying"):
                    self.state = {"state": "idle", "at": time.time()}

    def _run_helper(self, python, token):
        """Run one helper process until it exits. Returns the last event."""
        try:
            proc = subprocess.Popen(
                [python, "-E", "-s", CHAT_HELPER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return {"type": "error", "code": "SPAWN", "message": str(e)}
        with self.lock:
            self.proc = proc
        last = {"type": "end"}
        got_page = False
        try:
            proc.stdin.write(json.dumps({"token": token, "live_chat_id": self.live_chat_id,
                                         "page_token": self.page_token}) + "\n")
            proc.stdin.flush()  # keep stdin open: closing it tells the helper to quit
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    _log(obs.LOG_WARNING, f"YouTube chat helper: {line.rstrip()[:300]}")
                    continue
                if ev.get("type") == "page":
                    if not got_page:
                        got_page = True
                        self._set("connected")
                    if ev.get("nextPageToken"):
                        self.page_token = ev["nextPageToken"]
                    self._add(ev.get("items") or [])
                    if ev.get("offlineAt"):
                        last = {"type": "end", "offline": True}
                        break
                else:
                    last = ev
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=5)
        if got_page and last.get("type") == "end" and not last.get("offline"):
            last = {"type": "page-end"}  # stream closed normally after working: reconnect soon
        return last


def _youtube_send_chat(text):
    chat_id = _ytchat.live_chat_id if _ytchat else None
    if not chat_id:
        for b in _youtube.list_broadcasts("active"):
            chat_id = b["snippet"].get("liveChatId")
            if chat_id:
                break
    if not chat_id:
        raise ApiError(409, "YouTube: no live broadcast with chat right now")
    body = _youtube.api("POST", "/liveChat/messages", {"part": "snippet"}, json_body={
        "snippet": {"liveChatId": chat_id, "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text}}})
    return (body or {}).get("id")


# --- emotes (Twitch, 7TV, BTTV, FFZ) ----------------------------------------------
#
# Loaded in a background thread so a slow service never holds up the dock: after
# EMOTE_FIRST_WAIT_S the dock gets what is there (loading=True) and asks again
# shortly, slow services (FrankerFaceZ sometimes takes 20 s+) follow.

EMOTE_FIRST_WAIT_S = 4


def _emote_get(url, failed):
    # One retry: a single hiccup must not leave a whole emote service out.
    for attempt in (1, 2):
        try:
            status, body = _http("GET", url, timeout=12)
        except ApiError as e:
            status, body = 0, e.message
        if status == 200:
            return body
        if status == 404:  # the channel simply has no emotes on that service
            return None
        if attempt == 2:
            failed.append(url.split("/")[2])
            _log(obs.LOG_WARNING, f"Emotes: {url} failed ({status or body})")
    return None


def _emote_urls(twitch_id):
    urls = {
        "ffz_global": "https://api.frankerfacez.com/v1/set/global",
        "bttv_global": "https://api.betterttv.net/3/cached/emotes/global",
        "7tv_global": "https://7tv.io/v3/emote-sets/global",
    }
    if twitch_id:
        urls.update({
            "ffz_channel": f"https://api.frankerfacez.com/v1/room/id/{twitch_id}",
            "bttv_channel": f"https://api.betterttv.net/3/cached/users/twitch/{twitch_id}",
            "7tv_channel": f"https://7tv.io/v3/users/twitch/{twitch_id}",
        })
    return urls


def _build_emotes(results):
    """Picker sets and the render map from whatever has arrived so far.

    Returns {"emotes": name -> URL for 7TV/BTTV/FFZ in messages,
             "sets": ordered picker groups, "twitch_note": str|None}."""
    def from_7tv(emotes):
        out = []
        for e in emotes or []:
            host = ((e.get("data") or {}).get("host") or {}).get("url")
            if e.get("name") and host:
                out.append({"name": e["name"],
                            "url": f"https:{host}/1x.webp" if host.startswith("//") else f"{host}/1x.webp"})
        return out

    def from_bttv(emotes):
        return [{"name": e["code"], "url": f"https://cdn.betterttv.net/emote/{e['id']}/1x"}
                for e in emotes or [] if e.get("code") and e.get("id")]

    def from_ffz(emoticons):
        return [{"name": e["name"], "url": e["urls"]["1"]}
                for e in emoticons or [] if e.get("name") and (e.get("urls") or {}).get("1")]

    ffz_global = []
    g = results.get("ffz_global") or {}
    for sid in g.get("default_sets", []):
        ffz_global += from_ffz((g.get("sets", {}).get(str(sid)) or {}).get("emoticons"))
    f = results.get("ffz_channel") or {}
    ffz_channel = from_ffz((f.get("sets", {}).get(str(f["room"].get("set"))) or {}).get("emoticons")) \
        if f.get("room") else []
    b = results.get("bttv_channel") or {}

    def mk(key, label, scope, emotes):
        return {"key": key, "label": label, "source": key.split("-")[0], "scope": scope, "emotes": emotes}

    channel = [
        mk("7tv-channel", "7TV · channel", "channel",
           from_7tv(((results.get("7tv_channel") or {}).get("emote_set") or {}).get("emotes"))),
        mk("bttv-channel", "BTTV · channel", "channel",
           from_bttv(b.get("channelEmotes")) + from_bttv(b.get("sharedEmotes"))),
        mk("ffz-channel", "FFZ · channel", "channel", ffz_channel),
    ]
    glob = [
        mk("7tv-global", "7TV · global", "global", from_7tv((results.get("7tv_global") or {}).get("emotes"))),
        mk("bttv-global", "BTTV · global", "global", from_bttv(results.get("bttv_global"))),
        mk("ffz-global", "FFZ · global", "global", ffz_global),
    ]
    tw = results.get("twitch") or {}
    tw_sets = tw.get("sets") or []

    def tw_scope(scope):
        return [s for s in tw_sets if s["scope"] == scope]

    sets = [s for s in tw_scope("channel") + channel + tw_scope("other") + tw_scope("global") + glob
            if s["emotes"]]
    # Messages only get third-party emotes as images (Twitch's own come with
    # positions in the IRC tags). Channel emotes override global ones.
    render = {}
    for s in glob + channel:
        for e in s["emotes"]:
            render[e["name"]] = e["url"]
    return {"emotes": render, "sets": sets, "twitch_note": tw.get("note")}


def _publish_emotes(gen, data, *, final, complete=False):
    with _emotes["lock"]:
        if _emotes["gen"] != gen:
            return  # a newer load (other login) took over
        _emotes["data"] = data
        if final:
            _emotes["loading"] = False
            _emotes["complete"] = complete
            _emotes["at"] = time.time()


def _load_emotes(ident, gen):
    failed, results = [], {}
    try:
        jobs = {k: (lambda u=u: _emote_get(u, failed))
                for k, u in _emote_urls(ident["user_id"] if ident else None).items()}
        if ident and _twitch:
            def twitch_job():
                try:
                    return {"sets": _twitch.user_emotes()}
                except ApiError as e:
                    if e.status != 403:  # 403 = login predates the emote permission
                        _log(obs.LOG_WARNING, f"Emotes: {e.message}")
                    return {"note": e.message, "retry": e.status != 403}
            jobs["twitch"] = twitch_job
        threads = [threading.Thread(target=lambda k=k, fn=fn: results.__setitem__(k, fn()),
                                    name=f"obs-stream-dock-emotes-{k}", daemon=True)
                   for k, fn in jobs.items()]
        for t in threads:
            t.start()
        deadline = time.time() + EMOTE_FIRST_WAIT_S
        for t in threads:
            t.join(max(0.0, deadline - time.time()))
        if any(t.is_alive() for t in threads):
            _publish_emotes(gen, _build_emotes(dict(results)), final=False)
            for t in threads:
                t.join(60)
        complete = (not failed and not (results.get("twitch") or {}).get("retry")
                    and not any(t.is_alive() for t in threads))
        _publish_emotes(gen, _build_emotes(dict(results)), final=True, complete=complete)
    finally:
        with _emotes["lock"]:
            if _emotes["gen"] == gen and _emotes["loading"]:  # crashed before publishing
                _emotes["loading"], _emotes["complete"], _emotes["at"] = False, False, time.time()


def _get_emotes():
    """Never blocks on the emote services; starts a background load when needed."""
    with _emotes["lock"]:
        ident = _twitch.chat_identity() if _twitch else None
        # A new login (other account, new permission) must not wait for the cache.
        key = ((ident or {}).get("user_id"), tuple(_twitch.missing_scopes()) if _twitch else ())
        # Complete lists are kept for 30 min, incomplete ones only for 1 min.
        max_age = EMOTE_CACHE_S if _emotes["complete"] else 60
        new_key = _emotes["key"] != key
        stale = _emotes["data"] is None or time.time() - _emotes["at"] > max_age
        if new_key or (stale and not _emotes["loading"]):
            _emotes["gen"] += 1
            _emotes["key"] = key
            _emotes["loading"] = True
            if new_key:
                _emotes["data"] = None  # don't show the previous login's emotes
            threading.Thread(target=_load_emotes, args=(ident, _emotes["gen"]),
                             name="obs-stream-dock-emotes", daemon=True).start()
        data = _emotes["data"] or {"emotes": {}, "sets": [], "twitch_note": None}
        return {**data, "loading": _emotes["loading"],
                "complete": _emotes["complete"] and not _emotes["loading"]}


def _send_chat(text, targets):
    text = (text or "").strip()
    if not text:
        raise ApiError(400, "Message is empty")
    results = {}
    for target in targets:
        try:
            if target == "twitch":
                if len(text) > TWITCH_CHAT_MAX:
                    raise ApiError(400, f"Twitch allows {TWITCH_CHAT_MAX} characters")
                _twitch.send_chat(text)
            elif target == "youtube":
                if len(text) > YOUTUBE_CHAT_MAX:
                    raise ApiError(400, f"YouTube allows {YOUTUBE_CHAT_MAX} characters")
                _youtube_send_chat(text)
            else:
                continue
            results[target] = {"ok": True}
        except ApiError as e:
            results[target] = {"ok": False, "error": e.message}
    return results


# --- HTTP server ----------------------------------------------------------------

class _ExclusiveServer(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR by default. On Windows that lets a
    # second instance bind the same port silently, so we turn it off and
    # ask for exclusive use instead. A busy port then fails loudly.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


_CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class _Handler(SimpleHTTPRequestHandler):
    def handle(self):
        # The dock went away mid-request (reloaded, closed, OBS shutting down).
        # Nobody is left to answer, and a traceback in the OBS log would only alarm.
        try:
            super().handle()
        except _CLIENT_GONE:
            self.close_connection = True

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        # Blocks DNS rebinding: only accept requests addressed to us by name.
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _read_json_body(self):
        # Requiring application/json forces a CORS preflight for cross-site
        # requests, which we never answer, so other websites can't POST here.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ApiError(415, "Expected application/json")
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise ApiError(403, "Forbidden origin")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise ApiError(400, "Invalid JSON")

    def _api(self, method):
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        tw, yt = _twitch, _youtube
        accounts = {"twitch": tw, "youtube": yt}
        try:
            if method == "GET":
                if path == "/api/status":
                    return self._send_json(200, {
                        "ok": True,
                        "pid": os.getpid(),
                        "uptime_s": round(time.time() - _started_at, 1),
                        "streaming": _streaming.is_set(),
                        "twitch": tw.public_state(),
                        "youtube": yt.public_state(),
                        "youtube_chat": _ytchat.public_state(),
                    })
                if path == "/api/chat/config":
                    return self._send_json(200, {
                        "ok": True,
                        "twitch": tw.chat_identity(),
                        "youtube": {"logged_in": bool(yt.tokens)},
                        "limits": {"twitch": TWITCH_CHAT_MAX, "youtube": YOUTUBE_CHAT_MAX},
                    })
                if path == "/api/chat/emotes":
                    return self._send_json(200, {"ok": True, **_get_emotes()})
                if path == "/api/chat/youtube":
                    since = int((query.get("since") or ["0"])[0] or 0)
                    seq, msgs = _ytchat.since(since)
                    return self._send_json(200, {"ok": True, "seq": seq, "messages": msgs,
                                                 "state": _ytchat.public_state()})
                if path == "/api/twitch/channel":
                    return self._send_json(200, {"ok": True, **tw.get_channel()})
                if path == "/api/twitch/categories":
                    q = (query.get("q") or [""])[0]
                    return self._send_json(200, {"ok": True, "data": tw.search_categories(q)})
                if path == "/api/youtube/diagnostics":
                    return self._send_json(200, {"ok": True, **yt.diagnostics()})
            if method == "POST":
                body = self._read_json_body()
                parts = path.strip("/").split("/")  # api/<platform>/<action>
                if len(parts) == 3 and parts[1] in accounts and parts[2] in ("login", "logout"):
                    acc = accounts[parts[1]]
                    if parts[2] == "login":
                        return self._send_json(200, {"ok": True, "login": acc.start_login()})
                    acc.logout()
                    return self._send_json(200, {"ok": True})
                if path == "/api/twitch/channel":
                    ch = tw.update_channel(body.get("title"), body.get("game_id"))
                    return self._send_json(200, {"ok": True, **ch})
                if path == "/api/youtube/broadcast":
                    return self._send_json(200, {"ok": True, **yt.update(body.get("title"), body.get("description"))})
                if path == "/api/youtube/pending/clear":
                    yt.clear_pending()
                    return self._send_json(200, {"ok": True})
                if path == "/api/chat/youtube/connect":
                    if not yt.tokens:
                        raise ApiError(401, "Not logged in to YouTube")
                    _ytchat.start()
                    return self._send_json(200, {"ok": True})
                if path == "/api/chat/youtube/disconnect":
                    _ytchat.stop()
                    return self._send_json(200, {"ok": True})
                if path == "/api/chat/send":
                    targets = [t for t in (body.get("targets") or []) if t in ("twitch", "youtube")]
                    if not targets:
                        raise ApiError(400, "No platform selected")
                    return self._send_json(200, {"ok": True, "results": _send_chat(body.get("text"), targets)})
            return self._send_json(404, {"ok": False, "error": "Not found"})
        except ApiError as e:
            status = e.status if 400 <= e.status < 600 else 502
            return self._send_json(status, {"ok": False, "error": e.message})
        except _CLIENT_GONE:
            raise  # not a bug, handle() drops the connection quietly
        except Exception as e:  # never let a bug kill the handler silently
            _log(obs.LOG_ERROR, f"{method} {path} failed: {e!r}")
            return self._send_json(500, {"ok": False, "error": f"Internal error: {e}"})

    def do_GET(self):
        if not self._host_ok():
            return self._send_json(403, {"ok": False, "error": "Forbidden host"})
        if self.path.startswith("/api/"):
            return self._api("GET")
        super().do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._send_json(403, {"ok": False, "error": "Forbidden host"})
        if self.path.startswith("/api/"):
            return self._api("POST")
        self._send_json(404, {"ok": False, "error": "Not found"})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # keep the OBS script log quiet


def _start_server():
    global _server, _thread, _started_at
    handler = partial(_Handler, directory=WEB_DIR)
    try:
        _server = _ExclusiveServer((HOST, PORT), handler)
    except OSError as e:
        _server = None
        _log(obs.LOG_ERROR, f"Could not bind {HOST}:{PORT}: {e}")
        return
    _started_at = time.time()
    _thread = threading.Thread(
        target=_server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="obs-stream-dock-http",
        daemon=True,
    )
    _thread.start()
    _log(obs.LOG_INFO, f"Server running on http://{HOST}:{PORT}/")


def _stop_server():
    global _server, _thread
    if _server is None:
        return
    t0 = time.perf_counter()
    _server.shutdown()  # blocks until serve_forever has returned
    _server.server_close()
    alive = False
    if _thread is not None:
        _thread.join(timeout=2.0)
        alive = _thread.is_alive()
    ms = (time.perf_counter() - t0) * 1000
    _log(obs.LOG_WARNING if alive else obs.LOG_INFO, f"Server stopped in {ms:.0f} ms (thread alive: {alive})")
    _server = None
    _thread = None


# --- OBS script callbacks ---------------------------------------------------------

def _on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED:
        _streaming.set()
        if _youtube is not None:
            _youtube.ensure_watcher()
            if _youtube.tokens and _ytchat is not None:
                _ytchat.start()  # reads the chat as soon as the broadcast is live
    elif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED:
        _streaming.clear()


def script_description():
    return (
        "<b>OBS Stream Dock</b><br>"
        "Title, category and a combined chat for Twitch and YouTube.<br>"
        f"Docks → Custom Browser Docks: <code>http://{HOST}:{PORT}/</code> (stream info) "
        f"and <code>http://{HOST}:{PORT}/chat.html</code> (chat)."
    )


def script_load(settings):
    global _twitch, _youtube, _ytchat
    _stop.clear()
    cfg = _read_json(CONFIG_PATH) or {}
    twitch_id = (cfg.get("twitch_client_id") or "").strip()
    google_id = (cfg.get("google_client_id") or "").strip()
    google_secret = (cfg.get("google_client_secret") or "").strip()
    if not twitch_id:
        _log(obs.LOG_WARNING, "No twitch_client_id in config.local.json, Twitch is disabled")
    if not (google_id and google_secret):
        _log(obs.LOG_WARNING, "No google_client_id/secret in config.local.json, YouTube is disabled")
    _twitch = Twitch(twitch_id)
    _youtube = YouTube(google_id, google_secret)
    _ytchat = YouTubeChat(_youtube)

    if obs.obs_frontend_streaming_active():
        _streaming.set()
    else:
        _streaming.clear()
    obs.obs_frontend_add_event_callback(_on_frontend_event)
    _youtube.ensure_watcher()  # resume a title left pending before a reload
    if _streaming.is_set() and _youtube.tokens:
        _ytchat.start()
    _start_server()


def script_unload():
    _stop.set()
    if _ytchat is not None:
        _ytchat.stop()  # kills the helper process
    _stop_server()
