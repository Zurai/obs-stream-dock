"""
OBS Stream Info - Phase 1 feasibility test.

Starts a small HTTP server on 127.0.0.1 inside the OBS process when the
script is loaded and shuts it down cleanly when the script is unloaded
(script reload, OBS exit). The page in ./web is meant to be added as a
Custom Browser Dock: http://127.0.0.1:47823/
"""

import json
import os
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import obspython as obs

HOST = "127.0.0.1"
PORT = 47823
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_server = None
_thread = None
_started_at = None
_load_count = 0


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
    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps(
                {
                    "ok": True,
                    "pid": os.getpid(),
                    "uptime_s": round(time.time() - _started_at, 1),
                    "load_count": _load_count,
                    "threads": threading.active_count(),
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Keep the OBS script log quiet; status polling would flood it.
        pass


def _start_server():
    global _server, _thread, _started_at
    handler = partial(_Handler, directory=WEB_DIR)
    try:
        _server = _ExclusiveServer((HOST, PORT), handler)
    except OSError as e:
        _server = None
        obs.script_log(obs.LOG_ERROR, f"Could not bind {HOST}:{PORT}: {e}")
        return
    _started_at = time.time()
    _thread = threading.Thread(
        target=_server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="obs-stream-info-http",
        daemon=True,
    )
    _thread.start()
    obs.script_log(obs.LOG_INFO, f"Server running on http://{HOST}:{PORT}/ (load #{_load_count})")


def _stop_server():
    global _server, _thread
    if _server is None:
        return
    t0 = time.perf_counter()
    _server.shutdown()  # blocks until serve_forever has returned
    _server.server_close()
    if _thread is not None:
        _thread.join(timeout=2.0)
        alive = _thread.is_alive()
    else:
        alive = False
    ms = (time.perf_counter() - t0) * 1000
    level = obs.LOG_WARNING if alive else obs.LOG_INFO
    obs.script_log(level, f"Server stopped in {ms:.0f} ms (thread alive: {alive})")
    _server = None
    _thread = None


# --- OBS script callbacks ---------------------------------------------------

def script_description():
    return (
        "<b>OBS Stream Info</b> (Phase 1 test)<br>"
        f"Serves the dock at <code>http://{HOST}:{PORT}/</code>.<br>"
        "Add it via Docks → Custom Browser Docks."
    )


def script_load(settings):
    global _load_count
    _load_count += 1
    _start_server()


def script_unload():
    _stop_server()
