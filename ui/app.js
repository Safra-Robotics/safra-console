/* Safra Operator Console front-end.
   Views: login (operator identity) -> robots -> console.
   Telemetry arrives over SSE at 10 Hz; drive/jog intents are re-streamed at
   15 Hz while active (mirrors protocol.md: DRV/ZV/YV are streamed, stale
   intents dead-man to zero on the robot side).

   Controls are remappable (MAP CONTROLS in the DRIVE panel): every action
   slot holds a list of bindings — a keyboard key, a gamepad button, or a
   gamepad axis direction (standard-mapping Gamepad API; Xbox names shown).
   Bindings persist server-side in data/bindings.json (per machine). */

"use strict";

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* ---------- control bindings ---------- */
// slot -> [{key}, {btn}, {axis, sign}]; axis slots come in +/- pairs,
// button slots (stop/estop/home) fire on edges, creep is a hold.
const DEFAULT_BINDINGS = {
  slots: {
    "drive+": [{ key: "w" }, { key: "arrowup" }, { axis: 1, sign: -1 }],
    "drive-": [{ key: "s" }, { key: "arrowdown" }, { axis: 1, sign: 1 }],
    "turn+":  [{ key: "d" }, { key: "arrowright" }, { axis: 0, sign: 1 }],
    "turn-":  [{ key: "a" }, { key: "arrowleft" }, { axis: 0, sign: -1 }],
    "fork+":  [{ key: "r" }, { axis: 3, sign: -1 }],
    "fork-":  [{ key: "f" }, { axis: 3, sign: 1 }],
    "reach+": [{ key: "t" }, { btn: 12 }],
    "reach-": [{ key: "g" }, { btn: 13 }],
    "stop":   [{ key: " " }, { btn: 1 }],
    "estop":  [{ key: "x" }, { btn: 2 }],
    "home":   [{ key: "h" }, { btn: 3 }],
    "creep":  [{ key: "shift" }, { btn: 4 }],
  },
};
const SLOT_GROUPS = [
  ["DRIVE", [["drive+", "FORWARD"], ["drive-", "REVERSE"], ["turn+", "TURN RIGHT"], ["turn-", "TURN LEFT"]]],
  ["LIFT / REACH", [["fork+", "FORK UP"], ["fork-", "FORK DOWN"], ["reach+", "REACH OUT"], ["reach-", "REACH IN"]]],
  ["ACTIONS", [["stop", "STOP"], ["estop", "E-STOP"], ["home", "HOME Z"], ["creep", "CREEP (HOLD)"]]],
];
const BUTTON_SLOTS = ["stop", "estop", "home"];
const AXIS_NAMES = ["LX", "LY", "RX", "RY"];
const BTN_NAMES = { 0: "A", 1: "B", 2: "X", 3: "Y", 4: "LB", 5: "RB", 6: "LT", 7: "RT",
  8: "BACK", 9: "START", 10: "LS", 11: "RS", 12: "DPAD↑", 13: "DPAD↓", 14: "DPAD←", 15: "DPAD→" };
const KEY_NAMES = { " ": "SPACE", arrowup: "↑", arrowdown: "↓", arrowleft: "←", arrowright: "→" };
const DEADZONE = 0.16;

const state = {
  token: null,
  operator: null,
  robots: [],
  robot: null,
  telem: null,
  es: null,
  lastEvtId: 0,
  drive: { x: 0, y: 0, active: false },   // on-screen joystick, x=turn y=fwd
  keys: new Set(),
  bindings: structuredClone(DEFAULT_BINDINGS),
  capture: null,                          // {slot, axesBase, btnBase}
  btnPrev: {},                            // gamepad edge detection per slot
  lastSent: { l: 0, r: 0, zv: 0, yv: 0 },
};

/* ---------- api ---------- */
async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (state.token) opts.headers["X-Session"] = state.token;
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const cmd = (c) => api("/api/cmd", c).catch(() => {});

function setView(v) { document.body.dataset.view = v; }

