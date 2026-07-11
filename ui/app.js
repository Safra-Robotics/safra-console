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
    "pick":   [{ key: "enter" }, { btn: 0 }],
  },
};
const SLOT_GROUPS = [
  ["DRIVE", [["drive+", "FORWARD"], ["drive-", "REVERSE"], ["turn+", "TURN RIGHT"], ["turn-", "TURN LEFT"]]],
  ["LIFT / REACH", [["fork+", "FORK UP"], ["fork-", "FORK DOWN"], ["reach+", "REACH OUT"], ["reach-", "REACH IN"]]],
  ["ACTIONS", [["stop", "STOP"], ["estop", "E-STOP"], ["home", "HOME Z"], ["creep", "CREEP (HOLD)"], ["pick", "CASE PLACED"]]],
];
const BUTTON_SLOTS = ["stop", "estop", "home", "pick"];
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
  job: null,                              // active pick job (SSE)
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
    if (rb.kind === "sim") view.have = false;   // re-snap the eased view pose

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
    state.job = d.job || null;
    let taskEvt = false;
    for (const e of d.events || []) {
      if (e.id > state.lastEvtId) {
        state.lastEvtId = e.id;
        appendEvent(e);
        if (e.kind === "task" || e.kind === "label") taskEvt = true;
      }
    }
    renderTaskPanel();
    // keep the dashboard live: task/label events only appear when something
    // actually changed, so refresh on those rather than every 100 ms tick
    if (taskEvt && !$("tasks-modal").hidden) refreshJobs(false).catch(() => {});
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
    feedSim(t);
  }
}

