"""Pick-job integration for the operator console.

A JOB models how case-pick warehouses direct work: one pallet build for a
delivery route, made of an ordered list of case picks. Builds are
reverse-sequenced — the last stop's cases go on first, the first drop rides
on top — and every case gets a printed tag, plus one loader label for the
finished pallet (see labels.py).

There is no live WMS connection yet, so this layer is adapter-shaped:
jobs arrive as JSON or CSV exports (formats in the README) or from the
built-in demo generator, and a site-specific export maps onto the same
fields without code changes. The console tracks pick state, feeds the
label queue, and writes everything to the session log.

Pick states: pending -> picked, or pending -> quarantined (a damaged or
un-pickable case is flagged and skipped so the build keeps moving; a
morning crew clears quarantined cases during the wrap pass). A label
failure also flags the pallet but never stops picking.
"""

import csv
import io
import json
import secrets
import threading
import time

from stores import DATA_DIR, _load, _save
import os

JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")

PICK_STATES = ("pending", "picked", "quarantined")
LABEL_STATES = ("none", "printed", "failed", "off")

_lock = threading.Lock()
_jobs = None  # in-memory working copy; the SSE loop polls at 10 Hz


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _all():
    global _jobs
    if _jobs is None:
        _jobs = _load(JOBS_FILE, [])
    return _jobs


def _persist():
    _save(JOBS_FILE, _jobs)


def _get(jid):
    return next((j for j in _all() if j["id"] == jid), None)


# --- job construction -------------------------------------------------------------

def _new_job(name, route, stop, pallet, picks, source):
    job = {
        "id": secrets.token_hex(4),
        "name": name or _default_name(route, stop, pallet),
        "source": source,
        "created": _now(),
        "route": route, "stop": stop, "pallet": pallet,
        "status": "queued",          # queued -> active -> done
        "flagged": False,            # quarantine or label failure: check pallet
        "loader_label": "none",      # none / printed / failed / off
        "picks": picks,
    }
    return job


def _default_name(route, stop, pallet):
    bits = []
    if route:
        bits.append("Route {}".format(route))
    if stop:
        bits.append("Stop {}".format(stop))
    if pallet:
        bits.append("Pallet {}".format(pallet))
    return " · ".join(bits) or "Pick job"


def _clean_pick(raw, seq):
    sku = str(raw.get("sku") or "").strip()
    if not sku:
        return None
    qty = raw.get("qty") or 1
    try:
        qty = max(1, int(qty))
    except (TypeError, ValueError):
        qty = 1
    return {
        "seq": seq,
        "sku": sku,
        "desc": str(raw.get("desc") or raw.get("description") or "").strip(),
        "qty": qty,
        "location": str(raw.get("location") or raw.get("slot") or "").strip(),
        "stop": str(raw.get("stop") or "").strip(),
        "barcode": str(raw.get("barcode") or "").strip(),
        "status": "pending",
        "label": "none",
        "note": "",
        "ts": "",
    }


# --- import adapters ---------------------------------------------------------------

def import_text(text):
    """Parse a pasted/uploaded export into jobs. Accepts a JSON job object,
    {"jobs": [...]}, or CSV with a header row. Returns (jobs, error)."""
    text = (text or "").strip()
    if not text:
        return None, "nothing to import"
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError as e:
            return None, "bad JSON: {}".format(e)
        raws = data.get("jobs") if isinstance(data, dict) and "jobs" in data \
            else (data if isinstance(data, list) else [data])
        jobs = []
        for raw in raws:
            job, err = _from_dict(raw)
            if err:
                return None, err
            jobs.append(job)
        return jobs, None
    return _from_csv(text)


def _from_dict(raw):
    if not isinstance(raw, dict):
        return None, "each job must be a JSON object"
    picks = []
    for p in raw.get("picks") or []:
        pick = _clean_pick(p if isinstance(p, dict) else {}, len(picks) + 1)
        if pick:
            picks.append(pick)
    if not picks:
        return None, "job has no picks (each pick needs at least a sku)"
    return _new_job(str(raw.get("name") or "").strip(),
                    str(raw.get("route") or "").strip(),
                    str(raw.get("stop") or "").strip(),
                    str(raw.get("pallet") or "").strip(),
                    picks, "import"), None


def _from_csv(text):
    """CSV with a header row. Recognized columns (case-insensitive):
    sku, desc/description, qty, location/slot, stop, barcode, route, pallet.
    route/pallet are job-level and read from the first data row."""
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error as e:
        return None, "bad CSV: {}".format(e)
    if not rows:
        return None, "CSV has no data rows"
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            for r in rows]
    picks = []
    for r in rows:
        pick = _clean_pick(r, len(picks) + 1)
        if pick:
            picks.append(pick)
    if not picks:
        return None, "no rows had a sku column value"
    first = rows[0]
    return [_new_job("", first.get("route", ""), first.get("stop", ""),
                     first.get("pallet", ""), picks, "import")], None


# --- demo job ---------------------------------------------------------------------

