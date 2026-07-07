"""Update check for the installed console.

The distributable is a Windows installer (SafraConsole-Setup.exe, built by
tools/build_installer.py and published as a GitHub release asset). At
startup the app fetches the release feed (latest.json); if a newer version
is published, the UI shows a "Download update" button that opens the new
installer. Running it updates in place — the Inno Setup installer closes the
running app and restarts it via the Windows Restart Manager.

A source checkout never reports updates — git is its channel.

Trust model: the app trusts whatever the feed URL serves over HTTPS. The
default feed is the repo's public GitHub Releases "latest" endpoint, which
always resolves to the newest published release; override in
data/update.json.
"""

import json
import os
import ssl
import sys
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
