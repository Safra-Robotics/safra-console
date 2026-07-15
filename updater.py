"""Update check for the installed console.

The distributable is a Windows installer (SafraConsole-Setup.exe, built by
tools/build_installer.py and published as a GitHub release asset). At
startup the app fetches the release feed (latest.json); if a newer version
is published, the UI shows an "Install update" button. That calls
apply_update(), which downloads the installer, verifies its sha256 against
the manifest, and launches it silently (/SILENT); the Inno Setup installer
closes the running app via the Restart Manager, updates in place, and
relaunches it. If the silent apply fails the UI falls back to opening the
installer URL for a manual download.

A source checkout never reports updates — git is its channel.

Trust model: the app trusts whatever the feed URL serves over HTTPS. The
default feed is the repo's public GitHub Releases "latest" endpoint, which
always resolves to the newest published release; override in
data/update.json.
"""

import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request

import version
from stores import DATA_DIR

DEFAULT_FEED = ("https://github.com/Safra-Robotics/safra-console"
                "/releases/latest/download/latest.json")
CONFIG_FILE = os.path.join(DATA_DIR, "update.json")

_lock = threading.Lock()
_status = {"checked": False, "available": None, "error": None}


def is_installed():
    """True when running as the packaged (PyInstaller) build."""
    return getattr(sys, "frozen", False)


def feed_url():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("feed_url") or DEFAULT_FEED
    except (OSError, ValueError):
        return DEFAULT_FEED


def _fetch(url, timeout=6):
    req = urllib.request.Request(
        url, headers={"User-Agent": "SafraConsole/" + version.VERSION})
    return urllib.request.urlopen(url=req, timeout=timeout,
                                  context=ssl.create_default_context())


def status():
    with _lock:
        s = dict(_status)
    s["version"] = version.VERSION
    s["installed"] = is_installed()
    s["feed"] = feed_url()
    return s


def check(timeout=6):
    """Fetch the feed and record whether a newer release is published."""
    with _lock:
        _status["error"] = None
    if not is_installed():
        with _lock:
            _status["checked"] = True
            _status["error"] = "source checkout — update via git"
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


def check_async():
    threading.Thread(target=check, daemon=True).start()


def download_installer(timeout=180):
    """Download the pending update's installer to a temp file, verifying its
    sha256 against the manifest. Returns the local path; raises on any problem
    (no update, network error, checksum mismatch) so the caller can fall back
    to a manual download."""
    with _lock:
        avail = _status.get("available")
    if not avail or not avail.get("url"):
        raise RuntimeError("no update is available")
    want = (avail.get("sha256") or "").lower()
    dest = os.path.join(tempfile.mkdtemp(prefix="SafraConsoleUpdate-"),
                        "SafraConsole-Setup.exe")
    sha = hashlib.sha256()
    with _fetch(avail["url"], timeout) as r, open(dest, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
            sha.update(chunk)
    if want and sha.hexdigest() != want:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError("update checksum mismatch — download rejected")
    return dest


def apply_update():
    """Download the update and launch its installer silently.

    The Inno Setup installer closes this running app via the Restart Manager,
    updates in place, and relaunches it (see tools/build_installer.py), so this
    returns just before the app is shut down. Packaged build only — a source
    checkout updates via git.
    """
    if not is_installed():
        raise RuntimeError("auto-update applies to the installed build only "
                           "(a source checkout updates via git)")
    installer = download_installer()
    # /SILENT shows a small progress bar (no wizard); /SUPPRESSMSGBOXES and
    # /NOCANCEL keep it hands-off. Detach it so it outlives this process when
    # the installer's Restart Manager step closes the app mid-update.
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([installer, "/SILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL"],
                     creationflags=flags, close_fds=True)
    return {"ok": True}
