"""Simulated test robot for the Safra operator console.

Implements the observable behavior of protocol.md v1 + the pilot rig's
published geometry and motion limits, so the console exercises the same
contract it will speak to the real rig:

  states  0 BOOT / 1 HOMING / 2 READY / 3 MOVING / 4 FAULT
  faults  0 none / 1 e-stop / 2 watchdog / 3 limit / 4 serial / 5 bad-cmd
  Z jog cap 90 mm/s, creep 25 mm/s above the 300 mm fork line, caps halved
  when both axes move; hub throttle cap 40% above the creep line; reverse
  gated by brake -> 150 ms -> reverse; Z refused until homed; a latched
  fault clears only with a healthy e-stop chain.

Protocol v1 has no W (clamp) frames yet — those arrive in a planned
firmware update, so the sim exposes none either.
"""

import math
import threading
import time

# --- pilot rig geometry & motion limits --------------------------------------
Z_TRAVEL = 800.0          # carriage soft range, mm
FORK_OFFSET = 70.0        # fork = carriage + 70 (protocol.md telemetry note)
Y_TRAVEL = 300.0          # reach module, mm
Z_CAP = 90.0              # mm/s
Y_CAP = 60.0              # mm/s
CREEP_CAP = 25.0          # mm/s above the creep line
CREEP_FORK_MM = 300.0     # fork height that triggers creep/throttle caps
HOME_SEEK = 15.0          # mm/s
HOME_BACKOFF = 5.0        # mm
DRIVE_CAP_RAISED = 40     # % hub throttle while fork above creep line
REVERSE_HOLD_S = 0.150    # brake -> 150 ms -> reverse interlock
INPUT_DEADMAN_S = 0.300   # DRV/ZV/YV are streamed; stale intent -> zero
TRACK_M = 0.4932          # wheel track (2 x 246.6 mm half-track)
V_MAX = 1.0               # m/s at 100% throttle
WORLD_W, WORLD_H = 12.0, 8.0   # sim floor, m

BOOT, HOMING, READY, MOVING, FAULT = range(5)
F_NONE, F_ESTOP, F_WATCHDOG, F_LIMIT, F_SERIAL, F_BADCMD = range(6)

TICK = 0.02  # 50 Hz physics


