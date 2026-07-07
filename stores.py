"""Local persistence for the operator console: operators, robots, event log.

Per-machine state (gitignored in a source checkout). Operator PINs are
PBKDF2-hashed, but this is an OPERATOR IDENTITY layer for session logs — the
console binds to 127.0.0.1 and is not a network security boundary.
"""

import hashlib
import json
import os
import secrets
import sys
import threading
import time


def _default_data_dir():
    # explicit override wins; an installed (frozen) build keeps mutable state
    # out of the install dir, under %LOCALAPPDATA%; a source checkout uses ./data
    if os.environ.get("SAFRA_CONSOLE_DATA"):
        return os.environ["SAFRA_CONSOLE_DATA"]
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "SafraConsole", "data")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


DATA_DIR = _default_data_dir()
OPS_FILE = os.path.join(DATA_DIR, "operators.json")
BINDINGS_FILE = os.path.join(DATA_DIR, "bindings.json")
ROBOTS_FILE = os.path.join(DATA_DIR, "robots.json")
LOG_FILE = os.path.join(DATA_DIR, "session_log.jsonl")

SIM_ROBOT = {
    "id": "sim",
    "name": "Test Robot (Simulated)",
    "kind": "sim",
    "note": "Built-in protocol-v1 simulator — no hardware required",
}

_lock = threading.Lock()


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# --- operators ----------------------------------------------------------------

def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt),
                               200_000).hex()


def list_operators():
    with _lock:
        ops = _load(OPS_FILE, [])
    return [{"name": o["name"], "callsign": o.get("callsign", "")} for o in ops]


def add_operator(name, callsign, pin):
    name = name.strip()
    if not name or not pin or len(pin) < 4:
        return None, "name and a PIN of 4+ characters are required"
    with _lock:
        ops = _load(OPS_FILE, [])
        if any(o["name"].lower() == name.lower() for o in ops):
            return None, "operator already exists"
        salt = secrets.token_hex(16)
        ops.append({"name": name, "callsign": callsign.strip(),
                    "salt": salt, "pin": _hash_pin(pin, salt),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        _save(OPS_FILE, ops)
    return {"name": name, "callsign": callsign.strip()}, None


def check_pin(name, pin):
    with _lock:
        ops = _load(OPS_FILE, [])
    for o in ops:
        if o["name"].lower() == name.strip().lower():
            if secrets.compare_digest(_hash_pin(pin, o["salt"]), o["pin"]):
                return {"name": o["name"], "callsign": o.get("callsign", "")}
            return None
    return None


# --- robots ---------------------------------------------------------------------

def list_robots():
    with _lock:
        robots = _load(ROBOTS_FILE, None)
        if robots is None:
            robots = [dict(SIM_ROBOT),
                      {"id": secrets.token_hex(4), "name": "Pilot Rig B1",
                       "kind": "tcp", "host": "192.168.1.50", "port": 5760,
                       "note": "robot serial bridge — edit host before first use"}]
            _save(ROBOTS_FILE, robots)
        if not any(r["id"] == "sim" for r in robots):
            robots.insert(0, dict(SIM_ROBOT))
            _save(ROBOTS_FILE, robots)
    return robots


def upsert_robot(entry):
    name = (entry.get("name") or "").strip()
    host = (entry.get("host") or "").strip()
    try:
        port = int(entry.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not name or not host or not (0 < port < 65536):
        return None, "name, host, and a valid port are required"
    with _lock:
        robots = _load(ROBOTS_FILE, [])
        rid = entry.get("id") or secrets.token_hex(4)
        if rid == "sim":
            return None, "the simulated robot is built-in and not editable"
        new = {"id": rid, "name": name, "kind": "tcp", "host": host,
               "port": port, "note": (entry.get("note") or "").strip()}
        for i, r in enumerate(robots):
            if r["id"] == rid:
                robots[i] = new
                break
        else:
            robots.append(new)
        _save(ROBOTS_FILE, robots)
    return new, None


def delete_robot(rid):
    if rid == "sim":
        return False
    with _lock:
        robots = _load(ROBOTS_FILE, [])
        robots = [r for r in robots if r["id"] != rid]
        _save(ROBOTS_FILE, robots)
    return True


def get_robot(rid):
    return next((r for r in list_robots() if r["id"] == rid), None)


# --- control bindings (per-machine, shared across operators) ---------------------

def load_bindings():
    with _lock:
        return _load(BINDINGS_FILE, None)


def save_bindings(bindings):
    if not isinstance(bindings, dict):
        return False
    with _lock:
        _save(BINDINGS_FILE, bindings)
    return True


# --- event log -------------------------------------------------------------------

class EventLog:
    """Ring buffer for the UI + append-only JSONL on disk (the operator
    session audit trail)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []
        self._next_id = 1

    def add(self, kind, msg, operator=None, robot=None):
        evt = {"id": 0, "ts": time.strftime("%H:%M:%S"), "kind": kind,
               "msg": msg}
        with self._lock:
            evt["id"] = self._next_id
            self._next_id += 1
            self._events.append(evt)
            del self._events[:-200]
        rec = dict(evt, date=time.strftime("%Y-%m-%d"),
                   operator=operator, robot=robot)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        return evt

    def tail(self, after_id=0, n=60):
        with self._lock:
            return [e for e in self._events if e["id"] > after_id][-n:]
