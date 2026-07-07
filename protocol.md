# Safra UART protocol v1 — ESP32-S3 (HMI/comms) ⇄ Uno (motion/safety)

57,600 baud 8N1. ASCII lines, NMEA-style framing so it's debuggable in any
serial monitor. ESP32→Uno lines start `>`; Uno→ESP32 lines start `<`.
Frame: `>BODY*HH\n` where HH = two-digit hex XOR of every BODY character.
A bad checksum or malformed frame latches `FAULT(F_SERIAL)` — by design:
a corrupted link is a safety event, not a retry event.

**Watchdog:** ANY valid frame feeds the 300 ms watchdog. The ESP32 must send
`>HB*` at ≥10 Hz whenever no other traffic is flowing. Silence >300 ms ⇒
latching FAULT, Z brake clamps, throttles zero, hub brakes on.

## Commands (ESP32 → Uno)
| Frame | Meaning |
|---|---|
| `>HB*` | Heartbeat only. `<ACK,HB*` |
| `>DRV,<L>,<R>*` | Hub throttles, −100..100 each. Send at 10–20 Hz while driving. No ACK (rate). Reverse is gated by the brake→150 ms→reverse interlock; throttle is also capped to 40 % while the fork is above the creep line. |
| `>ZV,<mm_s>*` / `>YV,<mm_s>*` | Signed jog velocity. Firmware clamps to caps (Z 90, creep 25 above 300 mm fork height, halved when both axes move) and to soft walls. No ACK. |
| `>ZP,<mm>,<mm_s>*` / `>YP,<mm>,<mm_s>*` | Point move (record/replay primitive). Target clamped to soft range; decel near target. `<ACK,..*` |
| `>HOME*` | Z homing: seek Z-min at 15 mm/s, back off 5 mm, zero. `<ACK,HOME*` then `<ACK,HOMED*` on completion. Z is refused until homed. |
| `>STOP*` | Zero all motion intents (not a fault). |
| `>CLR*` | Clear a latched fault. Succeeds only with a healthy e-stop chain; `<NAK,CLR*` otherwise. |
| `>Q*` | Force an immediate status frame. |

## Telemetry (Uno → ESP32, 10 Hz)
`<ST,<state>,<fault>,<z>,<y>,<limits>,<estop>,<cap>*HH`

- state: 0 BOOT · 1 HOMING · 2 READY · 3 MOVING · 4 FAULT
- fault: 0 none · 1 e-stop · 2 watchdog · 3 limit · 4 serial · 5 bad-cmd
- z, y: position in 0.1 mm units (Z is carriage coordinate; fork ≈ carriage+70)
- limits: four digits, Zmin Zmax Ymin Ymax, 1 = tripped
- estop: 1 = chain healthy
- cap: current hub throttle cap % (0 in BOOT/HOMING/FAULT, 40 raised, 100 normal)

## Record/replay (console side)
A recorded primitive is simply a stored list of `ZP`/`YP`/`DRV` frames with
timestamps. The motion controller needs no changes: position moves + homing
+ step-count feedback are already the whole execution substrate.
