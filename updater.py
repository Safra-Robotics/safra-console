"""Auto-update for the packaged console.

Update unit = the `app/` folder of a packaged install (see
tools/build_release.py for the layout: <root>/python/ + <root>/app/ +
SafraConsole.bat). Flow:

  1. fetch <feed_url> (latest.json: {"version","url","sha256","notes"})
  2. if newer than version.VERSION: download the release zip, verify sha256
  3. extract its app/ subtree to <root>/app_next/
  4. the launcher (SafraConsole.bat) swaps app_next -> app on next start

Dev checkouts (running from the git repo, no package marker) never
self-update — git is the update channel there; check() says so.

Trust model, stated honestly: the app trusts whatever the feed URL serves
(sha256 in the manifest guards download corruption, not a hostile host).
Keep the feed on a host you control over HTTPS. The default feed is the
public GitHub Releases "latest" endpoint (see build_release.py), which
always resolves to the newest published release; override in
data/update.json.
"""

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import threading
import urllib.request
import zipfile

import version
from stores import DATA_DIR

DEFAULT_FEED = ("https://github.com/Safra-Robotics/safra-operator-console"
                "/releases/latest/download/latest.json")
CONFIG_FILE = os.path.join(DATA_DIR, "update.json")

HERE = os.path.dirname(os.path.abspath(__file__))
# packaged layout: <root>/app/updater.py + <root>/package.json marker
PKG_ROOT = os.path.dirname(HERE)
PKG_MARKER = os.path.join(PKG_ROOT, "package.json")

_lock = threading.Lock()
_status = {"checked": False, "available": None, "staged": False, "error": None}


def is_packaged():
    return os.path.basename(HERE) == "app" and os.path.isfile(PKG_MARKER)


def feed_url():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("feed_url") or DEFAULT_FEED
    except (OSError, ValueError):
        return DEFAULT_FEED


def _fetch(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "SafraConsole/" + version.VERSION})
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def status():
    with _lock:
        s = dict(_status)
    s["version"] = version.VERSION
    s["packaged"] = is_packaged()
    s["feed"] = feed_url()
    return s


def check(timeout=6):
    """Fetch the feed and record whether a newer release exists."""
    with _lock:
        _status["error"] = None
    if not is_packaged():
        with _lock:
            _status["checked"] = True
            _status["error"] = "dev checkout — update via git pull"
        return status()
    try:
        with _fetch(feed_url(), timeout) as r:
            manifest = json.loads(r.read().decode())
        newer = version.is_newer(manifest.get("version", "0"))
        with _lock:
            _status["checked"] = True
            _status["available"] = manifest if newer else None
    except (OSError, ValueError) as e:
        with _lock:
            _status["checked"] = True
            _status["error"] = "update check failed: {}".format(e)
    return status()


def apply():
    """Download + verify + stage the available update. Swap happens at next
    launch (the .bat replaces app/ with app_next/ before starting Python)."""
    with _lock:
        manifest = _status.get("available")
    if not is_packaged():
        return {"error": "dev checkout — update via git pull"}
    if not manifest:
        return {"error": "no update available (check first)"}
    url, want_sha = manifest.get("url"), (manifest.get("sha256") or "").lower()
    if not url or not want_sha:
        return {"error": "feed manifest is missing url/sha256"}
    try:
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        with os.fdopen(fd, "wb") as out, _fetch(url, timeout=60) as r:
            h = hashlib.sha256()
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest().lower() != want_sha:
            os.unlink(tmp)
            return {"error": "sha256 mismatch — download rejected"}
        staged = os.path.join(PKG_ROOT, "app_next")
        if os.path.isdir(staged):
            shutil.rmtree(staged)
        with zipfile.ZipFile(tmp) as z:
            # release zips carry SafraConsole/app/... — extract only app/*
            # (names normalized: some archivers emit backslash separators)
            members = [(m, m.replace("\\", "/")) for m in z.namelist()
                       if "/app/" in "/" + m.replace("\\", "/")]
            if not members:
                os.unlink(tmp)
                return {"error": "release zip has no app/ subtree"}
            prefix = members[0][1].split("app/")[0] + "app/"
            for m, nm in members:
                rel = nm[len(prefix):]
                if not rel or rel.endswith("/"):
                    continue
                dest = os.path.normpath(os.path.join(staged, rel))
                if not dest.startswith(os.path.normpath(staged)):
                    continue  # zip-slip guard
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        os.unlink(tmp)
        with _lock:
            _status["staged"] = True
        return {"ok": True, "staged": True, "version": manifest.get("version")}
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        return {"error": "update failed: {}".format(e)}


def check_async():
    threading.Thread(target=check, daemon=True).start()