/* ---------- login / operator identity ---------- */
async function initLogin() {
  const boot = await api("/api/bootstrap");
  $("app-version").textContent = "v" + boot.version;
  $("login-form").hidden = boot.needs_setup;
  $("setup-form").hidden = !boot.needs_setup;
  if (!boot.needs_setup) {
    const sel = $("login-name");
    sel.innerHTML = "";
    for (const op of boot.operators) {
      const o = document.createElement("option");
      o.value = op.name;
      o.textContent = op.callsign ? `${op.name} — ${op.callsign}` : op.name;
      sel.appendChild(o);
    }
  }
  setView("login");
}

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("setup-error").textContent = "";
  try {
    await api("/api/setup", {
      name: $("setup-name").value,
      callsign: $("setup-callsign").value,
      pin: $("setup-pin").value,
    });
    await initLogin();
  } catch (err) { $("setup-error").textContent = err.message; }
});

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const r = await api("/api/login", { name: $("login-name").value, pin: $("login-pin").value });
    state.token = r.token;
    state.operator = r.operator;
    $("login-pin").value = "";
    const badge = state.operator.callsign
      ? `${state.operator.name} · ${state.operator.callsign}` : state.operator.name;
    $("robots-op-badge").textContent = "OPERATOR  " + badge;
    $("console-op-badge").textContent = "OPERATOR  " + badge;
    openStream();
    loadBindings();
    refreshUpdateStatus();
    await showRobots();
  } catch (err) { $("login-error").textContent = err.message; }
});

$("btn-logout").addEventListener("click", async () => {
  await api("/api/logout", {}).catch(() => {});
  closeStream();
  state.token = state.operator = null;
  $("update-banner").hidden = true;
  initLogin();
});

/* ---------- robots ---------- */
async function showRobots() {
  $("robots-error").textContent = "";
  const r = await api("/api/robots");
  state.robots = r.robots;
  renderRobots();
  setView("robots");
}

function renderRobots() {
  const grid = $("robot-grid");
  grid.innerHTML = "";
  for (const rb of state.robots) {
    const card = document.createElement("div");
    card.className = "panel robot-card" + (rb.kind === "sim" ? " robot-card--sim" : "");
    const chip = rb.kind === "sim"
      ? '<span class="chip chip-sim">SIMULATED</span>'
      : '<span class="chip">FIELD · TCP</span>';
    card.innerHTML = `
      <div class="robot-card-head"><div class="robot-card-name"></div>${chip}</div>
      <div class="robot-card-addr">${rb.kind === "sim" ? "in-process · protocol v1 model" : `${rb.host}:${rb.port}`}</div>
      <div class="robot-card-note"></div>
      <div class="robot-card-actions">
        <button class="btn btn-cta">Connect</button>
        ${rb.kind === "sim" ? "" : '<button class="btn btn-ghost" data-act="edit">Edit</button><button class="btn btn-ghost" data-act="del">Remove</button>'}
      </div>`;
    card.querySelector(".robot-card-name").textContent = rb.name;
    card.querySelector(".robot-card-note").textContent = rb.note || "";
    card.querySelector(".btn-cta").addEventListener("click", () => connectRobot(rb));
    const edit = card.querySelector('[data-act="edit"]');
    if (edit) edit.addEventListener("click", () => {
      $("robot-id").value = rb.id; $("robot-name").value = rb.name;
      $("robot-host").value = rb.host; $("robot-port").value = rb.port;
      $("robot-note").value = rb.note || "";
      $("robot-form-submit").textContent = "Save";
      $("robot-form-cancel").hidden = false;
    });
    const del = card.querySelector('[data-act="del"]');
    if (del) del.addEventListener("click", async () => {
      await api("/api/robots/delete", { id: rb.id });
      showRobots();
    });
    grid.appendChild(card);
  }
}

function resetRobotForm() {
  for (const f of ["robot-id", "robot-name", "robot-host", "robot-port", "robot-note"]) $(f).value = "";
  $("robot-form-submit").textContent = "Add";
  $("robot-form-cancel").hidden = true;
}
$("robot-form-cancel").addEventListener("click", resetRobotForm);
$("robot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("robots-error").textContent = "";
  try {
    await api("/api/robots", {
      id: $("robot-id").value || undefined,
      name: $("robot-name").value, host: $("robot-host").value,
      port: $("robot-port").value, note: $("robot-note").value,
    });
    resetRobotForm();
    showRobots();
  } catch (err) { $("robots-error").textContent = err.message; }
});

async function connectRobot(rb) {
  $("robots-error").textContent = "";
  try {
    await api("/api/connect", { id: rb.id });
    state.robot = rb;
    $("console-robot-name").textContent = rb.name.toUpperCase();
    $("hud-feed").textContent = rb.kind === "sim" ? "SIM FEED · CAM-1" : "FIELD LINK";
    $("no-video").hidden = rb.kind === "sim";
    $("sim-canvas").style.display = rb.kind === "sim" ? "block" : "none";
    $("event-log").innerHTML = "";
    state.lastEvtId = 0;
    setView("console");
    resizeCanvas();
  } catch (err) { $("robots-error").textContent = err.message; }
}