/* ---------- console actions ---------- */
const ACTION_FIRE = {
  stop: () => cmd({ t: "stop" }),
  estop: () => cmd({ t: "estop" }),
  home: () => cmd({ t: "home" }),
  pick: () => casePlaced(),
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
  // a focused button already fires its own click on Enter — don't double-fire
  if (slot === "pick" && tag === "BUTTON") return;
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
    if (r.bindings && r.bindings.slots) {
      state.bindings = r.bindings;
      // bindings saved by an older build may predate newer action slots
      for (const s in DEFAULT_BINDINGS.slots) {
        if (!state.bindings.slots[s]) state.bindings.slots[s] = structuredClone(DEFAULT_BINDINGS.slots[s]);
      }
    }
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

/* ---------- sim feed: first-person 3D from the mast camera ----------
   Dependency-free software renderer on canvas 2D. A pinhole camera rides the
   mast top (robot-local x 0.25, z 1.05, pitched down ~12°). World geometry is
   axis-aligned boxes, each carrying an outward normal + a material; the
   renderer near-clips, back-face culls, Lambert-shades against a key light,
   applies distance fog, then painter-sorts far -> near. Long surfaces (walls,
   floor bays) are baked into ~1 m tiles so average-depth sorting stays correct
   as the operator turns — one un-tiled 12 m wall quad would otherwise pop in
   front of the racks. Telemetry lands at 10 Hz; a requestAnimationFrame loop
   eases the view pose between frames so the feed reads at display rate. */
const canvas = $("sim-canvas");
const ctx = canvas.getContext("2d");
const WORLD = { w: 12, h: 8 };
const CAM = { lx: 0.25, z: 1.05, pitch: 0.21, near: 0.06 };

// one key light (above, front-left) + ambient; fog haze fills the deep aisles
const LIGHT = (() => { const v = [-0.35, -0.30, 1.0], m = Math.hypot(v[0], v[1], v[2]);
  return [v[0] / m, v[1] / m, v[2] / m]; })();
const AMBIENT = 0.44, DIFFUSE = 0.56;
const FOG = [11, 11, 15], FOG_NEAR = 4.5, FOG_FAR = 19, FOG_MAX = 0.9;

// material = base rgb + alpha; `flat` skips shading & back-face culling (floor
// decals, emissive ceiling strips); `edge` is an optional outline rgba.
const MAT = {
  wall:    { col: [30, 30, 35], a: 0.98, edge: [244, 243, 239, 0.05] },
  upright: { col: [46, 43, 38], a: 0.96, edge: [255, 212, 0, 0.55] },
  beam:    { col: [150, 108, 18], a: 0.97, edge: [255, 212, 0, 0.8] },
  card:    { col: [124, 95, 56], a: 0.96, edge: [230, 178, 58, 0.5] },
  card2:   { col: [104, 78, 46], a: 0.96, edge: [230, 178, 58, 0.42] },
  pale:    { col: [150, 142, 122], a: 0.96, edge: [206, 198, 176, 0.5] },
  dark:    { col: [42, 38, 30], a: 0.96, edge: [230, 178, 58, 0.4] },
  pallet:  { col: [96, 71, 39], a: 0.97, edge: [201, 150, 70, 0.55] },
  bollard: { col: [232, 190, 22], a: 1.0, edge: [16, 16, 16, 0.7] },
  chassis: { col: [20, 20, 22], a: 0.98, edge: [255, 212, 0, 0.5] },
  strip:   { col: [255, 246, 214], a: 0.9, flat: true },
  lane:    { col: [230, 178, 58], a: 0.16, flat: true, edge: [230, 178, 58, 0.28] },
  dock:    { col: [200, 60, 40], a: 0.14, flat: true, edge: [230, 120, 60, 0.3] },
  pad:     { col: [70, 150, 120], a: 0.16, flat: true, edge: [90, 200, 160, 0.34] },
};

// eased view pose (10 Hz telem -> per-frame render); see feedSim() / frame()
const view = { pose: [3, 4, 0], fork: 70, reach: 0, speed: 0, tel: null, have: false };

function resizeCanvas() {
  const box = canvas.parentElement.getBoundingClientRect();
  canvas.width = box.width * devicePixelRatio;
  canvas.height = box.height * devicePixelRatio;
}
window.addEventListener("resize", resizeCanvas);

// box(x0,y0,z0, x1,y1,z1, mat) -> top + 4 side faces, each with an explicit
// outward normal so back-face culling & shading don't depend on winding
// (bottoms are never visible from the mast camera).
function boxFaces(x0, y0, z0, x1, y1, z1, mat) {
  const f = [];
  const q = (pts, n) => f.push({ pts, n, mat });
  q([[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], [0, 0, 1]);   // top
  q([[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]], [0, -1, 0]);  // -y
  q([[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]], [0, 1, 0]);   // +y
  q([[x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1]], [-1, 0, 0]);  // -x
  q([[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]], [1, 0, 0]);   // +x
  return f;
}

// a long box baked into ~seg-metre tiles along its dominant horizontal axis,
// so painter-sorting by average depth stays correct for walls & floor bays
function boxTiled(x0, y0, z0, x1, y1, z1, mat, seg) {
  const f = [], dx = x1 - x0, dy = y1 - y0;
  const n = Math.max(1, Math.round((dx >= dy ? dx : dy) / (seg || 1.0)));
  for (let i = 0; i < n; i++) {
    const a = i / n, b = (i + 1) / n;
    if (dx >= dy) f.push(...boxFaces(x0 + dx * a, y0, z0, x0 + dx * b, y1, z1, mat));
    else f.push(...boxFaces(x0, y0 + dy * a, z0, x1, y0 + dy * b, z1, mat));
  }
  return f;
}

// a flat horizontal decal (floor marking / ceiling strip): a single two-sided
// quad, normal up; `flat` materials are never culled or shaded
function decal(x0, y0, x1, y1, z, mat) {
  return [{ pts: [[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], n: [0, 0, 1], mat }];
}

const SCENE = (() => {
  const F = [];
  const add = (arr) => { for (let i = 0; i < arr.length; i++) F.push(arr[i]); };
  const H = 2.6, TH = 0.08;                        // wall height / thickness

  // perimeter shell, tiled ~1 m so it depth-sorts correctly against the racks
  add(boxTiled(0, 0, 0, WORLD.w, TH, H, MAT.wall, 1.0));                     // south
  add(boxTiled(0, WORLD.h - TH, 0, WORLD.w, WORLD.h, H, MAT.wall, 1.0));     // north (rack wall)
  add(boxTiled(0, 0, 0, TH, WORLD.h, H, MAT.wall, 1.0));                     // west (dock)
  add(boxTiled(WORLD.w - TH, 0, 0, WORLD.w, WORLD.h, H, MAT.wall, 1.0));     // east

  // emissive ceiling light strips
  for (const ly of [1.6, 4.0, 6.4]) add(decal(0.6, ly - 0.12, WORLD.w - 0.6, ly + 0.12, H - 0.05, MAT.strip));

  // floor markings: two aisle lanes, the dock strip, a charge pad
  for (const lx of [3.0, 9.0]) add(decal(lx - 0.06, 0.6, lx + 0.06, WORLD.h - 1.0, 0.006, MAT.lane));
  add(decal(TH, 0.1, 0.55, WORLD.h - 0.1, 0.005, MAT.dock));
  add(decal(9.9, 5.9, 11.1, 7.1, 0.005, MAT.pad));

  // safety bollards flanking the dock lane
  for (const by of [3.1, 4.9]) add(boxFaces(0.62, by - 0.06, 0, 0.74, by + 0.06, 0.72, MAT.bollard));

  // pallet-rack rows along the far wall: uprights, two beam levels, product
  const rackY0 = WORLD.h - TH - 0.9, rackY1 = WORLD.h - TH - 0.06;
  const caseMats = [MAT.card, MAT.card2, MAT.pale, MAT.dark];
  for (const [bx0, bx1] of [[0.8, 5.2], [6.6, 11.1]]) {
    for (const px of [bx0, (bx0 + bx1) / 2 - 0.045, bx1 - 0.09]) {          // uprights (front/back legs)
      add(boxFaces(px, rackY0, 0, px + 0.09, rackY0 + 0.09, 2.0, MAT.upright));
      add(boxFaces(px, rackY1 - 0.09, 0, px + 0.09, rackY1, 2.0, MAT.upright));
    }
    for (const bz of [0.5, 1.22]) {                                         // load beams
      add(boxFaces(bx0, rackY0, bz, bx1, rackY0 + 0.07, bz + 0.09, MAT.beam));
      add(boxFaces(bx0, rackY1 - 0.07, bz, bx1, rackY1, bz + 0.09, MAT.beam));
    }
    let slot = 0;                                                          // product cases per shelf
    for (const [lvl, hh] of [[0.59, 0.56], [1.31, 0.5]]) {
      let cx = bx0 + 0.16;
      while (cx < bx1 - 0.34) {
        const w = 0.34 + 0.12 * (((slot * 7) % 5) / 4);                    // deterministic jitter
        const dd = 0.5 + 0.05 * ((slot * 5) % 3);
        add(boxFaces(cx, rackY0 + 0.11, lvl, cx + w, rackY0 + 0.11 + dd, lvl + hh, caseMats[slot % 4]));
        cx += w + 0.13; slot++;
      }
    }
  }

  // staged pallets with box stacks out on the floor
  const stack = (x, y, m) => {
    add(boxFaces(x, y, 0, x + 1.2, y + 1.0, 0.14, MAT.pallet));            // pallet
    add(boxFaces(x + 0.08, y + 0.08, 0.14, x + 1.12, y + 0.92, 0.62, m));  // lower case
    add(boxFaces(x + 0.22, y + 0.18, 0.62, x + 0.86, y + 0.72, 1.04, MAT.card)); // upper case
  };
  stack(4.2, 1.5, MAT.card2);
  stack(7.8, 2.3, MAT.pale);
  return F;
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
  const Rn = (n) => [n[0] * c - n[1] * s, n[0] * s + n[1] * c, n[2]];   // rotate normal into world
  const lift = t.fork_mm / 1000;
  const f = [];
  // fork blades brighten as they rise, so the lift reads at a glance
  const fork = { col: [255, 212, 0], a: 0.34 + clamp(lift / 0.87, 0, 1) * 0.5, edge: [255, 212, 0, 0.95] };
  const mk = (x0, y0, z0, x1, y1, z1, mat) => {
    // local-space box -> world quads, mapping corners through W() and normals through Rn()
    for (const face of boxFaces(x0, y0, z0, x1, y1, z1, mat)) {
      f.push({ pts: face.pts.map((p) => W(p[0], p[1], p[2])), n: Rn(face.n), mat: face.mat });
    }
  };
  mk(0.02, -0.26, 0.06, 0.34, 0.26, 0.34, MAT.chassis);                 // chassis apron under the camera
  mk(0.30, -0.23, lift + 0.05, 0.37, 0.23, lift + 0.13, MAT.chassis);   // carriage cross-beam
  mk(0.385, 0.123, lift - 0.025, 0.785, 0.203, lift, fork);             // left blade
  mk(0.385, -0.203, lift - 0.025, 0.785, -0.123, lift, fork);           // right blade
  return f;
}

// Lambert key light + distance fog -> a css fill for one face; `flat`
// materials (decals, emissive strips) skip the lighting term.
function faceColor(mat, n, depth) {
  let r = mat.col[0], g = mat.col[1], b = mat.col[2];
  if (!mat.flat) {
    const lam = AMBIENT + DIFFUSE * Math.max(0, n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2]);
    r *= lam; g *= lam; b *= lam;
  }
  const ft = clamp((depth - FOG_NEAR) / (FOG_FAR - FOG_NEAR), 0, 1) * FOG_MAX;
  r += (FOG[0] - r) * ft; g += (FOG[1] - g) * ft; b += (FOG[2] - b) * ft;
  return `rgba(${r | 0},${g | 0},${b | 0},${mat.a})`;
}
function edgeColor(edge, depth) {
  const ft = clamp((depth - FOG_NEAR) / (FOG_FAR - FOG_NEAR), 0, 1) * FOG_MAX;
  return `rgba(${edge[0] | 0},${edge[1] | 0},${edge[2] | 0},${(edge[3] * (1 - ft)).toFixed(3)})`;
}

// one face -> screen: back-face cull, near-plane clip, shade, queue for sort
function collectFace(face, toCam, proj, cam, out) {
  const mat = face.mat;
  if (!mat.flat) {                                   // back-face cull (skip two-sided decals)
    let ax = 0, ay = 0, az = 0;
    for (const p of face.pts) { ax += p[0]; ay += p[1]; az += p[2]; }
    const k = 1 / face.pts.length;
    if (face.n[0] * (ax * k - cam[0]) + face.n[1] * (ay * k - cam[1]) + face.n[2] * (az * k - cam[2]) > 0) return;
  }
  const cams = face.pts.map(toCam);
  if (cams.every((c) => c[0] <= CAM.near)) return;
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
    if (poly.length < 3) return;
  }
  let depth = 0;
  for (const c of poly) depth += c[0];
  depth /= poly.length;
  out.push({ depth, pts2: poly.map(proj), fill: faceColor(mat, face.n, depth),
             stroke: mat.edge ? edgeColor(mat.edge, depth) : null });
}

let _lastDrawTs = 0;   // wall-clock ms of the last paint, for the rAF watchdog
function drawScene(t) {
  // self-heal: the canvas gets sized while hidden (rect 0) on view switches
  const want = Math.round(canvas.parentElement.getBoundingClientRect().width * devicePixelRatio);
  if (want > 4 && Math.abs(canvas.width - want) > 2) resizeCanvas();
  const [rx, ry, rh] = t.pose;
  const cW = canvas.width, cH = canvas.height;
  const cx = cW / 2, cy = cH / 2;
  const f = cH * 0.86;
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

  // ceiling / floor split at the horizon, each a soft vertical gradient
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  const horizon = cy - f * Math.tan(CAM.pitch);
  const ceil = ctx.createLinearGradient(0, 0, 0, Math.max(1, horizon));
  ceil.addColorStop(0, "#050507"); ceil.addColorStop(1, "#0e0e13");
  ctx.fillStyle = ceil; ctx.fillRect(0, 0, cW, Math.max(0, horizon));
  const floor = ctx.createLinearGradient(0, Math.max(0, horizon), 0, cH);
  floor.addColorStop(0, "#0b0b10"); floor.addColorStop(0.5, "#090909"); floor.addColorStop(1, "#050505");
  ctx.fillStyle = floor; ctx.fillRect(0, Math.max(0, horizon), cW, cH);

  // floor grid (near-clipped per segment, fading into the fog)
  ctx.lineWidth = 1;
  for (const [a, b] of GRID) {
    let ca = toCam(a), cb = toCam(b);
    if (ca[0] <= CAM.near && cb[0] <= CAM.near) continue;
    if (ca[0] <= CAM.near || cb[0] <= CAM.near) {
      const k = (CAM.near - ca[0]) / (cb[0] - ca[0]);
      const mid = [CAM.near, ca[1] + k * (cb[1] - ca[1]), ca[2] + k * (cb[2] - ca[2])];
      if (ca[0] <= CAM.near) ca = mid; else cb = mid;
    }
    const dep = (ca[0] + cb[0]) / 2;
    ctx.strokeStyle = `rgba(255,255,255,${(0.16 * (1 - clamp((dep - 1) / 12, 0, 1))).toFixed(3)})`;
    const pa = proj(ca), pb = proj(cb);
    ctx.beginPath(); ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]); ctx.stroke();
  }

  // faces: cull, clip, shade, painter-sort far -> near
  const cam = [camX, camY, CAM.z], out = [];
  for (const face of SCENE) collectFace(face, toCam, proj, cam, out);
  for (const face of robotFaces(t)) collectFace(face, toCam, proj, cam, out);
  out.sort((a, b) => b.depth - a.depth);
  ctx.lineJoin = "round";
  for (const o of out) {
    ctx.beginPath();
    o.pts2.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
    ctx.fillStyle = o.fill; ctx.fill();
    if (o.stroke) { ctx.lineWidth = 1.1; ctx.strokeStyle = o.stroke; ctx.stroke(); }
  }

  drawOverlay(cW, cH, cx, cy);
  _lastDrawTs = performance.now();
}