_DEMO_PICKS = [
    # (sku, description, slot, qty, stop) — dry-grocery case picks, built in
    # reverse drop sequence: the highest stop number goes on the pallet first
    ("208114", "TOMATO KETCHUP 6/114 OZ",     "DA-02-1", 2, "8"),
    ("104620", "MARINARA SAUCE 6/#10 CAN",    "DA-03-1", 1, "8"),
    ("133071", "ENRICHED FLOUR 50 LB",        "DA-06-3", 1, "7"),
    ("121448", "GRANULATED SUGAR 25 LB",      "DA-06-1", 1, "7"),
    ("140262", "VEGETABLE OIL 6/1 GAL",       "DA-08-1", 2, "6"),
    ("152330", "WHITE VINEGAR 4/1 GAL",       "DA-09-2", 1, "5"),
    ("113005", "PENNE RIGATE 2/10 LB",        "DA-05-2", 1, "4"),
    ("166904", "SALTINE CRACKERS 500 CT",     "DA-11-1", 1, "3"),
    ("171215", "PAPER TOWELS 30 ROLL",        "DA-12-4", 1, "2"),
    ("183440", "FOAM HINGED TRAYS 200 CT",    "DA-14-2", 1, "1"),
]


def demo_job():
    picks = [_clean_pick({"sku": s, "desc": d, "location": loc, "qty": q,
                          "stop": stop}, i + 1)
             for i, (s, d, loc, q, stop) in enumerate(_DEMO_PICKS)]
    return _new_job("Demo build · Route 12", "12", "", "P-{}".format(
        secrets.token_hex(2).upper()), picks, "demo")


# --- store operations --------------------------------------------------------------

def list_jobs():
    with _lock:
        return [summary(j) for j in _all()]


def get_job(jid):
    with _lock:
        j = _get(jid)
        return json.loads(json.dumps(j)) if j else None


def add_jobs(jobs):
    with _lock:
        _all().extend(jobs)
        _persist()
    return [summary(j) for j in jobs]


def delete_job(jid):
    global _jobs
    with _lock:
        jobs = _all()
        n = len(jobs)
        _jobs = [j for j in jobs if j["id"] != jid]
        if len(_jobs) != n:
            _persist()
            return True
    return False


def activate(jid):
    """Make one job active; any other active job returns to the queue."""
    with _lock:
        job = _get(jid)
        if not job:
            return None, "unknown job"
        if job["status"] == "done":
            return None, "job is already complete"
        for j in _all():
            if j["status"] == "active" and j["id"] != jid:
                j["status"] = "queued"
        job["status"] = "active"
        _persist()
        return summary(job), None


def complete_job(jid):
    with _lock:
        job = _get(jid)
        if not job:
            return None, "unknown job"
        if any(p["status"] == "pending" for p in job["picks"]):
            return None, "job still has pending picks"
        job["status"] = "done"
        _persist()
        return summary(job), None


def active_job():
    with _lock:
        return next((j for j in _all() if j["status"] == "active"), None)


def _seq(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def set_pick(jid, seq, status, note="", label=None, expect=None):
    """Move one pick to a new state. Returns (job, pick, error). Pass
    expect="pending" to reject double-completion (streamed confirm inputs
    can fire twice for the same case)."""
    if status not in PICK_STATES:
        return None, None, "bad pick status"
    with _lock:
        job = _get(jid)
        if not job:
            return None, None, "unknown job"
        pick = next((p for p in job["picks"] if p["seq"] == _seq(seq)), None)
        if not pick:
            return None, None, "unknown pick"
        if expect and pick["status"] != expect:
            return None, None, "pick already {}".format(pick["status"])
        pick["status"] = status
        pick["ts"] = _now() if status != "pending" else ""
        pick["note"] = (note or "").strip()
        if label is not None:
            pick["label"] = label
        if status == "quarantined":
            job["flagged"] = True
        _persist()
        return job, pick, None


def set_pick_label(jid, seq, label):
    with _lock:
        job = _get(jid)
        pick = next((p for p in job["picks"] if p["seq"] == _seq(seq)),
                    None) if job else None
        if not pick:
            return None, None, "unknown pick"
        pick["label"] = label
        if label == "failed":
            job["flagged"] = True
        _persist()
        return job, pick, None


def set_loader_label(jid, label):
    with _lock:
        job = _get(jid)
        if not job:
            return None, "unknown job"
        job["loader_label"] = label
        if label == "failed":
            job["flagged"] = True
        _persist()
        return job, None


# --- summaries (job list + the 10 Hz SSE payload) -----------------------------------

def current_pick(job):
    return next((p for p in job["picks"] if p["status"] == "pending"), None)


def summary(job):
    picked = sum(1 for p in job["picks"] if p["status"] == "picked")
    quarantined = sum(1 for p in job["picks"] if p["status"] == "quarantined")
    return {
        "id": job["id"], "name": job["name"], "source": job["source"],
        "created": job["created"], "status": job["status"],
        "route": job["route"], "stop": job["stop"], "pallet": job["pallet"],
        "flagged": job["flagged"], "loader_label": job["loader_label"],
        "total": len(job["picks"]), "picked": picked,
        "quarantined": quarantined,
    }


def active_summary():
    """Compact live state for the SSE stream: the active job + current pick."""
    job = active_job()
    if not job:
        return None
    s = summary(job)
    pick = current_pick(job)
    s["current"] = None if pick is None else {
        "seq": pick["seq"], "sku": pick["sku"], "desc": pick["desc"],
        "qty": pick["qty"], "location": pick["location"], "stop": pick["stop"],
    }
    return s