$("btn-disconnect").addEventListener("click", async () => {
  await api("/api/disconnect", {}).catch(() => {});
  state.robot = null;
  showRobots();
});

/* ---------- telemetry stream ---------- */
function openStream() {
  closeStream();
  const es = new EventSource("/api/stream?token=" + encodeURIComponent(state.token));
  es.onmessage = (m) => {
    const d = JSON.parse(m.data);
    state.telem = d.telem;
    for (const e of d.events || []) {
      if (e.id > state.lastEvtId) { state.lastEvtId = e.id; appendEvent(e); }
    }
    if (document.body.dataset.view === "console") {
      renderTelem(d);
      if (!d.connected && state.robot) { state.robot = null; showRobots(); }
    }
  };
  state.es = es;
}
function closeStream() { if (state.es) { state.es.close(); state.es = null; } }

function appendEvent(e) {
  const log = $("event-log");
  const div = document.createElement("div");
  div.className = "evt evt-" + e.kind;
  div.innerHTML = `${e.ts} <b></b>`;
  div.querySelector("b").textContent = e.msg;
  log.appendChild(div);
  while (log.children.length > 120) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

const STATE_CHIP = { BOOT: "chip", HOMING: "chip chip-warn", READY: "chip chip-ok", MOVING: "chip chip-ok", FAULT: "chip chip-fault" };

function renderTelem(d) {
  const t = d.telem;
  if (!t) return;
  $("chip-state").textContent = t.state_name;
  $("chip-state").className = STATE_CHIP[t.state_name] || "chip";
  const faulted = t.state_name === "FAULT";
  $("chip-fault").hidden = !faulted;
  $("chip-fault").textContent = "FAULT · " + t.fault_name;

  $("t-state").textContent = t.state_name + (t.sim && !t.homed ? " · UNHOMED" : "");
  $("t-fault").textContent = t.fault_name;
  $("t-fault").className = faulted ? "bad" : "";
  $("t-estop").textContent = t.estop_ok ? "HEALTHY" : "OPEN";
  $("t-estop").className = t.estop_ok ? "ok" : "bad";
  $("t-cap").textContent = t.cap + "%";
  $("t-cap").className = t.cap === 100 ? "" : (t.cap === 0 ? "bad" : "warn");
  $("t-batt").textContent = t.soc != null ? `${t.soc}% · ${t.volts}V` : (d.robot && d.robot.kind === "tcp" ? "n/a (v1)" : "—");
  $("t-limits").textContent = t.limits;
  $("t-limits").className = t.limits !== "0000" ? "warn" : "";

  $("g-fork").textContent = Math.round(t.fork_mm);
  $("g-z-fill").style.height = clamp((t.fork_mm / 870) * 100, 0, 100) + "%";
  $("g-reach").textContent = Math.round(t.y_mm);
  $("g-y-fill").style.width = clamp((t.y_mm / 300) * 100, 0, 100) + "%";

  const overlay = $("fault-overlay");
  overlay.hidden = !faulted;
  if (faulted) {
    $("fault-overlay-title").textContent = t.fault_name === "E-STOP" ? "E-STOP" : "FAULT";
    $("fault-overlay-sub").textContent = t.fault_name === "E-STOP"
      ? (t.estop_ok ? "CHAIN RESET — CLEAR FAULT TO RESUME" : "Z BRAKE CLAMPED · THROTTLES ZERO")
      : t.fault_name;
  }
  $("btn-clear").hidden = !faulted;
  $("btn-clear").disabled = !t.estop_ok;
  $("btn-estop-reset").hidden = !(t.sim && !t.estop_ok);
  $("btn-estop").hidden = faulted && t.sim && !t.estop_ok;

  if (t.pose) {
    $("hud-pose").textContent =
      `X ${t.pose[0].toFixed(2)}m  Y ${t.pose[1].toFixed(2)}m  θ ${(t.pose[2] * 180 / Math.PI).toFixed(0)}°  V ${t.speed.toFixed(2)}m/s`;
    drawScene(t);
  }
}

/* ---------- console actions ---------- */
const ACTION_FIRE = {
  stop: () => cmd({ t: "stop" }),
  estop: () => cmd({ t: "estop" }),
  home: () => cmd({ t: "home" }),
};
$("btn-stop").addEventListener("click", ACTION_FIRE.stop);
$("btn-estop").addEventListener("click", ACTION_FIRE.estop);
$("btn-estop-reset").addEventListener("click", () => cmd({ t: "estop_reset" }));
$("btn-clear").addEventListener("click", () => cmd({ t: "clr" }));
$("btn-home").addEventListener("click", ACTION_FIRE.home);

const heldJogs = { z: 0, y: 0 };
for (const btn of document.querySelectorAll(".btn-jog")) {
  const [axis, dir] = [btn.dataset.jog[0], btn.dataset.jog[1] === "+" ? 1 : -1];
  const start = (e) => { e.preventDefault(); btn.classList.add("held"); heldJogs[axis] = dir; };
  const end = () => { btn.classList.remove("held"); heldJogs[axis] = 0; };
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointerleave", end);
}

/* ---------- joystick ---------- */
const pad = $("joypad"), knob = $("joypad-knob");
function setKnob(x, y) {
  const r = pad.clientWidth / 2 - knob.clientWidth / 2 - 4;
  knob.style.left = (pad.clientWidth / 2 - knob.clientWidth / 2 + x * r) + "px";
  knob.style.top = (pad.clientHeight / 2 - knob.clientHeight / 2 - y * r) + "px";
}
function padInput(e) {
  const rect = pad.getBoundingClientRect();
  let x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  let y = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
  const m = Math.hypot(x, y);
  if (m > 1) { x /= m; y /= m; }
  state.drive.x = x; state.drive.y = y;
  setKnob(x, y);
}
pad.addEventListener("pointerdown", (e) => {
  pad.setPointerCapture(e.pointerId);
  state.drive.active = true;
  padInput(e);
});
pad.addEventListener("pointermove", (e) => { if (state.drive.active) padInput(e); });
const padRelease = () => {
  state.drive.active = false;
  state.drive.x = state.drive.y = 0;
  setKnob(0, 0);
};
pad.addEventListener("pointerup", padRelease);
pad.addEventListener("pointercancel", padRelease);

/* ---------- gamepad ---------- */
function activeGamepad() {
  for (const gp of navigator.getGamepads ? navigator.getGamepads() : []) {
    if (gp && gp.connected) return gp;
  }
  return null;
}
function gpContribution(b, gp) {
  if (!gp) return 0;
  if (b.btn != null) {
    const bt = gp.buttons[b.btn];
    return bt ? bt.value : 0;
  }
  if (b.axis != null) {
    const v = (b.sign || 1) * (gp.axes[b.axis] || 0);
    return v <= DEADZONE ? 0 : (v - DEADZONE) / (1 - DEADZONE);
  }
  return 0;
}
function slotValue(slot, gp) {
  let v = 0;
  for (const b of state.bindings.slots[slot] || []) {
    if (b.key != null) { if (state.keys.has(b.key)) v = Math.max(v, 1); }
    else v = Math.max(v, gpContribution(b, gp));
  }
  return clamp(v, 0, 1);
}

/* ---------- keyboard ---------- */
function keyBound(k) {
  for (const slot in state.bindings.slots) {
    if (state.bindings.slots[slot].some((b) => b.key === k)) return slot;
  }
  return null;
}
document.addEventListener("keydown", (e) => {
  if (state.capture) return;                 // capture handler owns the keys
  if (document.body.dataset.view !== "console") return;
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
  const k = e.key.toLowerCase();
  const slot = keyBound(k);
  if (!slot) return;
  e.preventDefault();
  if (BUTTON_SLOTS.includes(slot)) { if (!e.repeat) ACTION_FIRE[slot](); return; }
  state.keys.add(k);
});
document.addEventListener("keyup", (e) => state.keys.delete(e.key.toLowerCase()));
window.addEventListener("blur", () => state.keys.clear());

/* ---------- 15 Hz intent streamer ---------- */
setInterval(() => {
  const gp = activeGamepad();
  const onDot = $("gp-dot"), modalDot = $("gp-modal-status");
  onDot.classList.toggle("on", !!gp);
  if (modalDot) modalDot.textContent = gp ? (gp.id || "GAMEPAD").slice(0, 28).toUpperCase() : "NO GAMEPAD";

  if (document.body.dataset.view !== "console" || state.capture) return;

  // gamepad edges for button actions (keyboard edges fire in keydown)
  for (const slot of BUTTON_SLOTS) {
    let v = 0;
    for (const b of state.bindings.slots[slot] || []) {
      if (b.key == null) v = Math.max(v, gpContribution(b, gp));
    }
    const pressed = v >= 0.6;
    if (pressed && !state.btnPrev[slot]) ACTION_FIRE[slot]();
    state.btnPrev[slot] = pressed;
  }

  let fwd = slotValue("drive+", gp) - slotValue("drive-", gp);
  let turn = slotValue("turn+", gp) - slotValue("turn-", gp);
  if (state.drive.active) { fwd = state.drive.y; turn = state.drive.x; }
  let zv = slotValue("fork+", gp) - slotValue("fork-", gp);
  let yv = slotValue("reach+", gp) - slotValue("reach-", gp);
  if (heldJogs.z) zv = heldJogs.z;
  if (heldJogs.y) yv = heldJogs.y;
  if (slotValue("creep", gp) >= 0.5) { fwd *= 0.3; turn *= 0.4; zv *= 0.3; yv *= 0.3; }

  const l = Math.round(clamp((fwd + turn * 0.6) * 100, -100, 100));
  const r = Math.round(clamp((fwd - turn * 0.6) * 100, -100, 100));
  zv = Math.round(zv * 90);
  yv = Math.round(yv * 60);

  const S = state.lastSent;
  if (l || r || S.l || S.r) cmd({ t: "drive", l, r });
  if (zv || S.zv) cmd({ t: "zv", v: zv });
  if (yv || S.yv) cmd({ t: "yv", v: yv });
  state.lastSent = { l, r, zv, yv };
}, 66);

/* ---------- control mapping UI ---------- */
function bindLabel(b) {
  if (b.key != null) return KEY_NAMES[b.key] || b.key.toUpperCase();
  if (b.btn != null) return BTN_NAMES[b.btn] || "B" + b.btn;
  return (AXIS_NAMES[b.axis] || "AX" + b.axis) + ((b.sign || 1) > 0 ? "+" : "−");
}

function renderBindTable() {
  const tbl = $("bind-table");
  tbl.innerHTML = "";
  for (const [group, slots] of SLOT_GROUPS) {
    const g = document.createElement("div");
    g.className = "bind-group";
    g.textContent = group;
    tbl.appendChild(g);
    for (const [slot, label] of slots) {
      const row = document.createElement("div");
      row.className = "bind-row";
      const name = document.createElement("span");
      name.className = "bind-name";
      name.textContent = label;
      const chips = document.createElement("div");
      chips.className = "bind-chips";
      for (let i = 0; i < (state.bindings.slots[slot] || []).length; i++) {
        const b = state.bindings.slots[slot][i];
        const chip = document.createElement("span");
        chip.className = "bind-chip" + (b.key == null ? " gp" : "");
        chip.textContent = bindLabel(b);
        chip.title = "click to remove";
        chip.addEventListener("click", () => {
          state.bindings.slots[slot].splice(i, 1);
          saveBindings();
          renderBindTable();
        });
        chips.appendChild(chip);
      }
      const add = document.createElement("button");
      add.className = "bind-add";
      add.textContent = "＋";
      add.addEventListener("click", () => startCapture(slot, add));
      chips.appendChild(add);
      row.appendChild(name);
      row.appendChild(chips);
      tbl.appendChild(row);
    }
  }
}

function startCapture(slot, addBtn) {
  cancelCapture();
  const gp = activeGamepad();
  state.capture = {
    slot,
    axesBase: gp ? [...gp.axes] : [],
    btnBase: gp ? gp.buttons.map((b) => b.value) : [],
  };
  addBtn.classList.add("capturing");
  const hint = $("capture-hint");
  hint.classList.add("capturing");
  hint.textContent = "Listening… press a key or gamepad button, or push a stick — ESC cancels.";
  pollCapture();
}

function finishCapture(binding) {
  const slot = state.capture && state.capture.slot;
  cancelCapture();
  if (!slot || !binding) return;
  const list = state.bindings.slots[slot] = state.bindings.slots[slot] || [];
  const sig = JSON.stringify(binding);
  if (!list.some((b) => JSON.stringify(b) === sig)) list.push(binding);
  saveBindings();
  renderBindTable();
}

function cancelCapture() {
  state.capture = null;
  const hint = $("capture-hint");
  hint.classList.remove("capturing");
  hint.textContent = "Click ＋ on a row, then press a key, press a gamepad button, or move a stick. Click a chip to remove it.";
  document.querySelectorAll(".bind-add.capturing").forEach((b) => b.classList.remove("capturing"));
}

function pollCapture() {
  if (!state.capture) return;
  const gp = activeGamepad();
  if (gp) {
    for (let i = 0; i < gp.buttons.length; i++) {
      if (gp.buttons[i].value > 0.6 && (state.capture.btnBase[i] || 0) < 0.4) {
        finishCapture({ btn: i });
        return;
      }
    }
    for (let i = 0; i < gp.axes.length; i++) {
      const v = gp.axes[i], base = state.capture.axesBase[i] || 0;
      if (Math.abs(v) > 0.55 && Math.abs(v - base) > 0.4) {
        finishCapture({ axis: i, sign: v > 0 ? 1 : -1 });
        return;
      }
    }
  }
  requestAnimationFrame(pollCapture);
}

document.addEventListener("keydown", (e) => {
  if (!state.capture) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.key === "Escape") { cancelCapture(); return; }
  if (e.key === "Shift" || e.key === "Control" || e.key === "Alt") {
    finishCapture({ key: e.key.toLowerCase() });
    return;
  }
  finishCapture({ key: e.key.toLowerCase() });
}, true);