// camera-feed HUD: vignette, corner frame ticks, center reticle
function drawOverlay(cW, cH, cx, cy) {
  const vg = ctx.createRadialGradient(cx, cy, Math.min(cW, cH) * 0.26, cx, cy, Math.max(cW, cH) * 0.62);
  vg.addColorStop(0, "rgba(0,0,0,0)"); vg.addColorStop(1, "rgba(0,0,0,0.55)");
  ctx.fillStyle = vg; ctx.fillRect(0, 0, cW, cH);

  const dp = devicePixelRatio || 1, m = 15 * dp, len = 16 * dp;
  ctx.strokeStyle = "rgba(255,212,0,0.5)"; ctx.lineWidth = 1.4 * dp;
  for (const [ox, oy, sx, sy] of [[m, m, 1, 1], [cW - m, m, -1, 1], [m, cH - m, 1, -1], [cW - m, cH - m, -1, -1]]) {
    ctx.beginPath();
    ctx.moveTo(ox, oy + sy * len); ctx.lineTo(ox, oy); ctx.lineTo(ox + sx * len, oy);
    ctx.stroke();
  }
  const r0 = 4 * dp, r1 = 15 * dp;
  ctx.strokeStyle = "rgba(255,212,0,0.6)"; ctx.lineWidth = 1 * dp;
  ctx.beginPath();
  ctx.moveTo(cx - r1, cy); ctx.lineTo(cx - r0, cy);
  ctx.moveTo(cx + r0, cy); ctx.lineTo(cx + r1, cy);
  ctx.moveTo(cx, cy - r1); ctx.lineTo(cx, cy - r0);
  ctx.moveTo(cx, cy + r0); ctx.lineTo(cx, cy + r1);
  ctx.stroke();
  ctx.strokeStyle = "rgba(255,212,0,0.22)";
  ctx.beginPath(); ctx.arc(cx, cy, r1, 0, Math.PI * 2); ctx.stroke();
}

