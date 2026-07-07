"""Safra Operator Console — desktop entry point.

Zero-dependency: Python stdlib backend + a native window shell.
Window preference order:
  1. pywebview, if installed (nicest: real native window)
  2. Microsoft Edge in --app mode (chromeless app window; present on Win11)
  3. the default browser (last resort)

Usage:
  python safra_console.py               # serve + open the app window
  python safra_console.py --serve-only  # backend only (dev / browser use)
  python safra_console.py --port 8973
"""

import argparse
import os
import subprocess
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

DEFAULT_PORT = 8973


def _log(msg):
    # a --windowed (no-console) build has sys.stdout = None; don't crash on it
    try:
        if sys.stdout:
            print(msg)
    except Exception:
        pass


def find_edge():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     r"Microsoft\Edge\Application\msedge.exe"),
    ]
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                candidates.insert(0, winreg.QueryValue(k, None))
        except OSError:
            pass
    return next((p for p in candidates if p and os.path.isfile(p)), None)


def open_window(url):
    try:
        import webview  # type: ignore
        w = webview.create_window("Safra Operator Console", url,
                                  width=1280, height=800, background_color="#0E0E0E")
        webview.start()
        return "pywebview", w
    except ImportError:
        pass
    edge = find_edge()
    if edge:
        profile = os.path.join(os.environ.get("LOCALAPPDATA", "."),
                               "SafraConsole", "edge-profile")
        proc = subprocess.Popen([edge, "--app=" + url, "--window-size=1280,800",
                                 "--user-data-dir=" + profile])
        proc.wait()
        return "edge-app", None
    webbrowser.open(url)
    return "browser", None


def main():
    ap = argparse.ArgumentParser(description="Safra Operator Console")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--serve-only", action="store_true",
                    help="run the backend without opening a window")
    args = ap.parse_args()

    try:
        httpd = server.serve(args.port)
    except OSError:
        # port busy (e.g. a second launch) — take any free port
        httpd = server.serve(0)
    port = httpd.server_address[1]
    url = "http://127.0.0.1:{}/".format(port)
    _log("Safra Operator Console at {}".format(url))

    if args.serve_only:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    mode, _ = open_window(url)
    if mode == "browser":
        # nothing to wait on — keep serving until Ctrl+C
        _log("opened in the default browser; Ctrl+C to quit")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    httpd.shutdown()


if __name__ == "__main__":
    main()
