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

# --- world: static obstacles (collision + render) & dynamic boxes -------------
ROBOT_R = 0.42            # robot footprint radius, m (drive collision)
WALL_TH = 0.08           # perimeter wall thickness, m
FORK_CARRY_X = 0.62      # forward offset (m) of the fork carry point, + reach
PICK_UNDER = 0.14        # forks slide under a box within this height of its base

# x0, y0, x1, y1, height, kind  (collision uses the footprint; render uses all)
OBSTACLES = [
    (0.0, 0.0, WORLD_W, WALL_TH, 2.6, "wall"),                  # south
    (0.0, WORLD_H - WALL_TH, WORLD_W, WORLD_H, 2.6, "wall"),    # north
    (0.0, 0.0, WALL_TH, WORLD_H, 2.6, "wall"),                  # west
    (WORLD_W - WALL_TH, 0.0, WORLD_W, WORLD_H, 2.6, "wall"),    # east
    (0.8, WORLD_H - WALL_TH - 0.9, 5.2, WORLD_H - WALL_TH, 2.0, "rack"),   # N rack bay 1
    (6.6, WORLD_H - WALL_TH - 0.9, 11.1, WORLD_H - WALL_TH, 2.0, "rack"),  # N rack bay 2
]

# dynamic boxes on the floor (x,y = centre; w,d = footprint; h = height)
INIT_BOXES = [
    {"id": "palletA", "x": 4.7, "y": 2.1, "w": 1.15, "d": 0.95, "h": 0.66, "kind": "pallet"},
    {"id": "crateB",  "x": 8.3, "y": 2.7, "w": 0.90, "d": 0.90, "h": 0.60, "kind": "crate"},
    {"id": "crateC",  "x": 2.5, "y": 5.2, "w": 0.85, "d": 0.85, "h": 0.80, "kind": "crate"},
    {"id": "palletD", "x": 9.7, "y": 5.0, "w": 1.05, "d": 1.00, "h": 0.70, "kind": "pallet"},
]


def _closest_on_aabb(cx, cy, a):
    return min(max(cx, a[0]), a[2]), min(max(cy, a[1]), a[3])


def _aabb_of(b):
    return (b["x"] - b["w"] / 2, b["y"] - b["d"] / 2,
            b["x"] + b["w"] / 2, b["y"] + b["d"] / 2)