/* Smooth the 10 Hz telemetry up to display rate: renderTelem feeds the latest
   target, and a rAF loop eases the view pose toward it (heading by shortest
   arc) before redrawing — so panning the mast camera reads fluid, not steppy. */
function feedSim(t) {
  if (!view.have) { view.pose = [t.pose[0], t.pose[1], t.pose[2]]; view.fork = t.fork_mm; view.reach = t.y_mm; }
  view.tel = t; view.have = true;
  // watchdog: if the rAF loop hasn't painted recently (window backgrounded or
  // otherwise throttled), snap to the latest telemetry and paint here so the
  // feed can never freeze. In the normal case rAF paints every ~16 ms and this
  // never fires, leaving the smooth path untouched.
  if (performance.now() - _lastDrawTs > 200 &&
      document.body.dataset.view === "console" && state.robot && state.robot.kind === "sim") {
    view.pose = [t.pose[0], t.pose[1], t.pose[2]];
    view.fork = t.fork_mm; view.reach = t.y_mm; view.speed = t.speed || 0;
    drawScene({ pose: view.pose, fork_mm: view.fork, y_mm: view.reach, speed: view.speed });
  }
}
function angLerp(cur, tgt, k) {
  let d = tgt - cur;
  while (d > Math.PI) d -= 2 * Math.PI;
  while (d < -Math.PI) d += 2 * Math.PI;
  return cur + d * k;
}
let _lastFrame = 0;
function frame(ts) {
  requestAnimationFrame(frame);
  const on = document.body.dataset.view === "console" && state.robot &&
             state.robot.kind === "sim" && view.have && view.tel && view.tel.pose;
  if (!on) { _lastFrame = 0; return; }
  const dt = _lastFrame ? Math.min(0.05, (ts - _lastFrame) / 1000) : 0.016;
  _lastFrame = ts;
  const k = 1 - Math.pow(0.0025, dt);                 // ~120 ms time constant
  const tp = view.tel.pose;
  view.pose[0] += (tp[0] - view.pose[0]) * k;
  view.pose[1] += (tp[1] - view.pose[1]) * k;
  view.pose[2] = angLerp(view.pose[2], tp[2], k);
  view.fork += (view.tel.fork_mm - view.fork) * k;
  view.reach += (view.tel.y_mm - view.reach) * k;
  view.speed += ((view.tel.speed || 0) - view.speed) * k;
  drawScene({ pose: view.pose, fork_mm: view.fork, y_mm: view.reach, speed: view.speed });
}
requestAnimationFrame(frame);