async function loadBindings() {
  try {
    const r = await api("/api/bindings");
    if (r.bindings && r.bindings.slots) state.bindings = r.bindings;
  } catch { /* defaults stand */ }
  renderKeysHint();
}
async function saveBindings() {
  renderKeysHint();
  try { await api("/api/bindings", { bindings: state.bindings }); } catch { /* local only */ }
}

function firstKey(slot) {
  const b = (state.bindings.slots[slot] || []).find((x) => x.key != null);
  return b ? bindLabel(b) : "—";
}
function renderKeysHint() {
  $("hud-keys").textContent =
    `${firstKey("drive+")} ${firstKey("turn-")} ${firstKey("drive-")} ${firstKey("turn+")} drive · ` +
    `${firstKey("fork+")}/${firstKey("fork-")} fork · ${firstKey("reach+")}/${firstKey("reach-")} reach · ` +
    `${firstKey("stop")} stop · ${firstKey("estop")} e-stop`;
}

$("btn-controls").addEventListener("click", () => {
  renderBindTable();
  $("controls-modal").hidden = false;
});
$("controls-close").addEventListener("click", () => {
  cancelCapture();
  $("controls-modal").hidden = true;
});
$("controls-modal").addEventListener("pointerdown", (e) => {
  if (e.target === $("controls-modal")) { cancelCapture(); $("controls-modal").hidden = true; }
});
$("bind-reset").addEventListener("click", () => {
  state.bindings = structuredClone(DEFAULT_BINDINGS);
  saveBindings();
  renderBindTable();
});

