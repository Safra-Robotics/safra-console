"""Case-tag and pallet loader-label printing for pick jobs.

Warehouses that direct picks from handheld scanners print a tag per case
and a loader label per finished pallet; a robot-built pallet has to come
out labeled the same way or someone downstacks it just to tag it. This
module renders both as ZPL (the Zebra printer language, the de-facto
standard for warehouse label printers) and sends them raw to a networked
printer on the standard ZPL port, 9100.

Templates are plain ZPL with {field} placeholders and can be overridden in
the printer config, because tag layouts are site-specific — the defaults
here are a readable starting point sized for a 3-inch (576-dot) label, not
any site's real format. Swap in the site's own ZPL once its tag spec is in
hand.

Degradation policy: a label failure NEVER stops picking. The pick still
completes, the pallet is flagged, and the loader label (or the morning
crew) catches it — an unmanned shift can't stop to fix a printer.
"""

import os
import socket
import threading
import time

from stores import DATA_DIR, _load, _save

PRINTER_FILE = os.path.join(DATA_DIR, "printer.json")

DEFAULTS = {
    "enabled": False,      # off until a printer is configured
    "host": "",
    "port": 9100,          # Zebra raw-ZPL port
    "case_template": "",   # blank -> built-in default
    "loader_template": "",
}

_lock = threading.Lock()

# 3-inch / 203 dpi starting layout: route+stop band, big SKU, two-line
# description, qty + case count, Code 128 barcode, timestamp footer.
CASE_TEMPLATE = """^XA
^PW576
^LL400
^CF0,30
^FO28,24^FD{route_stop}^FS
^FO28,24^FB520,1,0,R,0^FDPALLET {pallet}^FS
^CF0,62
^FO28,62^FD{sku}^FS
^CF0,28
^FO28,134^FB520,2,4,L,0^FD{desc}^FS
^CF0,30
^FO28,204^FDQTY {qty}   CASE {seq}/{total}^FS
^BY2,3,84
^FO28,248^BCN,84,N,N,N^FD{barcode}^FS
^CF0,22
^FO28,352^FD{ts}^FS
^XZ
"""

LOADER_TEMPLATE = """^XA
^PW576
^LL400
^CF0,40
^FO28,28^FDLOADER^FS
^CF0,58
^FO28,74^FD{route_stop}^FS
^CF0,34
^FO28,148^FDPALLET {pallet}^FS
^FO28,192^FDCASES {picked}/{total}^FS
^CF0,30
^FO28,240^FD{flag_line}^FS
^BY2,3,70
^FO28,278^BCN,70,N,N,N^FD{barcode}^FS
^CF0,22
^FO28,364^FD{ts}   {operator}^FS
^XZ
"""


def config():
    with _lock:
        cfg = dict(DEFAULTS)
        cfg.update(_load(PRINTER_FILE, {}))
    try:
        cfg["port"] = int(cfg.get("port") or 9100)
    except (TypeError, ValueError):
        cfg["port"] = 9100
    return cfg


def save_config(updates):
    cfg = config()
    for k in DEFAULTS:
        if k in updates:
            cfg[k] = updates[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["host"] = str(cfg.get("host") or "").strip()
    try:
        cfg["port"] = int(cfg.get("port") or 9100)
    except (TypeError, ValueError):
        cfg["port"] = 9100
    with _lock:
        _save(PRINTER_FILE, cfg)
    return cfg


# --- rendering ---------------------------------------------------------------------

def _route_stop(job, pick=None):
    stop = (pick or {}).get("stop") or job.get("stop") or ""
    bits = []
    if job.get("route"):
        bits.append("RT {}".format(job["route"]))
    if stop:
        bits.append("STOP {}".format(stop))
    return " · ".join(bits) or "PICK"


def case_fields(job, pick):
    return {
        "sku": pick["sku"],
        "desc": pick["desc"] or pick["sku"],
        "qty": pick["qty"],
        "seq": pick["seq"],
        "total": len(job["picks"]),
        "route": job.get("route", ""),
        "stop": pick.get("stop") or job.get("stop", ""),
        "route_stop": _route_stop(job, pick),
        "pallet": job.get("pallet", ""),
        "barcode": pick.get("barcode") or pick["sku"],
        "ts": time.strftime("%Y-%m-%d %H:%M"),
    }


def loader_fields(job, operator=""):
    picked = sum(1 for p in job["picks"] if p["status"] == "picked")
    return {
        "route": job.get("route", ""),
        "stop": job.get("stop", ""),
        "route_stop": _route_stop(job),
        "pallet": job.get("pallet", ""),
        "picked": picked,
        "total": len(job["picks"]),
        "flag_line": "** CHECK PALLET — SEE LOG **" if job.get("flagged") else "",
        "barcode": job.get("pallet") or job["id"],
        "operator": operator or "",
        "ts": time.strftime("%Y-%m-%d %H:%M"),
    }


class TemplateError(ValueError):
    pass


def render(template, fields):
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        raise TemplateError("bad label template: {}".format(e))


def case_zpl(job, pick, cfg=None):
    cfg = cfg or config()
    return render(cfg.get("case_template") or CASE_TEMPLATE, case_fields(job, pick))


def loader_zpl(job, operator="", cfg=None):
    cfg = cfg or config()
    return render(cfg.get("loader_template") or LOADER_TEMPLATE,
                  loader_fields(job, operator))


def test_zpl():
    return ("^XA^PW576^LL200^CF0,44^FO28,40^FDSAFRA CONSOLE^FS"
            "^CF0,28^FO28,100^FDprinter test · {}^FS^XZ".format(
                time.strftime("%Y-%m-%d %H:%M")))


# --- transport ---------------------------------------------------------------------

def send(zpl, cfg=None, timeout=3.0):
    """Push raw ZPL at the configured printer. Returns (ok, detail)."""
    cfg = cfg or config()
    if not cfg["enabled"]:
        return False, "printer disabled"
    if not cfg["host"]:
        return False, "no printer host configured"
    try:
        with socket.create_connection((cfg["host"], cfg["port"]),
                                      timeout=timeout) as s:
            s.sendall(zpl.encode("utf-8"))
        return True, "sent to {}:{}".format(cfg["host"], cfg["port"])
    except OSError as e:
        return False, "printer unreachable at {}:{} — {}".format(
            cfg["host"], cfg["port"], e)