/* ---------- pick tasks: sidebar panel + WMS dashboard ----------
   Job state rides the SSE stream (payload.job = the active job + current
   pick), so the sidebar panel is always live. The dashboard modal manages
   the queue: import (JSON/CSV paste or file), start/close/delete jobs,
   per-pick status + tag reprints, label previews, and the printer config. */

function renderTaskPanel() {
  const j = state.job;
  $("task-empty").hidden = !!j;
  $("task-active").hidden = !j;
  if (!j) return;
  $("task-name").textContent = (j.name || "").toUpperCase();
  const handled = j.picked + j.quarantined;
  $("task-fill").style.width = Math.round((100 * handled) / Math.max(1, j.total)) + "%";
  $("task-counts").textContent =
    `${j.picked}/${j.total} PICKED` +
    (j.quarantined ? ` · ${j.quarantined} QUARANTINED` : "") +
    (j.flagged ? " · ⚑ FLAGGED" : "");
  $("task-counts").classList.toggle("flag", !!j.flagged);
  const cur = j.current;
  $("task-pick").hidden = !cur;
  $("task-complete").hidden = !!cur;
  if (cur) {
    $("task-sku").textContent = cur.sku;
    $("task-desc").textContent = cur.desc || "";
    $("task-loc").textContent = cur.location || "—";
    $("task-qty").textContent = cur.qty;
    $("task-stop").textContent = cur.stop || "—";
  } else {
    const msg = $("task-complete-msg");
    msg.textContent = j.flagged
      ? "ALL CASES HANDLED — PALLET FLAGGED, CHECK LOG AT WRAP"
      : "ALL CASES PLACED — WRAP & LABEL";
    msg.classList.toggle("flagged", !!j.flagged);
    $("btn-loader-label").textContent =
      j.loader_label === "printed" ? "REPRINT LOADER" : "LOADER LABEL";
  }
}