class SimRobot:
    """Threaded robot model. All mutators are thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = BOOT
        self.fault = F_NONE
        self.homed = False
        self.estop_ok = True          # chain healthy
        # Z carriage: raw coordinate is distance above the Z-min switch;
        # zero is set HOME_BACKOFF above the switch, so soft z = raw - backoff.
        self._raw_z = 40.0
        self._z_zero = 0.0            # raw value that reads as z=0 once homed
        self.z_vel = 0.0
        self.y = 0.0
        self.y_vel = 0.0
        self._jog_z = 0.0
        self._jog_y = 0.0
        self._jog_t = 0.0
        self._target_z = None         # (mm, mm/s) point moves
        self._target_y = None
        self._home_phase = 0
        self._drive_cmd = [0.0, 0.0]  # commanded throttle %
        self._drive_t = 0.0
        self._wheel = [0.0, 0.0]      # effective throttle % after lag/interlock
        self._rev_hold = [0.0, 0.0]   # reverse-interlock brake timers
        self.pose = [3.0, 4.0, 0.0]   # x m, y m, heading rad
        self.speed = 0.0
        self.soc = 100.0
        self._events = []
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # --- console-facing commands (mirror protocol.md frames) -----------------

    def cmd_drive(self, l, r):
        with self._lock:
            if self.state in (BOOT, HOMING, FAULT):
                return
            self._drive_cmd = [max(-100.0, min(100.0, float(l))),
                               max(-100.0, min(100.0, float(r)))]
            self._drive_t = time.monotonic()

    def cmd_jog(self, axis, v):
        with self._lock:
            if self.state == FAULT or self.state == HOMING:
                return
            if axis == "z" and not self.homed:
                self._event("NAK ZV — Z refused until homed")
                return
            if axis == "z":
                self._jog_z = float(v)
            else:
                self._jog_y = float(v)
            self._jog_t = time.monotonic()

    def cmd_point(self, axis, mm, v):
        with self._lock:
            if self.state in (HOMING, FAULT):
                return False
            if axis == "z":
                if not self.homed:
                    self._event("NAK ZP — Z refused until homed")
                    return False
                self._target_z = (max(0.0, min(Z_TRAVEL, float(mm))),
                                  max(1.0, min(Z_CAP, abs(float(v)))))
            else:
                self._target_y = (max(0.0, min(Y_TRAVEL, float(mm))),
                                  max(1.0, min(Y_CAP, abs(float(v)))))
            return True

    def cmd_home(self):
        with self._lock:
            if self.state == FAULT:
                return False
            self._stop_intents()
            self.state = HOMING
            self._home_phase = 1
            self._event("HOMING — seek Z-min at 15 mm/s")
            return True

    def cmd_stop(self):
        with self._lock:
            self._stop_intents()
            self._event("STOP — motion intents zeroed")

    def cmd_clr(self):
        with self._lock:
            if not self.estop_ok:
                self._event("NAK CLR — e-stop chain open")
                return False
            if self.state == FAULT:
                self.fault = F_NONE
                self.state = READY if self.homed else BOOT
                self._event("fault cleared")
            return True

    # --- sim-only inputs (the physical chain on the real rig) ----------------

    def trip_estop(self):
        with self._lock:
            self.estop_ok = False
            self._latch(F_ESTOP, "E-STOP — chain open, Z brake clamped, throttles zero")

    def reset_estop(self):
        with self._lock:
            self.estop_ok = True
            self._event("e-stop chain reset (fault stays latched until CLR)")

    # --- internals ------------------------------------------------------------

    def _stop_intents(self):
        self._drive_cmd = [0.0, 0.0]
        self._jog_z = self._jog_y = 0.0
        self._target_z = self._target_y = None

    def _latch(self, code, msg):
        self.fault = code
        self.state = FAULT
        self._stop_intents()
        self._wheel = [0.0, 0.0]
        self.z_vel = self.y_vel = 0.0
        self._event(msg)

    def _event(self, msg):
        self._events.append(msg)
        del self._events[:-40]

    def drain_events(self):
        with self._lock:
            out, self._events = self._events, []
            return out

    def _axis_step(self, pos, vel, jog, target, lo, hi, cap, dt):
        """One axis tick: point move wins over jog; decel walls at the soft range."""
        if target is not None:
            goal, spd = target
            err = goal - pos
            if abs(err) < 0.15:
                return goal, 0.0, None
            v = math.copysign(min(spd, cap, abs(err) * 4.0), err)  # decel near target
            return pos + v * dt, v, target
        v = max(-cap, min(cap, jog))
        npos = pos + v * dt
        if npos <= lo:
            npos, v = lo, 0.0
        elif npos >= hi:
            npos, v = hi, 0.0
        return npos, v, None

    def _loop(self):
        last = time.monotonic()
        while self._run:
            time.sleep(TICK)
            now = time.monotonic()
            dt, last = now - last, now
            with self._lock:
                self._tick(now, dt)

    def _tick(self, now, dt):
        if self.state == FAULT:
            self.speed = 0.0
            return

        # deadman on streamed intents (protocol: DRV at 10-20 Hz while driving)
        if now - self._drive_t > INPUT_DEADMAN_S:
            self._drive_cmd = [0.0, 0.0]
        if now - self._jog_t > INPUT_DEADMAN_S:
            self._jog_z = self._jog_y = 0.0

        fork = self.fork_mm
        creep = fork > CREEP_FORK_MM

        # homing sequence: seek Z-min, back off 5 mm, zero
        if self.state == HOMING:
            if self._home_phase == 1:
                self._raw_z -= HOME_SEEK * dt
                if self._raw_z <= 0.0:
                    self._raw_z = 0.0
                    self._home_phase = 2
            elif self._home_phase == 2:
                self._raw_z += HOME_SEEK * dt
                if self._raw_z >= HOME_BACKOFF:
                    self._raw_z = HOME_BACKOFF
                    self._z_zero = HOME_BACKOFF
                    self.homed = True
                    self._home_phase = 0
                    self.state = READY
                    self._event("HOMED — Z zeroed 5 mm above the switch")
            return

        # Z / Y axes — caps halved when both axes are commanded (protocol.md)
        z_cap = CREEP_CAP if creep else Z_CAP
        y_cap = CREEP_CAP if creep else Y_CAP
        both = (self._jog_z or self._target_z) and (self._jog_y or self._target_y)
        if both:
            z_cap, y_cap = z_cap / 2.0, y_cap / 2.0
        if self.homed:
            z = self.z_mm
            z, self.z_vel, self._target_z = self._axis_step(
                z, self.z_vel, self._jog_z, self._target_z, 0.0, Z_TRAVEL, z_cap, dt)
            self._raw_z = z + self._z_zero
        self.y, self.y_vel, self._target_y = self._axis_step(
            self.y, self.y_vel, self._jog_y, self._target_y, 0.0, Y_TRAVEL, y_cap, dt)

        # hub throttles: cap, reverse interlock, first-order lag
        cap = float(self.throttle_cap)
        for i in (0, 1):
            cmd = max(-cap, min(cap, self._drive_cmd[i]))
            if cmd < -0.5 and self._wheel[i] > 0.5:      # brake before reverse
                self._rev_hold[i] = REVERSE_HOLD_S
            if self._rev_hold[i] > 0.0:
                self._rev_hold[i] -= dt
                cmd = 0.0
            self._wheel[i] += (cmd - self._wheel[i]) * min(1.0, dt / 0.15)

        # differential drive
        vl = self._wheel[0] / 100.0 * V_MAX
        vr = self._wheel[1] / 100.0 * V_MAX
        v = (vl + vr) / 2.0
        w = (vr - vl) / TRACK_M
        x, y, h = self.pose
        h += w * dt
        x += v * math.cos(h) * dt
        y += v * math.sin(h) * dt
        x = max(0.5, min(WORLD_W - 0.5, x))
        y = max(0.5, min(WORLD_H - 0.5, y))
        self.pose = [x, y, math.atan2(math.sin(h), math.cos(h))]
        self.speed = abs(v)

        moving = (abs(self._wheel[0]) > 0.5 or abs(self._wheel[1]) > 0.5
                  or abs(self.z_vel) > 0.05 or abs(self.y_vel) > 0.05)
        self.state = MOVING if moving else READY

        drain = 0.02 if moving else 0.002
        self.soc = max(0.0, self.soc - drain * dt)

    # --- telemetry -------------------------------------------------------------

    @property
    def z_mm(self):
        return self._raw_z - self._z_zero

    @property
    def fork_mm(self):
        return self.z_mm + FORK_OFFSET

    @property
    def throttle_cap(self):
        if self.state in (BOOT, HOMING, FAULT):
            return 0
        return DRIVE_CAP_RAISED if self.fork_mm > CREEP_FORK_MM else 100

    def snapshot(self):
        with self._lock:
            # hardware switches sit beyond the soft walls; in normal soft-range
            # ops nothing trips — only the Z-min switch during the homing seek
            limits = "{}000".format(1 if self._raw_z <= 0.01 else 0)
            volts = 46.4 + 12.0 * self.soc / 100.0
            return {
                "sim": True,
                "state": self.state,
                "fault": self.fault,
                "homed": self.homed,
                "z_mm": round(self.z_mm, 1),
                "y_mm": round(self.y, 1),
                "fork_mm": round(self.fork_mm, 1),
                "limits": limits,
                "estop_ok": self.estop_ok,
                "cap": self.throttle_cap,
                "wheels": [round(self._wheel[0], 1), round(self._wheel[1], 1)],
                "pose": [round(self.pose[0], 3), round(self.pose[1], 3),
                         round(self.pose[2], 4)],
                "speed": round(self.speed, 3),
                "soc": round(self.soc, 1),
                "volts": round(volts, 1),
            }

    def close(self):
        self._run = False