/* ---------- updates ----------
   Installed builds check the release feed; if a newer version is published,
   the banner links to the new installer. Running it updates in place. */
let updateUrl = null;
async function refreshUpdateStatus(retried) {
  let s;
  try { s = await api("/api/update/status"); } catch { return; }
  if (!s.checked && !retried) { setTimeout(() => refreshUpdateStatus(true), 6000); return; }
  if (s.available && s.available.url) {
    updateUrl = s.available.url;
    $("update-text").textContent = `Version ${s.available.version} is available.`;
    $("update-apply").textContent = "Download update";
    $("update-banner").hidden = false;
  }
}
$("update-apply").addEventListener("click", () => {
  if (updateUrl) window.open(updateUrl, "_blank", "noopener");
});

/* ---------- sim feed: simple first-person 3D from the mast camera ----------
   Software pinhole projection on canvas 2D — no libraries. Camera rides the
   mast top (robot-local x 0.25, z 1.05, pitched down 12°); world objects are
   boxes rendered painter-sorted with near-plane clipping. */
const canvas = $("sim-canvas");
const ctx = canvas.getContext("2d");
const WORLD = { w: 12, h: 8 };
const CAM = { lx: 0.25, z: 1.05, pitch: 0.21, near: 0.06 };

function resizeCanvas() {
  const box = canvas.parentElement.getBoundingClientRect();
  canvas.width = box.width * devicePixelRatio;
  canvas.height = box.height * devicePixelRatio;
}
window.addEventListener("resize", resizeCanvas);