let _pickSent = { seq: 0, t: 0 };   // double-press guard (Enter / gamepad A)
async function casePlaced() {
  const j = state.job;
  if (!j || !j.current) return;
  const seq = j.current.seq;
  if (_pickSent.seq === seq && Date.now() - _pickSent.t < 1500) return;
  _pickSent = { seq, t: Date.now() };
  await api("/api/picks/complete", { id: j.id, seq }).catch(() => {});
}
$("btn-case-placed").addEventListener("click", casePlaced);
$("btn-quarantine").addEventListener("click", () => {
  const j = state.job;
  if (j && j.current) api("/api/picks/quarantine", { id: j.id, seq: j.current.seq }).catch(() => {});
});
$("btn-loader-label").addEventListener("click", () => {
  if (state.job) api("/api/jobs/loader_label", { id: state.job.id }).catch(() => {});
});
$("btn-job-complete").addEventListener("click", () => {
  if (state.job) api("/api/jobs/complete", { id: state.job.id }).catch(() => {});
});

/* ---- dashboard modal ---- */
const tasksModal = $("tasks-modal");
let dashJobs = [];
let dashSel = null;

async function openTasks() {
  $("tasks-error").textContent = "";
  $("label-preview").hidden = true;
  await refreshPrinter().catch(() => {});
  await refreshJobs(true).catch(() => {});
  tasksModal.hidden = false;
}
$("btn-tasks").addEventListener("click", openTasks);
$("btn-tasks-robots").addEventListener("click", openTasks);
$("tasks-close").addEventListener("click", () => { tasksModal.hidden = true; });
tasksModal.addEventListener("pointerdown", (e) => {
  if (e.target === tasksModal) tasksModal.hidden = true;
});

async function refreshJobs(autoselect) {
  const r = await api("/api/jobs");
  dashJobs = r.jobs || [];
  if (dashSel && !dashJobs.some((j) => j.id === dashSel)) dashSel = null;
  if (autoselect && !dashSel && dashJobs.length) {
    const act = dashJobs.find((j) => j.status === "active");
    dashSel = (act || dashJobs[0]).id;
  }
  renderJobList();
  await renderJobDetail();
}

const JOB_CHIP = { queued: "chip", active: "chip chip-ok", done: "chip chip-sim" };

function renderJobList() {
  const list = $("job-list");
  list.innerHTML = "";
  if (!dashJobs.length) {
    const d = document.createElement("div");
    d.className = "job-list-empty";
    d.textContent = "No jobs yet — import an export below, or try the demo job.";
    list.appendChild(d);
    return;
  }
  for (const j of dashJobs) {
    const item = document.createElement("div");
    item.className = "job-item" + (j.id === dashSel ? " sel" : "");
    const row = document.createElement("div");
    row.className = "job-item-row";
    const name = document.createElement("div");
    name.className = "job-item-name";
    name.textContent = j.name;
    const chip = document.createElement("span");
    chip.className = JOB_CHIP[j.status] || "chip";
    chip.textContent = j.status.toUpperCase();
    row.appendChild(name);
    if (j.flagged) {
      const flag = document.createElement("span");
      flag.className = "chip chip-fault";
      flag.textContent = "⚑";
      row.appendChild(flag);
    }
    row.appendChild(chip);
    const meta = document.createElement("div");
    meta.className = "job-item-meta";
    meta.textContent = `${j.picked}/${j.total} picked` +
      (j.quarantined ? ` · ${j.quarantined} quar` : "") + ` · ${j.source}`;
    item.appendChild(row);
    item.appendChild(meta);
    item.addEventListener("click", () => {
      dashSel = j.id;
      $("label-preview").hidden = true;
      renderJobList();
      renderJobDetail().catch(() => {});
    });
    list.appendChild(item);
  }
}