def _separate(a, o, bx, by):
    """Min-translation separation of AABB `a` (centred at bx,by) out of AABB `o`.
    Returns the adjusted centre (bx,by)."""
    ox = min(a[2], o[2]) - max(a[0], o[0])
    oy = min(a[3], o[3]) - max(a[1], o[1])
    if ox <= 0 or oy <= 0:
        return bx, by
    acx, acy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    ocx, ocy = (o[0] + o[2]) / 2, (o[1] + o[3]) / 2
    if ox < oy:
        return bx + (ox if acx >= ocx else -ox), by
    return bx, by + (oy if acy >= ocy else -oy)


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
        self.boxes = [dict(b, z=0.0, carried=False) for b in INIT_BOXES]
        self._carried = None          # id of the box riding the forks, or None
        self._engaged = None          # id of the box the forks are currently under
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

    # --- world / boxes / collision --------------------------------------------

    def cmd_move_box(self, box_id, x, y):
        """Reposition a box (mouse-drag from the console). Ignored while the box
        rides the forks. The box is settled out of walls/racks/other boxes."""
        with self._lock:
            b = self._box(box_id)
            if b is None or b["carried"]:
                return False
            b["x"], b["y"], b["z"] = float(x), float(y), 0.0
            self._settle_box(b)
            return True

    def _box(self, bid):
        for b in self.boxes:
            if b["id"] == bid:
                return b
        return None

    def _resolve_static(self, x, y):
        """Push the robot circle out of any wall/rack AABB (slides along faces)."""
        for _ in range(2):
            for o in OBSTACLES:
                qx, qy = _closest_on_aabb(x, y, o)
                dx, dy = x - qx, y - qy
                d2 = dx * dx + dy * dy
                if d2 < ROBOT_R * ROBOT_R:
                    d = math.sqrt(d2)
                    if d > 1e-6:
                        x += dx / d * (ROBOT_R - d)
                        y += dy / d * (ROBOT_R - d)
                    else:                       # centre inside: eject upward
                        y = o[3] + ROBOT_R
        return x, y

    def _collide_boxes(self, x, y, h):
        """Robot vs dynamic boxes: shove a box aside, or (if it's jammed) stop
        the robot. Boxes the forks are sliding under are skipped so you can
        drive under a pallet to pick it up."""
        for b in self.boxes:
            # skip the box on the forks, or the one the forks are engaged under
            # (raising the forks should lift it, not shove it)
            if b["carried"] or b["id"] == self._engaged:
                continue
            a = _aabb_of(b)
            qx, qy = _closest_on_aabb(x, y, a)
            dx, dy = x - qx, y - qy
            if dx * dx + dy * dy >= ROBOT_R * ROBOT_R:
                continue
            if self._can_slide_under(b, x, y, h):
                continue
            d = math.sqrt(dx * dx + dy * dy)
            if d > 1e-6:
                nx, ny, pen = dx / d, dy / d, ROBOT_R - d
            else:                               # centre inside: shove it forward
                nx, ny, pen = -math.cos(h), -math.sin(h), ROBOT_R
            b["x"] -= nx * pen                  # push the box away from the robot
            b["y"] -= ny * pen
            self._settle_box(b)
            a = _aabb_of(b)                     # if it couldn't move, stop the robot
            qx, qy = _closest_on_aabb(x, y, a)
            dx, dy = x - qx, y - qy
            d2 = dx * dx + dy * dy
            if d2 < ROBOT_R * ROBOT_R:
                d = math.sqrt(d2)
                if d > 1e-6:
                    x += dx / d * (ROBOT_R - d)
                    y += dy / d * (ROBOT_R - d)
                else:
                    x -= nx * ROBOT_R
                    y -= ny * ROBOT_R
        return x, y

    def _can_slide_under(self, b, x, y, h):
        fz = self.fork_mm / 1000.0
        if fz > b["z"] + PICK_UNDER:            # forks above the base -> no
            return False
        reach = self.y / 1000.0
        cx = x + math.cos(h) * (FORK_CARRY_X + reach)
        cy = y + math.sin(h) * (FORK_CARRY_X + reach)
        a = _aabb_of(b)
        return (a[0] - 0.25 <= cx <= a[2] + 0.25) and (a[1] - 0.25 <= cy <= a[3] + 0.25)

    def _settle_box(self, b):
        """Keep a box inside the arena and out of walls/racks/other boxes."""
        b["x"] = max(b["w"] / 2, min(WORLD_W - b["w"] / 2, b["x"]))
        b["y"] = max(b["d"] / 2, min(WORLD_H - b["d"] / 2, b["y"]))
        for _ in range(3):
            a = _aabb_of(b)
            for o in OBSTACLES:
                b["x"], b["y"] = _separate(a, o, b["x"], b["y"])
                a = _aabb_of(b)
            for other in self.boxes:
                if other is b or other["carried"]:
                    continue
                b["x"], b["y"] = _separate(a, _aabb_of(other), b["x"], b["y"])
                a = _aabb_of(b)

    def _support_z(self, b):
        """Resting height for a box at its (x,y): floor, or the top of a box
        under it (so loads can be stacked)."""
        rest, a = 0.0, _aabb_of(b)
        for other in self.boxes:
            if other is b or other["carried"]:
                continue
            oa = _aabb_of(other)
            if a[0] < oa[2] and a[2] > oa[0] and a[1] < oa[3] and a[3] > oa[1]:
                rest = max(rest, other["z"] + other["h"])
        return rest

    def _update_fork_carry(self, x, y, h):
        """Forklift logic: forks low + under a box latches 'engaged'; raising
        them lifts it; lowering onto a surface sets it down."""
        fz = self.fork_mm / 1000.0
        reach = self.y / 1000.0
        cx = x + math.cos(h) * (FORK_CARRY_X + reach)
        cy = y + math.sin(h) * (FORK_CARRY_X + reach)
        if self._carried is not None:
            b = self._box(self._carried)
            if b is None:
                self._carried = None
                return
            b["x"], b["y"], b["z"] = cx, cy, fz
            rest = self._support_z(b)
            # release when the forks are lowered near the resting surface; the
            # margin sits above the 70 mm fork floor and below the lift point so
            # setting down doesn't immediately re-grab.
            if fz <= rest + 0.12:
                b["z"], b["carried"] = rest, False
                self._carried = self._engaged = None
            return
        under = None
        for b in self.boxes:
            if b["carried"]:
                continue
            a = _aabb_of(b)
            if a[0] <= cx <= a[2] and a[1] <= cy <= a[3] and fz <= b["z"] + PICK_UNDER:
                under = b
                break
        if under is not None:
            self._engaged = under["id"]
        if self._engaged is not None:
            b = self._box(self._engaged)
            if b is None or b["carried"]:
                self._engaged = None
                return
            a = _aabb_of(b)
            inside = a[0] <= cx <= a[2] and a[1] <= cy <= a[3]
            if inside and fz > b["z"] + PICK_UNDER + 0.02:   # raised -> lift it
                b["carried"] = True
                self._carried = b["id"]
            elif not inside:
                self._engaged = None

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
        h = math.atan2(math.sin(h + w * dt), math.cos(h + w * dt))
        x += v * math.cos(h) * dt
        y += v * math.sin(h) * dt
        # collide with walls, racks and boxes (boxes get shoved unless the forks
        # are sliding under them); then keep the carried box on the forks
        x, y = self._resolve_static(x, y)
        x, y = self._collide_boxes(x, y, h)
        x, y = self._resolve_static(x, y)
        x = max(ROBOT_R, min(WORLD_W - ROBOT_R, x))
        y = max(ROBOT_R, min(WORLD_H - ROBOT_R, y))
        self.pose = [x, y, h]
        self.speed = abs(v)
        self._update_fork_carry(x, y, h)

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
                "carried": self._carried,
                "boxes": [{"id": b["id"], "x": round(b["x"], 3), "y": round(b["y"], 3),
                           "z": round(b["z"], 3), "w": b["w"], "d": b["d"], "h": b["h"],
                           "kind": b["kind"], "carried": b["carried"]} for b in self.boxes],
                "world": {"w": WORLD_W, "h": WORLD_H, "wall_th": WALL_TH,
                          "robot_r": ROBOT_R,
                          "obstacles": [{"x0": o[0], "y0": o[1], "x1": o[2], "y1": o[3],
                                         "h": o[4], "kind": o[5]} for o in OBSTACLES]},
            }

    def close(self):
        self._run = False