// box(x0,y0,z0, x1,y1,z1) -> top + 4 side faces (bottoms are never visible
// from the mast camera)
function boxFaces(x0, y0, z0, x1, y1, z1, fill, stroke) {
  const f = [];
  const q = (pts) => f.push({ pts, fill, stroke });
  q([[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]);        // top
  q([[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]]);        // -y
  q([[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]]);        // +y
  q([[x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1]]);        // -x
  q([[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]]);        // +x
  return f;
}

const SCENE = (() => {
  const f = [];
  const wallFill = "rgba(15,15,18,0.96)", wallEdge = "rgba(244,243,239,0.16)";
  const rackEdge = "rgba(255,212,0,0.5)", rackFill = "rgba(20,18,10,0.9)";
  const boxFill = "rgba(28,24,18,0.95)", boxEdge = "rgba(230,178,58,0.45)";
  // perimeter walls (h 2.4)
  for (const [x0, y0, x1, y1] of [[0, 0, 12, 0.05], [0, 7.95, 12, 8], [0, 0, 0.05, 8], [11.95, 0, 12, 8]]) {
    f.push(...boxFaces(x0, y0, 0, x1, y1, 2.4, wallFill, wallEdge));
  }
  // dock stripe (floor marking)
  f.push({ pts: [[0.05, 0.05, 0.004], [0.4, 0.05, 0.004], [0.4, 7.95, 0.004], [0.05, 7.95, 0.004]],
           fill: "rgba(230,178,58,0.10)", stroke: "rgba(230,178,58,0.25)" });
  // racks along the far wall: posts, two shelf slabs, product cases
  for (const rx of [1.0, 6.5]) {
    const x1 = rx + 4.5, y0 = 6.8, y1 = 7.75;
    for (const px of [rx, rx + 2.25, x1 - 0.05]) {
      f.push(...boxFaces(px, y0, 0, px + 0.05, y0 + 0.05, 1.7, rackFill, rackEdge));
      f.push(...boxFaces(px, y1 - 0.05, 0, px + 0.05, y1, 1.7, rackFill, rackEdge));
    }
    for (const sz of [0.45, 1.15]) {
      f.push(...boxFaces(rx, y0, sz, x1, y1, sz + 0.04, rackFill, rackEdge));
    }
    for (const [bx, bw, bz] of [[rx + 0.4, 0.38, 0.49], [rx + 1.5, 0.3, 0.49], [rx + 2.6, 0.35, 0.49],
                                 [rx + 0.9, 0.32, 1.19], [rx + 2.9, 0.38, 1.19], [rx + 3.7, 0.3, 0.49]]) {
      f.push(...boxFaces(bx, y0 + 0.15, bz, bx + bw, y0 + 0.15 + 0.55, bz + 0.28, boxFill, boxEdge));
    }
  }
  // pallet + one staged case
  f.push(...boxFaces(9.4, 1.0, 0, 10.62, 2.02, 0.145, "rgba(24,20,12,0.95)", "rgba(255,212,0,0.55)"));
  f.push(...boxFaces(9.6, 1.2, 0.145, 10.0, 1.5, 0.395, boxFill, boxEdge));
  return f;
})();

// floor grid, 1 m pitch
const GRID = (() => {
  const seg = [];
  for (let x = 0; x <= WORLD.w; x++) seg.push([[x, 0, 0], [x, WORLD.h, 0]]);
  for (let y = 0; y <= WORLD.h; y++) seg.push([[0, y, 0], [WORLD.w, y, 0]]);
  return seg;
})();

function robotFaces(t) {
  const [rx, ry, rh] = t.pose;
  const c = Math.cos(rh), s = Math.sin(rh);
  const W = (lx, ly, lz) => [rx + lx * c - ly * s, ry + lx * s + ly * c, lz];
  const lift = t.fork_mm / 1000;
  const f = [];
  const forkFill = `rgba(255,212,0,${(0.3 + clamp(lift / 0.87, 0, 1) * 0.4).toFixed(2)})`;
  const mk = (x0, y0, z0, x1, y1, z1, fill, stroke) => {
    // local-space box -> world quads (top + sides), reusing boxFaces on
    // local coords then mapping corners through W()
    for (const face of boxFaces(x0, y0, z0, x1, y1, z1, fill, stroke)) {
      f.push({ pts: face.pts.map((p) => W(p[0], p[1], p[2])), fill: face.fill, stroke: face.stroke });
    }
  };
  mk(0.385, 0.123, lift - 0.025, 0.785, 0.203, lift, forkFill, "rgba(255,212,0,0.95)");     // left blade
  mk(0.385, -0.203, lift - 0.025, 0.785, -0.123, lift, forkFill, "rgba(255,212,0,0.95)");   // right blade
  mk(0.30, -0.23, lift + 0.05, 0.37, 0.23, lift + 0.13, "rgba(16,16,16,0.95)", "rgba(255,212,0,0.7)"); // W beam
  return f;
}

function drawScene(t) {
  // self-heal: the canvas gets sized while hidden (rect 0) on view switches
  const want = Math.round(canvas.parentElement.getBoundingClientRect().width * devicePixelRatio);
  if (want > 4 && Math.abs(canvas.width - want) > 2) resizeCanvas();
  const [rx, ry, rh] = t.pose;
  const cW = canvas.width, cH = canvas.height;
  const cx = cW / 2, cy = cH / 2;
  const f = cH * 0.9;
  const cosH = Math.cos(rh), sinH = Math.sin(rh);
  const camX = rx + CAM.lx * cosH, camY = ry + CAM.lx * sinH;
  const cosP = Math.cos(CAM.pitch), sinP = Math.sin(CAM.pitch);

  // world -> camera (d fwd, l left, u up) with the down-pitch applied
  const toCam = (p) => {
    const dx = p[0] - camX, dy = p[1] - camY, dz = p[2] - CAM.z;
    const d = cosH * dx + sinH * dy;
    const l = -sinH * dx + cosH * dy;
    return [d * cosP - dz * sinP, l, d * sinP + dz * cosP];
  };
  const proj = (c) => [cx - f * (c[1] / c[0]), cy - f * (c[2] / c[0])];

  // sky / floor split at the horizon
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  const horizon = cy - f * Math.tan(CAM.pitch);
  ctx.fillStyle = "#0c0c10";
  ctx.fillRect(0, 0, cW, Math.max(0, horizon));
  ctx.fillStyle = "#0a0a0a";
  ctx.fillRect(0, Math.max(0, horizon), cW, cH);

  // grid (clipped to the near plane per segment)
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  for (const [a, b] of GRID) {
    let ca = toCam(a), cb = toCam(b);
    if (ca[0] <= CAM.near && cb[0] <= CAM.near) continue;
    if (ca[0] <= CAM.near || cb[0] <= CAM.near) {
      const k = (CAM.near - ca[0]) / (cb[0] - ca[0]);
      const mid = [CAM.near, ca[1] + k * (cb[1] - ca[1]), ca[2] + k * (cb[2] - ca[2])];
      if (ca[0] <= CAM.near) ca = mid; else cb = mid;
    }
    const pa = proj(ca), pb = proj(cb);
    ctx.beginPath(); ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]); ctx.stroke();
  }

  // faces: transform, near-clip, painter-sort far -> near
  const out = [];
  for (const face of [...SCENE, ...robotFaces(t)]) {
    const cams = face.pts.map(toCam);
    if (cams.every((c) => c[0] <= CAM.near)) continue;
    let poly = cams;
    if (cams.some((c) => c[0] <= CAM.near)) {
      poly = [];
      for (let i = 0; i < cams.length; i++) {
        const a = cams[i], b = cams[(i + 1) % cams.length];
        const ain = a[0] > CAM.near, bin = b[0] > CAM.near;
        if (ain) poly.push(a);
        if (ain !== bin) {
          const k = (CAM.near - a[0]) / (b[0] - a[0]);
          poly.push([CAM.near, a[1] + k * (b[1] - a[1]), a[2] + k * (b[2] - a[2])]);
        }
      }
      if (poly.length < 3) continue;
    }
    let depth = 0;
    for (const c of poly) depth += c[0];
    out.push({ depth: depth / poly.length, pts2: poly.map(proj), fill: face.fill, stroke: face.stroke });
  }
  out.sort((a, b) => b.depth - a.depth);
  ctx.lineWidth = 1.2;
  ctx.lineJoin = "round";
  for (const face of out) {
    ctx.beginPath();
    face.pts2.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
    if (face.fill) { ctx.fillStyle = face.fill; ctx.fill(); }
    if (face.stroke) { ctx.strokeStyle = face.stroke; ctx.stroke(); }
  }

  // subtle camera vignette + reticle
  ctx.strokeStyle = "rgba(255,212,0,0.25)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx - 14, cy); ctx.lineTo(cx - 4, cy);
  ctx.moveTo(cx + 4, cy); ctx.lineTo(cx + 14, cy);
  ctx.moveTo(cx, cy - 14); ctx.lineTo(cx, cy - 4);
  ctx.moveTo(cx, cy + 4); ctx.lineTo(cx, cy + 14);
  ctx.stroke();
}

/* ---------- boot ---------- */
renderKeysHint();
initLogin().catch((e) => {
  document.title = "SafraConsole boot error: " + e.message;
  setView("login");
  $("login-form").hidden = false;
  $("login-error").textContent = "console failed to load: " + e.message;
});
