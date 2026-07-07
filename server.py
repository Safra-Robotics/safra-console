"""Local HTTP + SSE backend for the Safra operator console.

Stdlib only (no pip installs required). Binds 127.0.0.1 —
the console is a local desktop app; the network hop to a real robot is the
TcpLink, not this server. Telemetry streams to the UI over SSE at 10 Hz;
commands arrive as small JSON POSTs (drive/jog intents are re-streamed by
the UI, mirroring the protocol's own streaming semantics).
"""

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import stores
import updater
import version
from links import SimLink, TcpLink

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
# dev checkout serves the repo's canonical assets; packaged installs carry a
# copy under ui/brand (build_release.py puts it there)
BRAND_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "assets"))
if not os.path.isdir(BRAND_DIR):
    BRAND_DIR = os.path.join(UI_DIR, "brand")

STATE_NAMES = ["BOOT", "HOMING", "READY", "MOVING", "FAULT"]
FAULT_NAMES = ["NONE", "E-STOP", "WATCHDOG", "LIMIT", "SERIAL", "BAD-CMD"]


class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}        # token -> operator dict
        self.link = None
        self.robot = None
        self.log = stores.EventLog()

    def operator(self, token):
        return self.sessions.get(token or "")

    def disconnect(self, operator=None):
        with self.lock:
            link, robot = self.link, self.robot
            self.link = self.robot = None
        if link:
            link.close()
            self.log.add("link", "disconnected from {}".format(robot["name"]),
                         operator=(operator or {}).get("name"),
                         robot=robot["name"])

    def connect(self, robot, operator):
        self.disconnect(operator)
        if robot["kind"] == "sim":
            link = SimLink()
        else:
            link = TcpLink(robot["host"], robot["port"])
            link.connect()  # raises OSError on failure
        with self.lock:
            self.link, self.robot = link, robot
        self.log.add("link", "connected to {}".format(robot["name"]),
                     operator=operator.get("name"), robot=robot["name"])


