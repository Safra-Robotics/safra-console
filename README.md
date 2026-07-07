# Safra Operator Console

A branded desktop application for remotely operating [Safra
Robotics](https://safrarobotics.com) rigs: operator sign-in, a robot
picker with a built-in **simulated test robot**, and a drive / lift /
reach console with live telemetry, remappable keyboard + game-controller
controls, a first-person 3D view of the simulated robot, and a session
log.

It runs against a **built-in simulated robot** with zero hardware, so you
can try the whole interface immediately.

## Download

Grab the latest packaged Windows build from the
[**Releases**](https://github.com/Safra-Robotics/safra-operator-console/releases/latest)
page — unzip anywhere and run `SafraConsole.bat`. No install, no Python
required; the app checks for updates on its own and installs them in place.

## Run from source

Requires Python 3.10+ (standard library only — no `pip install`):

```
python safra_console.py
```

The backend binds `127.0.0.1:8973` and opens a window via, in order of
preference: [pywebview](https://pywebview.flowrl.com/) if installed →
Microsoft Edge in `--app` mode (a chromeless app window) → your default
browser. Flags: `--port N`, `--serve-only`.

First run enrolls the first operator (name, callsign, PIN). Operators,
robots, and control bindings are stored locally in `data/` (dev checkout)
or `%LOCALAPPDATA%\SafraConsole\data` (packaged install).

## Robots

- **Test Robot (Simulated)** — built in, always available, no hardware. An
  in-process model of the [`protocol.md`](protocol.md) v1 contract and the
  pilot rig's published motion limits: `BOOT → HOMING → READY / MOVING /
  FAULT` states, Z refused until homed, seek/back-off/zero homing, a
  90 mm/s lift with a 25 mm/s creep above the 300 mm fork line (caps halved
  when two axes move together), a 40 % drive-throttle cap with the fork
  raised, a brake→150 ms→reverse interlock, latched faults that clear only
  on a healthy e-stop chain, and streamed drive intents that dead-man to
  zero. The E-STOP button trips the simulated safety chain so you can
  rehearse the trip → reset → clear cycle.
- **Field robots (TCP)** — add a name + `host:port`. The console speaks
  `protocol.md` v1 frames (`>BODY*HH`, XOR checksum) over TCP and expects
  the robot's onboard computer to expose the motion-controller UART on
  that port. It streams heartbeats at ~12 Hz to feed the controller's
  300 ms watchdog and parses `<ST` telemetry at 10 Hz. On a field link the
  red button sends **STOP** — the physical e-stop chain is hardware-only
  by design.

## Controls

**MAP CONTROLS** (in the DRIVE panel) opens a binding table. Every action
— forward / reverse / turns, fork up / down, reach out / in, stop,
e-stop, home, and a creep-hold — can hold several bindings at once:
keyboard keys and/or game-controller inputs (standard-mapping Gamepad API;
Xbox button names shown, analog sticks get a deadzone and analog scaling).
Click ＋ on a row, then press a key / button or push a stick to capture;
click a chip to remove it. Bindings persist per machine.

Defaults: **WASD / arrows** or the **left stick** drive · **R / F** or the
**right stick** raise/lower the fork · **T / G** or the **D-pad** extend
reach · **Space / B** stop · **X** e-stop · **H / Y** home · **Shift / LB**
creep. An on-screen joystick and jog buttons mirror the keys.

## Simulated view

The simulated robot's viewport is a first-person 3D render from the mast
camera — a small software pinhole projection on a 2D canvas, no external
libraries: floor grid, walls, racking with cases, a pallet drop zone, and
the robot's own fork blades rising through the frame as you lift. Field
robots show a placeholder until live video ships.

## Packaging & auto-update

`python tools/build_release.py` produces a portable Windows build (an
embeddable CPython runtime + the app + a launcher) plus a `latest.json`
update manifest, and prints the `gh release create` command to publish
them. Packaged installs check the release feed at startup, offer an
in-app **Install** for a newer version (download → SHA-256 verify → stage),
and the launcher swaps the new version in on next start. Running from a
source checkout never self-updates.

## Architecture

```
ui/  (HTML / CSS / JS, Safra brand)
  │  SSE telemetry 10 Hz ↓   ·   JSON command POSTs ↑     (127.0.0.1 only)
server.py  (Python standard-library http.server)
  ├─ SimLink → sim.py            the built-in test robot
  ├─ TcpLink → host:port         protocol v1 over TCP → robot UART
  └─ updater.py → release feed   (packaged installs only)
```

Drive and jog intents are re-streamed at 15 Hz and dead-man to zero on
release, mirroring the protocol's own streaming semantics.

## Scope & limits

- The sign-in screen is an **operator-identity layer** for session logging,
  not a network security boundary — the app binds to localhost only. PINs
  are PBKDF2-hashed.
- No clamp (W-axis) controls yet — protocol v1 has no clamp frames until a
  planned firmware update.
- No live video on field links yet; the simulated viewport is a model
  render, not a camera.
- The field-link path is verified by a loopback test against the protocol,
  not yet against physical hardware.

---

© Safra Robotics. See [LICENSE](LICENSE).
