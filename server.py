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
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import labels
import stores
import updater
import version
import wms
from links import SimLink, TcpLink

# PyInstaller unpacks bundled data (ui/) under sys._MEIPASS; a source checkout
# serves it from next to this file.
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(RES, "ui")
# a source checkout serves the monorepo's canonical assets; a packaged build
# carries a copy under ui/brand
BRAND_DIR = os.path.normpath(os.path.join(RES, "..", "..", "assets"))
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
        self.alive = 0            # open /api/alive streams (desktop-window heartbeat)

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

    def _send_label(self, render_fn):
        """Render + send one label per the printer config. Returns
        (status, detail): printed / failed / off (off = printer disabled —
        by policy a missing or dead printer never stops picking)."""
        cfg = labels.config()
        if not cfg["enabled"]:
            return "off", "printer disabled — label skipped"
        try:
            zpl = render_fn(cfg)
        except labels.TemplateError as e:
            return "failed", str(e)
        ok, detail = labels.send(zpl, cfg)
        return ("printed" if ok else "failed"), detail

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
                    "job": wms.active_summary(),
                }
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            return

    # --- liveness heartbeat -----------------------------------------------------

    def _alive(self):
        """Open-ended stream the desktop window holds while it's up.

        The shell can't track the window via the browser launcher process (Edge
        --app hands the window to a background process and the launcher exits at
        once), so the front-end keeps this stream open instead. When the window
        closes the socket drops, APP.alive falls to 0, and wait_until_idle()
        lets the process exit. No auth: it carries no data, only liveness.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with APP.lock:
            APP.alive += 1
        try:
            while True:
                self.wfile.write(b": alive\n\n")  # SSE comment; ignored by the client
                self.wfile.flush()
                time.sleep(1.0)
        except OSError:
            return
        finally:
            with APP.lock:
                APP.alive -= 1

    # --- routes ---------------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/alive":
            self._alive()
        elif u.path == "/api/stream":
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
        elif u.path == "/api/jobs":
            if self._auth():
                self._json({"jobs": wms.list_jobs()})
        elif u.path == "/api/jobs/detail":
            if self._auth():
                q = parse_qs(u.query)
                job = wms.get_job((q.get("id") or [""])[0])
                self._json({"job": job} if job else {"error": "unknown job"},
                           200 if job else 404)
        elif u.path == "/api/labels/preview":
            if self._auth():
                q = parse_qs(u.query)
                job = wms.get_job((q.get("id") or [""])[0])
                if not job:
                    self._json({"error": "unknown job"}, 404)
                    return
                seq = (q.get("seq") or [""])[0]
                try:
                    if seq == "loader":
                        fields = labels.loader_fields(job)
                        zpl = labels.loader_zpl(job)
                    else:
                        pick = next((p for p in job["picks"]
                                     if str(p["seq"]) == seq), None)
                        if not pick:
                            self._json({"error": "unknown pick"}, 404)
                            return
                        fields = labels.case_fields(job, pick)
                        zpl = labels.case_zpl(job, pick)
                except labels.TemplateError as e:
                    self._json({"error": str(e)}, 400)
                    return
                self._json({"fields": fields, "zpl": zpl})
        elif u.path == "/api/printer":
            if self._auth():
                self._json({"printer": labels.config()})
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

        # --- pick jobs / labels ------------------------------------------------

        elif u.path == "/api/jobs/import":
            op = self._auth()
            if not op:
                return
            jobs, err = wms.import_text(b.get("text", ""))
            if err:
                self._json({"error": err}, 400)
                return
            added = wms.add_jobs(jobs)
            APP.log.add("task", "imported {} pick job{}".format(
                len(added), "" if len(added) == 1 else "s"), operator=op["name"])
            self._json({"ok": True, "jobs": added})

        elif u.path == "/api/jobs/demo":
            op = self._auth()
            if not op:
                return
            added = wms.add_jobs([wms.demo_job()])
            APP.log.add("task", "demo pick job created", operator=op["name"])
            self._json({"ok": True, "jobs": added})

        elif u.path == "/api/jobs/activate":
            op = self._auth()
            if not op:
                return
            s, err = wms.activate(b.get("id", ""))
            if err:
                self._json({"error": err}, 400)
                return
            APP.log.add("task", "pick job started: {}".format(s["name"]),
                        operator=op["name"])
            self._json({"ok": True, "job": s})

        elif u.path == "/api/jobs/delete":
            if not self._auth():
                return
            self._json({"ok": wms.delete_job(b.get("id", ""))})

        elif u.path == "/api/jobs/complete":
            op = self._auth()
            if not op:
                return
            s, err = wms.complete_job(b.get("id", ""))
            if err:
                self._json({"error": err}, 400)
                return
            APP.log.add("task", "pallet build complete: {} ({}/{} cases{})".format(
                s["name"], s["picked"], s["total"],
                ", FLAGGED" if s["flagged"] else ""), operator=op["name"])
            self._json({"ok": True, "job": s})

        elif u.path == "/api/jobs/loader_label":
            op = self._auth()
            if not op:
                return
            job = wms.get_job(b.get("id", ""))
            if not job:
                self._json({"error": "unknown job"}, 404)
                return
            status, detail = self._send_label(
                lambda cfg: labels.loader_zpl(job, op["name"], cfg))
            wms.set_loader_label(job["id"], status)
            APP.log.add("label", "loader label {} — {}".format(
                "printed" if status == "printed" else status, detail),
                operator=op["name"])
            self._json({"ok": status == "printed", "label": status,
                        "detail": detail})

        elif u.path == "/api/picks/complete":
            op = self._auth()
            if not op:
                return
            job, pick, err = wms.set_pick(b.get("id", ""), b.get("seq"),
                                          "picked", expect="pending")
            if err:
                self._json({"error": err}, 400)
                return
            status, detail = self._send_label(
                lambda cfg: labels.case_zpl(job, pick, cfg))
            wms.set_pick_label(job["id"], pick["seq"], status)
            APP.log.add("task", "case {}/{} placed — {} ({})".format(
                pick["seq"], len(job["picks"]), pick["sku"],
                pick["desc"] or "case"), operator=op["name"],
                robot=APP.robot["name"] if APP.robot else None)
            if status == "failed":
                APP.log.add("label", "case tag FAILED — pallet flagged, "
                            "picking continues ({})".format(detail),
                            operator=op["name"])
            elif status == "printed":
                APP.log.add("label", "case tag printed — {}".format(pick["sku"]),
                            operator=op["name"])
            self._json({"ok": True, "label": status, "detail": detail})

        elif u.path == "/api/picks/quarantine":
            op = self._auth()
            if not op:
                return
            job, pick, err = wms.set_pick(b.get("id", ""), b.get("seq"),
                                          "quarantined", b.get("note", ""),
                                          expect="pending")
            if err:
                self._json({"error": err}, 400)
                return
            APP.log.add("task", "case {} QUARANTINED — pallet flagged for the "
                        "wrap crew{}".format(
                            pick["sku"],
                            ": " + pick["note"] if pick["note"] else ""),
                        operator=op["name"],
                        robot=APP.robot["name"] if APP.robot else None)
            self._json({"ok": True})

        elif u.path == "/api/picks/reopen":
            op = self._auth()
            if not op:
                return
            job, pick, err = wms.set_pick(b.get("id", ""), b.get("seq"),
                                          "pending", label="none")
            if err:
                self._json({"error": err}, 400)
                return
            APP.log.add("task", "case {} reopened".format(pick["sku"]),
                        operator=op["name"])
            self._json({"ok": True})

        elif u.path == "/api/labels/reprint":
            op = self._auth()
            if not op:
                return
            job = wms.get_job(b.get("id", ""))
            pick = next((p for p in job["picks"]
                         if str(p["seq"]) == str(b.get("seq"))), None) if job else None
            if not pick:
                self._json({"error": "unknown pick"}, 404)
                return
            status, detail = self._send_label(
                lambda cfg: labels.case_zpl(job, pick, cfg))
            wms.set_pick_label(job["id"], pick["seq"], status)
            APP.log.add("label", "case tag reprint {} — {} ({})".format(
                status, pick["sku"], detail), operator=op["name"])
            self._json({"ok": status == "printed", "label": status,
                        "detail": detail})

        elif u.path == "/api/printer":
            op = self._auth()
            if not op:
                return
            # validate custom templates before saving, so a bad one fails
            # loudly here instead of silently at 2 a.m. on the pick floor
            try:
                dj = wms.demo_job()
                if b.get("case_template"):
                    labels.render(b["case_template"],
                                  labels.case_fields(dj, dj["picks"][0]))
                if b.get("loader_template"):
                    labels.render(b["loader_template"], labels.loader_fields(dj))
            except labels.TemplateError as e:
                self._json({"error": str(e)}, 400)
                return
            cfg = labels.save_config(b)
            APP.log.add("label", "printer config saved — {}".format(
                "{}:{} enabled".format(cfg["host"], cfg["port"])
                if cfg["enabled"] else "disabled"), operator=op["name"])
            self._json({"ok": True, "printer": cfg})

        elif u.path == "/api/printer/test":
            op = self._auth()
            if not op:
                return
            ok, detail = labels.send(labels.test_zpl())
            APP.log.add("label", "printer test — {}".format(detail),
                        operator=op["name"])
            self._json({"ok": ok, "detail": detail})

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


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # don't let in-flight requests block process exit

    def handle_error(self, request, client_address):
        # A client vanishing mid-request (window closed, tab reloaded) aborts the
        # socket — that's expected here, not an error worth logging. It matters
        # doubly in the --windowed build, where sys.stderr is None and the
        # default traceback dump would blow up. Real errors still propagate.
        if not issubclass(sys.exc_info()[0] or Exception, (ConnectionError, BrokenPipeError)):
            super().handle_error(request, client_address)


def serve(port):
    updater.check_async()  # non-blocking; result lands in /api/update/status
    httpd = _Server(("127.0.0.1", port), Handler)
    return httpd


def wait_until_idle(grace=3.0, startup=30.0):
    """Block until the desktop window is gone.

    Returns once the front-end's /api/alive stream has connected and then stayed
    gone for `grace` seconds (window closed), or if no window ever connects
    within `startup` seconds (nothing to wait on). This is how the shell tracks
    the window's lifetime when the browser launcher process can't — see _alive.
    """
    start = time.monotonic()
    seen = False
    gone_at = None
    while True:
        time.sleep(0.5)
        if APP.alive > 0:
            seen, gone_at = True, None
        elif not seen:
            if time.monotonic() - start > startup:
                return
        elif gone_at is None:
            gone_at = time.monotonic()
        elif time.monotonic() - gone_at > grace:
            return
