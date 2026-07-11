# Safra Operator Console

A desktop app for driving [Safra Robotics](https://safrarobotics.com) rigs
from your computer. You sign in as an operator, pick a robot, and get a
console for driving, lifting, and reaching with live telemetry. Controls
work from the keyboard or a game controller, there's a first-person 3D view
of the robot, and every session is logged.

It ships with a **simulated test robot** built in, so you can open the app
and try the whole thing right away — no hardware required.

## Download

Grab the Windows installer from **[safrarobotics.com/console](https://safrarobotics.com/console)**
(or straight from the [Releases](https://github.com/Safra-Robotics/safra-console/releases/latest)
page) and run **`SafraConsole-Setup.exe`**. It installs just for your user —
no admin rights, no Python — adds a Start-Menu shortcut, and shows up in
*Apps & features* like any other program. When a new version is out, the app
notices on launch and points you at the installer, which updates it in place.

## Run from source

You'll need Python 3.10 or newer. There's nothing to `pip install` — it runs
on the standard library alone:

```
python safra_console.py
```

The backend listens on `127.0.0.1:8973` and opens a window using whatever's
available, in this order: [pywebview](https://pywebview.flowrl.com/) if you
have it, then Microsoft Edge in `--app` mode (a chromeless app window), and
finally your default browser. Two flags are handy: `--port N` and
`--serve-only`.

The first time you run it, you'll enroll the first operator (name, callsign,
PIN). Operators, robots, and control bindings are all kept locally — in
`data/` when you're running from a checkout, or
`%LOCALAPPDATA%\SafraConsole\data` on a packaged install.

## Robots

- **Test Robot (Simulated)** — always there, always available, no hardware
  needed. It's an in-process model of the [`protocol.md`](protocol.md) v1
  contract and the pilot rig's published motion limits, and it behaves like
  the real thing: it boots through `BOOT → HOMING → READY / MOVING / FAULT`,
  refuses Z moves until it's homed, and homes by seeking, backing off, and
  zeroing. The lift runs at 90 mm/s and drops to a 25 mm/s creep above the
  300 mm fork line (both caps halve when two axes move at once). Drive
  throttle is capped at 40 % with the fork raised. There's a
  brake → 150 ms → reverse interlock, faults latch and only clear once the
  e-stop chain is healthy again, and drive intents dead-man to zero if they
  stop arriving. The E-STOP button trips the simulated safety chain, so you
  can practice the trip → reset → clear cycle for real.
- **Field robots (TCP)** — add one with a name and a `host:port`. The console
  talks `protocol.md` v1 frames (`>BODY*HH`, XOR checksum) over TCP, and
  expects the robot's onboard computer to expose the motion-controller UART
  on that port. It sends heartbeats at about 12 Hz to keep the controller's
  300 ms watchdog happy and reads `<ST` telemetry back at 10 Hz. On a field
  link the red button sends **STOP** — the physical e-stop chain is
  hardware-only, by design.

## Controls

Hit **MAP CONTROLS** in the DRIVE panel to open the binding table. Every
action — forward / reverse / turns, fork up / down, reach out / in, stop,
e-stop, home, and creep-hold — can have several bindings at once, from the
keyboard or a game controller (standard-mapping Gamepad API; Xbox button
names are shown, and analog sticks get a deadzone plus analog scaling). Click
＋ on a row, then press a key or button or push a stick to capture it; click a
chip to remove it. Bindings are saved per machine.

Out of the box: **WASD / arrows** or the **left stick** drive · **R / F** or
the **right stick** raise and lower the fork · **T / G** or the **D-pad**
extend the reach · **Space / B** stop · **X** e-stop · **H / Y** home ·
**Shift / LB** creep. There's also an on-screen joystick and jog buttons that
mirror the keys.

## Simulated view

The simulated robot gets a first-person 3D view from the mast camera. It's a
small software pinhole projection drawn on a 2D canvas — no external
libraries — with a floor grid, walls, racking full of cases, a pallet drop
zone, and the robot's own fork blades rising through the frame as you lift.
Field robots show a placeholder here until live video ships.

## Pick tasks & label printing (WMS integration)

Warehouses that direct case picking from handheld scanners also print a tag
for every case and a loader label for every finished pallet — so a
robot-built pallet has to come out labeled the same way, or someone ends up
downstacking it just to tag it. The console carries that flow end to end:

- **Pick jobs.** A job is one pallet build for a delivery route: an ordered
  list of case picks (reverse drop sequence — the first stop rides on top),
  each with a SKU, description, quantity, and slot. Open the **PICK TASKS**
  dashboard (button on the robot-select screen, or DASHBOARD in the console
  sidebar) to import jobs from a WMS export — JSON or CSV, pasted or loaded
  from a file — or generate the built-in demo job to try it.
- **Working the queue.** With a job active, the PICK TASK panel in the
  console sidebar shows the current case (SKU, slot, qty, stop) and live
  progress. **CASE PLACED** — also Enter, or gamepad **A**, remappable like
  any binding — confirms the case and prints its tag; **QUARANTINE** flags
  and skips a damaged or un-pickable case so the build keeps moving, and the
  pallet is marked for a follow-up check. When every case is handled, print
  the pallet's loader label and close the job. Everything lands in the
  session log under the operator's name.
- **Labels.** Case tags and loader labels render as ZPL and go raw over TCP
  to a networked label printer (host + port in the dashboard; 9100 is the
  usual raw-ZPL port — the label preview shows the exact ZPL either way). A
  dead or missing printer never stops picking: the pick still completes, the
  pallet is flagged, and the log carries the reason. Tag layouts are
  site-specific, so the built-in templates are readable placeholders — real
  ones drop in as `case_template` / `loader_template` (ZPL with `{field}`
  placeholders) via `POST /api/printer` or `data/printer.json`.

Import formats:

```json
{
  "name": "Route 12 · Pallet A",
  "route": "12", "pallet": "A",
  "picks": [
    {"sku": "104620", "desc": "MARINARA SAUCE 6/#10", "qty": 1,
     "location": "DA-03-1", "stop": "4", "barcode": "LP000123"}
  ]
}
```

JSON can be one job, a list of jobs, or `{"jobs": [...]}`; `stop` and
`barcode` are optional per pick. The same fields work as CSV with a header
row, one pick per line (`route`/`pallet` are read from the first row):

```csv
sku,desc,qty,location,stop,route,pallet
104620,MARINARA SAUCE 6/#10,1,DA-03-1,4,12,A
```

There's no live WMS connection yet — jobs come in by paste/file until a
site's export or API is wired up, which is why the import side is kept
adapter-shaped: a new source only has to map onto these fields.

## Building the installer & auto-update

`python tools/build_installer.py` does the whole packaging run: it bundles
the app into a standalone `SafraConsole.exe` with
[PyInstaller](https://pyinstaller.org), wraps that into
`SafraConsole-Setup.exe` with [Inno Setup](https://jrsoftware.org/isinfo.php)
(per-user, Start-Menu shortcut, uninstaller), and writes the `latest.json`
update manifest. At the end it prints the `gh release create` command to
publish both as GitHub release assets. You'll need `pyinstaller` (from pip)
and Inno Setup 6.

Installed builds check the release feed each time they launch, and when a
newer version shows up, a banner links you to the new installer. Running it
updates in place — Inno Setup's Restart Manager closes the app and relaunches
it. A source checkout never updates itself; there, git is your update
channel.

## Architecture

```
ui/  (HTML / CSS / JS, Safra brand)
  │  SSE telemetry 10 Hz ↓   ·   JSON command POSTs ↑     (127.0.0.1 only)
server.py  (Python standard-library http.server)
  ├─ SimLink → sim.py            the built-in test robot
  ├─ TcpLink → host:port         protocol v1 over TCP → robot UART
  ├─ wms.py / labels.py          pick jobs → ZPL case tags + loader labels → printer (TCP 9100)
  └─ updater.py → release feed   (installed builds only)
```

Drive and jog intents are re-streamed at 15 Hz and dead-man to zero when you
let go, which mirrors the protocol's own streaming behavior.

## Scope & limits

- The sign-in screen is there to tag sessions with an operator identity, not
  to be a network security boundary — the app only binds to localhost. PINs
  are PBKDF2-hashed.
- No clamp (W-axis) controls yet. Protocol v1 doesn't have clamp frames until
  a planned firmware update adds them.
- No live video on field links yet; the simulated viewport is a model render,
  not a camera.
- The field-link path has been verified with a loopback test against the
  protocol, but not yet against physical hardware.
- Pick jobs are imported (JSON/CSV), not fetched live from a WMS, and the
  built-in label templates are generic placeholders — both are meant to be
  fitted to a site's real export and tag spec.

---

© Safra Robotics. See [LICENSE](LICENSE).
