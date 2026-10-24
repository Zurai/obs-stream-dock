"""
OBS Stream Info

An OBS Python script that serves a small web UI on 127.0.0.1 for use as a
Custom Browser Dock. From the dock you can set the stream title and category
on Twitch (YouTube follows later).

The HTTP server runs in a background thread inside the OBS process. It starts
when the script is loaded and shuts down cleanly when the script is unloaded
(script reload, OBS exit).

Local, never committed:
  config.local.json      {"twitch_client_id": "..."}
  data/twitch_tokens.json  access + refresh token
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

TWITCH_SCOPES = "channel:manage:broadcast"
TWITCH_DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
TWITCH_HELIX = "https://api.twitch.tv/helix"
TITLE_MAX_LEN = 140

_server = None
_thread = None
_started_at = None
_stop = threading.Event()  # tells background workers to quit on unload
_twitch = None


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
    if isinstance(body, dict):
        return body.get("message") or body.get("error") or fallback
    return fallback


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


# --- Twitch -------------------------------------------------------------------

class Twitch:
    """Device Code Flow login (public client, no secret) and Helix calls."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.lock = threading.RLock()
        self.tokens = _read_json(TWITCH_TOKEN_PATH)
        self.login = {"state": "idle"}
        self.last_validated = 0.0

    # state for the UI
    def public_state(self):
        with self.lock:
            t = self.tokens
            return {
                "configured": bool(self.client_id),
                "logged_in": bool(t),
                "user": {"login": t.get("login"), "display_name": t.get("display_name")} if t else None,
                "login": dict(self.login),
            }

    # login
    def start_login(self):
        if not self.client_id:
            raise ApiError(400, "No Twitch client ID configured (config.local.json)")
        with self.lock:
            if self.login.get("state") == "pending" and time.time() < self.login.get("expires_at", 0):
                return dict(self.login)
        status, body = _http("POST", TWITCH_DEVICE_URL, form={"client_id": self.client_id, "scopes": TWITCH_SCOPES})
        if status != 200:
            raise ApiError(status, _message(body))
        state = {
            "state": "pending",
            "user_code": body["user_code"],
            "verification_uri": body["verification_uri"],
            "expires_at": time.time() + int(body.get("expires_in", 1800)),
        }
        with self.lock:
            self.login = state
        threading.Thread(
            target=self._poll_login,
            args=(body["device_code"], max(int(body.get("interval", 5)), 1), state["expires_at"]),
            name="obs-stream-info-twitch-login",
            daemon=True,
        ).start()
        try:
            webbrowser.open(body["verification_uri"])
        except Exception:
            pass  # the dock shows the link and code as a fallback
        return dict(state)

    def _set_login_error(self, msg):
        with self.lock:
            self.login = {"state": "error", "error": msg}
        _log(obs.LOG_WARNING, f"Twitch login failed: {msg}")

    def _poll_login(self, device_code, interval, expires_at):
        form = {
            "client_id": self.client_id,
            "scopes": TWITCH_SCOPES,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        while not _stop.wait(interval):
            if time.time() > expires_at:
                return self._set_login_error("Code expired, please try again")
            try:
                status, body = _http("POST", TWITCH_TOKEN_URL, form=form)
            except ApiError:
                continue  # transient network issue, keep polling
            if status == 200:
                try:
                    tokens = self._tokens_from(body)
                    tokens.update(self._get_user(tokens["access_token"]))
                except ApiError as e:
                    return self._set_login_error(e.message)
                with self.lock:
                    self.tokens = tokens
                    self.last_validated = time.time()
                    _write_json(TWITCH_TOKEN_PATH, tokens)
                    self.login = {"state": "ok"}
                _log(obs.LOG_INFO, f"Twitch: logged in as {tokens.get('login')}")
                return
            msg = _message(body)
            if msg == "authorization_pending":
                continue
            if msg == "slow_down":
                interval += 5
                continue
            return self._set_login_error(msg)

    def logout(self):
        with self.lock:
            t = self.tokens
            self.tokens = None
            self.login = {"state": "idle"}
            _delete(TWITCH_TOKEN_PATH)
        if t:
            try:
                _http("POST", TWITCH_REVOKE_URL, form={"client_id": self.client_id, "token": t["access_token"]})
            except ApiError:
                pass

    # tokens
    @staticmethod
    def _tokens_from(body):
        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": time.time() + int(body.get("expires_in", 0)),
            "scope": body.get("scope", []),
        }

    def _get_user(self, access_token):
        status, body = _http(
            "GET", f"{TWITCH_HELIX}/users",
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id},
        )
        if status != 200 or not body or not body.get("data"):
            raise ApiError(status, _message(body, "Could not read Twitch user"))
        u = body["data"][0]
        return {"user_id": u["id"], "login": u["login"], "display_name": u["display_name"]}

    def _drop_session(self, reason):
        self.tokens = None
        _delete(TWITCH_TOKEN_PATH)
        _log(obs.LOG_WARNING, f"Twitch session dropped: {reason}")
        raise ApiError(401, "Twitch login expired, please log in again")

    def _refresh(self):
        # caller holds self.lock. Refresh tokens are single use, so the lock
        # also makes sure two requests never spend the same one.
        status, body = _http("POST", TWITCH_TOKEN_URL, form={
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
        })
        if status in (400, 401):
            self._drop_session(_message(body))
        if status != 200:
            raise ApiError(status, _message(body))
        self.tokens.update(self._tokens_from(body))
        self.last_validated = time.time()
        _write_json(TWITCH_TOKEN_PATH, self.tokens)

    def _session(self, force_refresh=False):
        with self.lock:
            if not self.tokens:
                raise ApiError(401, "Not logged in to Twitch")
            now = time.time()
            if force_refresh or now > self.tokens.get("expires_at", 0) - 60:
                self._refresh()
            elif now - self.last_validated > 3600:
                # Twitch asks apps to validate tokens at least hourly.
                status, _ = _http("GET", TWITCH_VALIDATE_URL,
                                  headers={"Authorization": f"OAuth {self.tokens['access_token']}"})
                if status == 401:
                    self._refresh()
                self.last_validated = time.time()
            return self.tokens["access_token"], self.tokens["user_id"]

    def helix(self, method, path, params=None, json_body=None):
        for attempt in (0, 1):
            token, _ = self._session(force_refresh=(attempt == 1))
            url = f"{TWITCH_HELIX}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            status, body = _http(method, url, json_body=json_body, headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": self.client_id,
            })
            if status == 401 and attempt == 0:
                continue
            if status >= 400:
                raise ApiError(status, f"Twitch: {_message(body)}")
            return body

    # API used by the dock
    def get_channel(self):
        _, uid = self._session()
        body = self.helix("GET", "/channels", {"broadcaster_id": uid})
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
            if len(title) > TITLE_MAX_LEN:
                raise ApiError(400, f"Title is longer than {TITLE_MAX_LEN} characters")
            payload["title"] = title
        if game_id:
            payload["game_id"] = str(game_id)
        if not payload:
            raise ApiError(400, "Nothing to update")
        _, uid = self._session()
        self.helix("PATCH", "/channels", {"broadcaster_id": uid}, json_body=payload)
        return self.get_channel()


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
        tw = _twitch
        try:
            if method == "GET" and path == "/api/status":
                return self._send_json(200, {
                    "ok": True,
                    "pid": os.getpid(),
                    "uptime_s": round(time.time() - _started_at, 1),
                    "twitch": tw.public_state(),
                })
            if method == "GET" and path == "/api/twitch/channel":
                return self._send_json(200, {"ok": True, **tw.get_channel()})
            if method == "GET" and path == "/api/twitch/categories":
                q = (query.get("q") or [""])[0]
                return self._send_json(200, {"ok": True, "data": tw.search_categories(q)})
            if method == "POST":
                body = self._read_json_body()
                if path == "/api/twitch/login":
                    return self._send_json(200, {"ok": True, "login": tw.start_login()})
                if path == "/api/twitch/logout":
                    tw.logout()
                    return self._send_json(200, {"ok": True})
                if path == "/api/twitch/channel":
                    ch = tw.update_channel(body.get("title"), body.get("game_id"))
                    return self._send_json(200, {"ok": True, **ch})
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

def script_description():
    return (
        "<b>OBS Stream Info</b><br>"
        "Set title and category for Twitch from a dock.<br>"
        f"Add <code>http://{HOST}:{PORT}/</code> via Docks → Custom Browser Docks."
    )


def script_load(settings):
    global _twitch
    _stop.clear()
    cfg = _read_json(CONFIG_PATH) or {}
    client_id = (cfg.get("twitch_client_id") or "").strip()
    if not client_id:
        _log(obs.LOG_WARNING, "No twitch_client_id in config.local.json, Twitch is disabled")
    _twitch = Twitch(client_id)
    _start_server()


def script_unload():
    _stop.set()
    _stop_server()