function pickActBtn(text, title, fn) {
  const b = document.createElement("button");
  b.className = "pick-act";
  b.textContent = text;
  b.title = title;
  b.addEventListener("click", fn);
  return b;
}

async function renderJobDetail() {
  const empty = $("job-detail-empty"), detail = $("job-detail");
  if (!dashSel) { empty.hidden = false; detail.hidden = true; return; }
  let job;
  try { job = (await api("/api/jobs/detail?id=" + encodeURIComponent(dashSel))).job; }
  catch { empty.hidden = false; detail.hidden = true; return; }
  empty.hidden = true;
  detail.hidden = false;

  $("jd-name").textContent = job.name;
  const meta = $("jd-meta");
  meta.textContent = "";
  const picked = job.picks.filter((p) => p.status === "picked").length;
  const quar = job.picks.filter((p) => p.status === "quarantined").length;
  const bits = [job.status.toUpperCase(), `${picked}/${job.picks.length} picked`];
  if (quar) bits.push(`${quar} quarantined`);
  bits.push(`loader label ${job.loader_label}`, job.source, job.created);
  meta.append(bits.join(" · "));
  if (job.flagged) {
    const f = document.createElement("span");
    f.className = "flag";
    f.textContent = " · ⚑ CHECK PALLET";
    meta.appendChild(f);
  }

  $("jd-activate").hidden = job.status !== "queued";
  $("jd-complete").hidden = job.status !== "active";
  $("jd-complete").disabled = job.picks.some((p) => p.status === "pending");
  $("jd-loader").disabled = job.picks.every((p) => p.status === "pending");

  const cur = job.status === "active"
    ? (job.picks.find((p) => p.status === "pending") || {}).seq : null;
  const rows = $("pick-rows");
  rows.innerHTML = "";
  for (const p of job.picks) {
    const tr = document.createElement("tr");
    if (p.seq === cur) tr.className = "cur";
    const td = (cls, text) => {
      const c = document.createElement("td");
      if (cls) c.className = cls;
      c.textContent = text;
      tr.appendChild(c);
      return c;
    };
    td("mono dim", p.seq);
    td("mono", p.sku);
    td("", p.desc);
    td("mono", p.qty);
    td("mono", p.location);
    td("mono dim", p.stop || "");
    const st = td("", "");
    const stSpan = document.createElement("span");
    stSpan.className = "pick-status " + p.status;
    stSpan.textContent = p.status.toUpperCase();
    stSpan.title = p.note || p.ts || "";
    st.appendChild(stSpan);
    const lb = td("", "");
    const lbSpan = document.createElement("span");
    lbSpan.className = "pick-label " + p.label;
    lbSpan.textContent = p.label === "none" ? "—" : p.label.toUpperCase();
    lb.appendChild(lbSpan);
    const act = td("", "");
    act.style.whiteSpace = "nowrap";
    act.appendChild(pickActBtn("TAG", "preview the case tag",
      () => showPreview(p.seq)));
    if (p.status === "picked") {
      act.appendChild(pickActBtn("RE-PRINT", "print this case tag again",
        () => api("/api/labels/reprint", { id: job.id, seq: p.seq })
          .then(() => renderJobDetail()).catch(() => {})));
    }
    if (p.status !== "pending") {
      act.appendChild(pickActBtn("REOPEN", "put this case back in the queue",
        () => api("/api/picks/reopen", { id: job.id, seq: p.seq })
          .then(() => refreshJobs(false)).catch(() => {})));
    } else if (job.status === "active") {
      act.appendChild(pickActBtn("QUAR", "flag + skip this case",
        () => api("/api/picks/quarantine", { id: job.id, seq: p.seq })
          .then(() => refreshJobs(false)).catch(() => {})));
    }
    rows.appendChild(tr);
  }
}

$("jd-activate").addEventListener("click", () =>
  api("/api/jobs/activate", { id: dashSel }).then(() => refreshJobs(false))
    .catch((e) => { $("tasks-error").textContent = e.message; }));
$("jd-complete").addEventListener("click", () =>
  api("/api/jobs/complete", { id: dashSel }).then(() => refreshJobs(false))
    .catch((e) => { $("tasks-error").textContent = e.message; }));
$("jd-delete").addEventListener("click", () =>
  api("/api/jobs/delete", { id: dashSel }).then(() => { dashSel = null; return refreshJobs(false); })
    .catch(() => {}));
