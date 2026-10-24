"""
OBS Stream Info

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
"""

import json
import os
import socket
import threading
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

_server = None
_thread = None
_started_at = None
_stop = threading.Event()  # tells background workers to quit on unload
_streaming = threading.Event()  # mirrors the OBS main stream state
_twitch = None
_youtube = None


def _log(level, msg):
    # OBS only knows the current script inside its own callbacks. Messages
    # from our worker threads would show up as "[Unknown Script]", so tag them.
    if threading.current_thread() is not threading.main_thread():
        msg = f"[obs_stream_info.py] {msg}"
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

    # state for the UI
    def public_state(self):
        with self.lock:
            t = self.tokens
            return {
                "configured": self.configured(),
                "logged_in": bool(t),
                "user": {"display_name": t.get("display_name")} if t else None,
                "login": dict(self.login),
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
            name=f"obs-stream-info-{self.label.lower()}-login",
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
    scopes = "channel:manage:broadcast"

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
                self._watcher = threading.Thread(target=self._watch, name="obs-stream-info-youtube-watch", daemon=True)
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


class _Handler(SimpleHTTPRequestHandler):
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
                    })
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
            return self._send_json(404, {"ok": False, "error": "Not found"})
        except ApiError as e:
            status = e.status if 400 <= e.status < 600 else 502
            return self._send_json(status, {"ok": False, "error": e.message})
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
        name="obs-stream-info-http",
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
    elif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED:
        _streaming.clear()


def script_description():
    return (
        "<b>OBS Stream Info</b><br>"
        "Set title and category for Twitch and YouTube from a dock.<br>"
        f"Add <code>http://{HOST}:{PORT}/</code> via Docks → Custom Browser Docks."
    )


def script_load(settings):
    global _twitch, _youtube
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

    if obs.obs_frontend_streaming_active():
        _streaming.set()
    else:
        _streaming.clear()
    obs.obs_frontend_add_event_callback(_on_frontend_event)
    _youtube.ensure_watcher()  # resume a title left pending before a reload
    _start_server()


def script_unload():
    _stop.set()
    _stop_server()
