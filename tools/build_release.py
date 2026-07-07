"""Build a distributable Safra Operator Console release (Windows x64).

Stdlib only. Produces:

  dist/SafraConsole/                     portable install
    python/                              embeddable CPython (downloaded, cached)
    app/                                 console code + ui (+ ui/brand assets)
    SafraConsole.bat                     launcher; applies staged updates
    package.json                         marker + version (updater checks this)
  dist/SafraConsole-v<V>-win64.zip       the release artifact
  dist/latest.json                       update-feed manifest for this release

Publish a release = attach BOTH files as assets of a GitHub Release so
that <base-url>/latest.json and <base-url>/<zip name> resolve. With
`--base-url` pointing at the repo's `releases/latest/download` endpoint
(the default), the feed always resolves to the newest published release.
The console's updater (app/updater.py) fetches latest.json, compares
versions, verifies the zip's sha256, stages app/ and swaps on next launch.

Usage:  python tools/build_release.py [--base-url URL] [--notes "..."]
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
import zipfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(TOOLS)   # repo root (standalone layout)
DIST = os.path.join(CONSOLE, "dist")
CACHE = os.path.join(TOOLS, "_cache")

sys.path.insert(0, CONSOLE)
from version import VERSION  # noqa: E402

PY_EMBED_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
# brand assets (ui/brand/) ride along in the ui/ copytree below
APP_FILES = ["safra_console.py", "server.py", "sim.py", "links.py",
             "stores.py", "updater.py", "version.py", "protocol.md", "README.md"]
DEFAULT_BASE_URL = ("https://github.com/SuleimanGrape/safra-operator-console"
                    "/releases/latest/download")

LAUNCHER = r"""@echo off
rem Safra Operator Console launcher. If an update was staged (app_next),
rem swap it in before starting - see app\updater.py.
cd /d "%~dp0"
if exist app_next (
  if exist app_old rmdir /s /q app_old
  move app app_old >nul
  move app_next app >nul
  rmdir /s /q app_old
)
set "SAFRA_CONSOLE_DATA=%LOCALAPPDATA%\SafraConsole\data"
start "" "%~dp0python\pythonw.exe" "%~dp0app\safra_console.py"
"""


def fetch_embed():
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, os.path.basename(PY_EMBED_URL))
    if not os.path.isfile(cached):
        print("downloading", PY_EMBED_URL)
        req = urllib.request.Request(PY_EMBED_URL, headers={"User-Agent": "safra-build"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        with open(cached, "wb") as f:
            f.write(data)
    return cached


def build(base_url, notes):
    root = os.path.join(DIST, "SafraConsole")
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)

    # python/ — embeddable runtime; ._pth gains ..\app so the console imports
    # its own modules (embeddable python locks sys.path to the ._pth entries)
    pydir = os.path.join(root, "python")
    with zipfile.ZipFile(fetch_embed()) as z:
        z.extractall(pydir)
    pth = next(f for f in os.listdir(pydir) if f.endswith("._pth"))
    with open(os.path.join(pydir, pth), "a", encoding="ascii") as f:
        f.write("..\\app\n")

    # app/ — flat modules + the whole ui/ tree (fonts + brand included)
    app = os.path.join(root, "app")
    os.makedirs(app)
    for name in APP_FILES:
        shutil.copy2(os.path.join(CONSOLE, name), app)
    shutil.copytree(os.path.join(CONSOLE, "ui"), os.path.join(app, "ui"))

    with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "SafraConsole", "version": VERSION}, f)
    with open(os.path.join(root, "SafraConsole.bat"), "w", encoding="ascii") as f:
        f.write(LAUNCHER)

    # zip it
    zip_name = "SafraConsole-v{}-win64.zip".format(VERSION)
    zip_path = os.path.join(DIST, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                full = os.path.join(dirpath, name)
                z.write(full, os.path.relpath(full, DIST))

    sha = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)

    manifest = {"version": VERSION, "url": base_url.rstrip("/") + "/" + zip_name,
                "sha256": sha.hexdigest(), "notes": notes}
    with open(os.path.join(DIST, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print("built  ", zip_path, "({:.1f} MB)".format(os.path.getsize(zip_path) / 1e6))
    print("sha256 ", sha.hexdigest())
    print("feed   ", os.path.join(DIST, "latest.json"))
    print("publish: gh release create v{v} \"{z}\" \"{j}\"".format(
        v=VERSION, z=zip_path, j=os.path.join(DIST, "latest.json")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()
    build(args.base_url, args.notes)