$("jd-loader").addEventListener("click", () =>
  api("/api/jobs/loader_label", { id: dashSel })
    .then((r) => { printerStatus(r.ok, r.detail); return refreshJobs(false); })
    .catch((e) => { $("tasks-error").textContent = e.message; }));

/* ---- import ---- */
$("btn-import").addEventListener("click", async () => {
  $("tasks-error").textContent = "";
  try {
    const r = await api("/api/jobs/import", { text: $("import-text").value });
    $("import-text").value = "";
    if (r.jobs && r.jobs.length) dashSel = r.jobs[0].id;
    await refreshJobs(false);
  } catch (e) { $("tasks-error").textContent = e.message; }
});
$("btn-demo").addEventListener("click", async () => {
  $("tasks-error").textContent = "";
  try {
    const r = await api("/api/jobs/demo", {});
    if (r.jobs && r.jobs.length) dashSel = r.jobs[0].id;
    await refreshJobs(false);
  } catch (e) { $("tasks-error").textContent = e.message; }
});
$("btn-import-file").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", () => {
  const f = $("import-file").files[0];
  if (!f) return;
  f.text().then((t) => { $("import-text").value = t; });
  $("import-file").value = "";
});

/* ---- label preview ---- */
async function showPreview(seq) {
  let r;
  try { r = await api(`/api/labels/preview?id=${encodeURIComponent(dashSel)}&seq=${seq}`); }
  catch (e) { $("tasks-error").textContent = e.message; return; }
  const card = $("label-card");
  card.innerHTML = "";
  const el = (cls, text) => {
    const d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    card.appendChild(d);
    return d;
  };
  const f = r.fields;
  const band = el("lc-band", "");
  const b1 = document.createElement("span"); b1.textContent = f.route_stop;
  const b2 = document.createElement("span");
  b2.textContent = f.pallet ? "PALLET " + f.pallet : "";
  band.append(b1, b2);
  if (seq === "loader") {
    el("lc-sku", "LOADER");
    el("lc-desc", `PALLET ${f.pallet || "—"}`);
    el("lc-qty", `CASES ${f.picked}/${f.total}`);
    el("lc-flag", f.flag_line);
  } else {
    el("lc-sku", f.sku);
    el("lc-desc", f.desc);
    el("lc-qty", `QTY ${f.qty} · CASE ${f.seq}/${f.total}`);
  }
  el("lc-barcode", "");
  el("lc-code", String(f.barcode));
  el("lc-ts", f.ts + (f.operator ? "   " + f.operator : ""));
  $("label-zpl").textContent = r.zpl;
  $("label-preview").hidden = false;
}
$("label-preview-close").addEventListener("click", () => { $("label-preview").hidden = true; });

/* ---- printer config ---- */
function printerChip(p) {
  const chip = $("printer-chip");
  chip.textContent = p.enabled ? `PRINTER ${p.host}:${p.port}` : "PRINTER OFF";
  chip.className = p.enabled ? "chip chip-ok" : "chip";
}
function printerStatus(ok, text) {
  const s = $("printer-status");
  s.textContent = text;
  s.className = "printer-hint " + (ok ? "ok" : "bad");
}
async function refreshPrinter() {
  const p = (await api("/api/printer")).printer;
  $("pr-host").value = p.host;
  $("pr-port").value = p.port;
  $("pr-enabled").checked = !!p.enabled;
  printerChip(p);
}
$("printer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/printer", {
      host: $("pr-host").value, port: $("pr-port").value,
      enabled: $("pr-enabled").checked,
    });
    printerChip(r.printer);
    printerStatus(true, r.printer.enabled
      ? `saved — labels go to ${r.printer.host}:${r.printer.port}`
      : "saved — printing disabled (labels are skipped, picking continues)");
  } catch (e2) { printerStatus(false, e2.message); }
});
$("pr-test").addEventListener("click", async () => {
  try {
    const r = await api("/api/printer/test", {});
    printerStatus(r.ok, r.detail);
  } catch (e) { printerStatus(false, e.message); }
});

/* ---------- boot ---------- */
// Liveness: hold a stream open so the desktop shell knows this window is up.
// When the window closes the socket drops and the local server exits; kept on
// `state` so it isn't garbage-collected. Harmless in a plain browser tab.
try { state.alive = new EventSource("/api/alive"); } catch (e) { /* no SSE */ }
renderKeysHint();
initLogin().catch((e) => {
  document.title = "SafraConsole boot error: " + e.message;
  setView("login");
  $("login-form").hidden = false;
  $("login-error").textContent = "console failed to load: " + e.message;
});
