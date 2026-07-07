"""Robot links for the operator console.

SimLink wraps the in-process SimRobot. TcpLink speaks protocol.md v1
over TCP — the deployment assumption is the robot's onboard computer (or a
dev machine) exposing the motion-controller UART on a TCP port (e.g.
socat / ser2net / a console bridge service). Console->robot frames start
'>', robot->console '<', body is checksummed with a two-hex-digit XOR
exactly as the controller expects.

The Uno's 300 ms watchdog is fed by ANY valid frame, so TcpLink streams
>HB* at ~12 Hz whenever no command traffic is flowing.
"""

import socket
import threading
import time

from sim import SimRobot, BOOT, FAULT


def checksum(body):
    x = 0
    for ch in body:
        x ^= ord(ch)
    return "{:02X}".format(x)


def frame(body):
    return ">{}*{}\n".format(body, checksum(body))


def parse(line):
    """'<BODY*HH' -> BODY, or None on bad framing/checksum (protocol: that is
    a safety event on the wire; on the console side we just count it)."""
    line = line.strip()
    if len(line) < 4 or line[0] != "<" or line[-3] != "*":
        return None
    body, hh = line[1:-3], line[-2:]
    return body if checksum(body) == hh.upper() else None


class SimLink:
    kind = "sim"

    def __init__(self):
        self.robot = SimRobot()
        self.error = None

    def send(self, cmd):
        r = self.robot
        t = cmd.get("t")
        if t == "drive":
            r.cmd_drive(cmd.get("l", 0), cmd.get("r", 0))
        elif t == "zv":
            r.cmd_jog("z", cmd.get("v", 0))
        elif t == "yv":
            r.cmd_jog("y", cmd.get("v", 0))
        elif t == "zp":
            r.cmd_point("z", cmd.get("mm", 0), cmd.get("v", 40))
        elif t == "yp":
            r.cmd_point("y", cmd.get("mm", 0), cmd.get("v", 40))
        elif t == "home":
            r.cmd_home()
        elif t == "stop":
            r.cmd_stop()
        elif t == "clr":
            r.cmd_clr()
        elif t == "estop":
            r.trip_estop()
        elif t == "estop_reset":
            r.reset_estop()

    def snapshot(self):
        snap = self.robot.snapshot()
        snap["link_ok"] = True
        return snap

    def drain_events(self):
        return self.robot.drain_events()

    def close(self):
        self.robot.close()


class TcpLink:
    kind = "tcp"

    def __init__(self, host, port):
        self.host, self.port = host, int(port)
        self._sock = None
        self._lock = threading.Lock()
        self._telem = {}
        self._last_rx = 0.0
        self._last_tx = 0.0
        self._bad_frames = 0
        self._events = []
        self._run = False
        self.error = None

    def connect(self, timeout=3.0):
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.settimeout(0.5)
        self._run = True
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._hb_loop, daemon=True).start()
        self._send_raw(frame("Q"))  # force an immediate status frame

    def _send_raw(self, data):
        with self._lock:
            if not self._sock:
                return
            try:
                self._sock.sendall(data.encode("ascii"))
                self._last_tx = time.monotonic()
            except OSError as e:
                self._fail("link write failed: {}".format(e))

    def _fail(self, msg):
        if self._run:
            self._run = False
            self.error = msg
            self._events.append(msg)
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _hb_loop(self):
        # feed the Uno watchdog whenever no other traffic flows (>=10 Hz)
        while self._run:
            if time.monotonic() - self._last_tx > 0.08:
                self._send_raw(frame("HB"))
            time.sleep(0.04)

    def _rx_loop(self):
        buf = b""
        while self._run:
            sock = self._sock
            if sock is None:
                return
            try:
                data = sock.recv(1024)
            except socket.timeout:
                continue
            except OSError as e:
                self._fail("link read failed: {}".format(e))
                return
            if not data:
                self._fail("robot closed the connection")
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                body = parse(line.decode("ascii", "replace"))
                if body is None:
                    self._bad_frames += 1
                    continue
                self._last_rx = time.monotonic()
                if body.startswith("ST,"):
                    self._on_status(body)
                elif body.startswith(("ACK", "NAK")):
                    self._events.append(body)

    def _on_status(self, body):
        # <ST,state,fault,z,y,limits,estop,cap  (z/y in 0.1 mm units)
        p = body.split(",")
        if len(p) < 8:
            self._bad_frames += 1
            return
        try:
            z = int(p[3]) / 10.0
            self._telem = {
                "state": int(p[1]), "fault": int(p[2]),
                "z_mm": z, "y_mm": int(p[4]) / 10.0, "fork_mm": z + 70.0,
                "limits": p[5], "estop_ok": p[6] == "1", "cap": int(p[7]),
            }
        except ValueError:
            self._bad_frames += 1

    def send(self, cmd):
        t = cmd.get("t")
        body = None
        if t == "drive":
            body = "DRV,{:d},{:d}".format(int(cmd.get("l", 0)), int(cmd.get("r", 0)))
        elif t == "zv":
            body = "ZV,{:d}".format(int(cmd.get("v", 0)))
        elif t == "yv":
            body = "YV,{:d}".format(int(cmd.get("v", 0)))
        elif t == "zp":
            body = "ZP,{:d},{:d}".format(int(cmd.get("mm", 0)), int(cmd.get("v", 40)))
        elif t == "yp":
            body = "YP,{:d},{:d}".format(int(cmd.get("mm", 0)), int(cmd.get("v", 40)))
        elif t == "home":
            body = "HOME"
        elif t in ("stop", "estop"):
            body = "STOP"  # the real e-stop chain is hardware-only by design
        elif t == "clr":
            body = "CLR"
        if body:
            self._send_raw(frame(body))

    def snapshot(self):
        snap = dict(self._telem) if self._telem else {
            "state": BOOT, "fault": 0, "z_mm": 0, "y_mm": 0, "fork_mm": 70,
            "limits": "0000", "estop_ok": True, "cap": 0}
        age = time.monotonic() - self._last_rx if self._last_rx else 999.0
        snap.update({
            "sim": False,
            "link_ok": self._run and age < 1.0,
            "rx_age_ms": int(age * 1000) if age < 999 else None,
            "bad_frames": self._bad_frames,
        })
        if self.error:
            snap["link_error"] = self.error
        return snap

    def drain_events(self):
        out, self._events = self._events, []
        return out

    def close(self):
        self._run = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