APP = App()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet the request log
        pass

    # --- helpers ---------------------------------------------------------------

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except ValueError:
            return {}

    def _auth(self):
        tok = self.headers.get("X-Session") or ""
        op = APP.operator(tok)
        if not op:
            self._json({"error": "not signed in"}, 401)
        return op

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        if path.startswith("/brand/"):
            root, rel = BRAND_DIR, path[len("/brand/"):]
        else:
            root, rel = UI_DIR, path.lstrip("/")
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root) or not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".woff2"):
            ctype = "font/woff2"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # --- SSE telemetry stream ----------------------------------------------------

    def _stream(self, q):
        tok = (q.get("token") or [""])[0]
        op = APP.operator(tok)
        if not op:
            self._json({"error": "not signed in"}, 401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last_evt = 0
        try:
            while True:
                link, robot = APP.link, APP.robot
                telem = link.snapshot() if link else None
                if link:
                    for msg in link.drain_events():
                        APP.log.add("robot", msg, operator=op["name"],
                                    robot=robot["name"] if robot else None)
                    if telem is not None:
                        telem["state_name"] = STATE_NAMES[telem.get("state", 0)]
                        telem["fault_name"] = FAULT_NAMES[telem.get("fault", 0)]
                    if getattr(link, "error", None):
                        APP.disconnect(op)
                events = APP.log.tail(after_id=last_evt)
                if events:
                    last_evt = events[-1]["id"]
                payload = {
                    "connected": link is not None,
                    "robot": robot,
                    "telem": telem,
                    "events": events,
                }
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            return

    # --- routes ---------------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/stream":
            self._stream(parse_qs(u.query))
        elif u.path == "/api/bootstrap":
            ops = stores.list_operators()
            self._json({"needs_setup": not ops, "operators": ops,
                        "app": "Safra Operator Console",
                        "version": version.VERSION})
        elif u.path == "/api/robots":
            if self._auth():
                self._json({"robots": stores.list_robots()})
        elif u.path == "/api/bindings":
            if self._auth():
                self._json({"bindings": stores.load_bindings()})
        elif u.path == "/api/update/status":
            if self._auth():
                self._json(updater.status())
        elif u.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
        else:
            self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()

        if u.path == "/api/setup":
            # first-run only; adding more operators later requires a session
            if stores.list_operators() and not APP.operator(self.headers.get("X-Session")):
                self._json({"error": "sign in to add operators"}, 401)
                return
            op, err = stores.add_operator(b.get("name", ""), b.get("callsign", ""),
                                          b.get("pin", ""))
            if err:
                self._json({"error": err}, 400)
                return
            APP.log.add("auth", "operator {} enrolled".format(op["name"]),
                        operator=op["name"])
            self._json({"ok": True, "operator": op})

        elif u.path == "/api/login":
            op = stores.check_pin(b.get("name", ""), b.get("pin", ""))
            if not op:
                time.sleep(0.5)  # blunt brute-force damper
                self._json({"error": "wrong operator or PIN"}, 401)
                return
            tok = secrets.token_urlsafe(24)
            APP.sessions[tok] = op
            APP.log.add("auth", "operator {} signed in".format(op["name"]),
                        operator=op["name"])
            self._json({"token": tok, "operator": op})

        elif u.path == "/api/logout":
            op = APP.operator(self.headers.get("X-Session"))
            if op:
                APP.disconnect(op)
                APP.sessions.pop(self.headers.get("X-Session"), None)
                APP.log.add("auth", "operator {} signed out".format(op["name"]),
                            operator=op["name"])
            self._json({"ok": True})

        elif u.path == "/api/robots":
            if not self._auth():
                return
            robot, err = stores.upsert_robot(b)
            self._json({"error": err} if err else {"ok": True, "robot": robot},
                       400 if err else 200)

        elif u.path == "/api/robots/delete":
            if not self._auth():
                return
            self._json({"ok": stores.delete_robot(b.get("id", ""))})

        elif u.path == "/api/connect":
            op = self._auth()
            if not op:
                return
            robot = stores.get_robot(b.get("id", ""))
            if not robot:
                self._json({"error": "unknown robot"}, 404)
                return
            try:
                APP.connect(robot, op)
            except OSError as e:
                APP.log.add("link", "connect to {} failed: {}".format(
                    robot["name"], e), operator=op["name"], robot=robot["name"])
                self._json({"error": "could not reach {}:{} — {}".format(
                    robot.get("host"), robot.get("port"), e)}, 502)
                return
            self._json({"ok": True, "robot": robot})

        elif u.path == "/api/bindings":
            if not self._auth():
                return
            ok = stores.save_bindings(b.get("bindings"))
            self._json({"ok": ok} if ok else {"error": "bad bindings"}, 200 if ok else 400)

        elif u.path == "/api/update/check":
            if self._auth():
                self._json(updater.check())

        elif u.path == "/api/update/apply":
            op = self._auth()
            if op:
                res = updater.apply()
                if res.get("staged"):
                    APP.log.add("app", "update v{} staged — restart the console to apply".format(
                        res.get("version")), operator=op["name"])
                self._json(res, 200 if res.get("ok") else 400)

        elif u.path == "/api/disconnect":
            op = self._auth()
            if op:
                APP.disconnect(op)
                self._json({"ok": True})

        elif u.path == "/api/cmd":
            op = self._auth()
            if not op:
                return
            link = APP.link
            if not link:
                self._json({"error": "not connected"}, 409)
                return
            t = b.get("t")
            link.send(b)
            # intents (drive/zv/yv) stream at 15 Hz — log only discrete acts
            if t in ("home", "stop", "clr", "estop", "zp", "yp"):
                APP.log.add("cmd", {"home": "HOME commanded",
                                    "stop": "STOP — all motion intents zeroed",
                                    "clr": "CLR — fault clear requested",
                                    "estop": "E-STOP pressed at the console",
                                    "zp": "Z point move {} mm".format(b.get("mm")),
                                    "yp": "Y point move {} mm".format(b.get("mm"))}[t],
                            operator=op["name"],
                            robot=APP.robot["name"] if APP.robot else None)
            self._json({"ok": True})

        else:
            self._json({"error": "not found"}, 404)


def serve(port):
    updater.check_async()  # non-blocking; result lands in /api/update/status
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd
