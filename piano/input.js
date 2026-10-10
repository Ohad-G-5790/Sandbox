/*
 * Input handlers for the piano.
 *
 *   ComputerKeyboardInput  QWERTY-style playing on any keyboard layout (uses physical key codes)
 *   MidiInput              any class-compliant MIDI keyboard via Web MIDI, with real velocity + pedals
 *   PointerInput           the on-screen keys: mouse, touch (multi-touch) and pressure-sensitive pens
 */
"use strict";

/**
 * Physical key -> semitone offset from the lower row's C.
 * Two rows of white keys with the black keys on the row above, like a piano:
 *   Z row:  Z X C V B N M , . /   (white)   S D G H J L ;   (black)   -> C3 .. E4
 *   Q row:  Q W E R T Y U I O P [ ]  (white)   2 3 5 6 7 9 0 =  (black)   -> C4 .. G5
 * event.code names the physical key, so this works on AZERTY, QWERTZ, Dvorak, ... too.
 */
const PIANO_KEY_OFFSETS = {
  KeyZ: 0, KeyS: 1, KeyX: 2, KeyD: 3, KeyC: 4, KeyV: 5, KeyG: 6, KeyB: 7, KeyH: 8, KeyN: 9, KeyJ: 10, KeyM: 11,
  Comma: 12, KeyL: 13, Period: 14, Semicolon: 15, Slash: 16,
  KeyQ: 12, Digit2: 13, KeyW: 14, Digit3: 15, KeyE: 16, KeyR: 17, Digit5: 18, KeyT: 19, Digit6: 20, KeyY: 21,
  Digit7: 22, KeyU: 23, KeyI: 24, Digit9: 25, KeyO: 26, Digit0: 27, KeyP: 28, BracketLeft: 29, Equal: 30, BracketRight: 31,
};

const PIANO_US_LABELS = {
  Comma: ",", Period: ".", Slash: "/", Semicolon: ";", BracketLeft: "[", BracketRight: "]", Equal: "=",
};

const PIANO_DYNAMICS = [
  { name: "pp", velocity: 28 }, { name: "p", velocity: 44 }, { name: "mp", velocity: 60 },
  { name: "mf", velocity: 76 }, { name: "f", velocity: 92 }, { name: "ff", velocity: 108 },
];

function pianoDynamicName(v) {
  if (v <= 32) return "pp";
  if (v <= 48) return "p";
  if (v <= 64) return "mp";
  if (v <= 80) return "mf";
  if (v <= 96) return "f";
  if (v <= 112) return "ff";
  return "fff";
}

class ComputerKeyboardInput {
  constructor(engine, { onChange = () => {} } = {}) {
    this.engine = engine;
    this.onChange = onChange;
    this.baseNote = 48;          // C3 on the Z row, C4 on the Q row
    this.octave = 0;             // -2 .. +2
    this.velocity = 76;          // base dynamic level, 1..127
    this.accent = 28;            // added while Shift is held
    this.enabled = true;
    this.down = new Map();       // code -> note currently sounding
    this.labels = null;          // code -> label from the user's real layout, when the browser can tell us
    this._onKeyDown = (e) => this.handleKeyDown(e);
    this._onKeyUp = (e) => this.handleKeyUp(e);
    this._onBlur = () => this.releaseAll();
  }

  attach(target = window) {
    target.addEventListener("keydown", this._onKeyDown);
    target.addEventListener("keyup", this._onKeyUp);
    target.addEventListener("blur", this._onBlur);
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.releaseAll(); });
    return this;
  }

  detach(target = window) {
    target.removeEventListener("keydown", this._onKeyDown);
    target.removeEventListener("keyup", this._onKeyUp);
    target.removeEventListener("blur", this._onBlur);
  }

  /** Ask the browser for the real labels of the physical keys (Chrome/Edge); fall back to US labels. */
  async loadLabels() {
    const labels = {};
    for (const code of Object.keys(PIANO_KEY_OFFSETS)) labels[code] = ComputerKeyboardInput.usLabel(code);
    try {
      if (navigator.keyboard && navigator.keyboard.getLayoutMap) {
        const map = await navigator.keyboard.getLayoutMap();
        for (const code of Object.keys(PIANO_KEY_OFFSETS)) {
          const l = map.get(code);
          if (l && l.length <= 2) labels[code] = l.toUpperCase();
        }
      }
    } catch (_) { /* keep US labels */ }
    this.labels = labels;
    this.onChange();
    return labels;
  }

  static usLabel(code) {
    if (PIANO_US_LABELS[code]) return PIANO_US_LABELS[code];
    if (code.startsWith("Key")) return code.slice(3);
    if (code.startsWith("Digit")) return code.slice(5);
    return code;
  }

  label(code) { return (this.labels && this.labels[code]) || ComputerKeyboardInput.usLabel(code); }

  noteForCode(code) {
    const off = PIANO_KEY_OFFSETS[code];
    if (off === undefined) return null;
    return this.baseNote + off + 12 * this.octave;
  }

  /** All (code, note) pairs for the current octave, for drawing labels on the keys. */
  mapping() {
    const out = [];
    for (const code of Object.keys(PIANO_KEY_OFFSETS)) out.push({ code, note: this.noteForCode(code), label: this.label(code) });
    return out;
  }

  setOctave(o) {
    o = pianoClamp(Math.round(o), -2, 2);
    if (o === this.octave) return;
    this.octave = o;             // keys already down keep their note (we remember it per code)
    this.onChange();
  }

  setVelocity(v) {
    v = pianoClampVelocity(v);
    if (v === this.velocity) return;
    this.velocity = v;
    this.onChange();
  }

  /** Step through pp .. ff. */
  stepDynamics(direction) {
    if (direction > 0) {
      const next = PIANO_DYNAMICS.find((d) => d.velocity > this.velocity);
      this.setVelocity(next ? next.velocity : 127);
    } else {
      const prev = PIANO_DYNAMICS.slice().reverse().find((d) => d.velocity < this.velocity);
      this.setVelocity(prev ? prev.velocity : 1);
    }
  }

  velocityFor(event) {
    return event.shiftKey ? pianoClampVelocity(this.velocity + this.accent) : this.velocity;
  }

  /** Focused form controls keep their own keyboard handling. */
  static isFormTarget(t) {
    return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.tagName === "BUTTON" || t.isContentEditable === true);
  }

  handleKeyDown(e) {
    if (!this.enabled || ComputerKeyboardInput.isFormTarget(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;      // leave browser/OS shortcuts alone
    const mapped = PIANO_KEY_OFFSETS[e.code] !== undefined;
    if (e.repeat) { if (mapped || e.code === "Space") e.preventDefault(); return; }
    switch (e.code) {
      case "Space": e.preventDefault(); this.engine.setSustain(true); return;
      case "ArrowLeft": e.preventDefault(); this.setOctave(this.octave - 1); return;
      case "ArrowRight": e.preventDefault(); this.setOctave(this.octave + 1); return;
      case "ArrowUp": e.preventDefault(); this.stepDynamics(+1); return;
      case "ArrowDown": e.preventDefault(); this.stepDynamics(-1); return;
      case "Escape": this.engine.panic(); this.down.clear(); return;
      default: break;
    }
    if (!mapped) return;
    e.preventDefault();
    if (this.down.has(e.code)) return;
    const note = this.noteForCode(e.code);
    if (note < PIANO_NOTE_MIN || note > PIANO_NOTE_MAX) return;
    this.down.set(e.code, note);
    this.engine.noteOn(note, this.velocityFor(e), "keyboard");
  }

  handleKeyUp(e) {
    if (e.code === "Space") { this.engine.setSustain(false); return; }
    const note = this.down.get(e.code);
    if (note === undefined) return;
    this.down.delete(e.code);
    this.engine.noteOff(note, "keyboard");
  }

  releaseAll() {
    for (const note of this.down.values()) this.engine.noteOff(note, "keyboard");
    this.down.clear();
    this.engine.setSustain(false);
  }
}

/* ------------------------------------------------------------------------ */

class MidiInput {
  constructor(engine) {
    this.engine = engine;
    this.access = null;
    this.inputs = [];
    this.status = "idle";        // idle | unsupported | requesting | denied | error | ready
    this.error = null;
    this.gamma = 1;              // velocity curve exponent: <1 softer keyboards feel lighter, >1 heavier
    this.lastVelocity = null;
    this.messageCount = 0;
    this._listeners = new Set();
    this._handler = (e) => this.handleMessage(e);
  }

  get supported() { return typeof navigator !== "undefined" && typeof navigator.requestMIDIAccess === "function"; }

  onChange(fn) { this._listeners.add(fn); return () => this._listeners.delete(fn); }
  _emit() { for (const fn of this._listeners) { try { fn(this); } catch (err) { console.error(err); } } }

  setCurve(name) {
    this.curveName = name;
    this.gamma = name === "soft" ? 0.65 : name === "hard" ? 1.5 : 1;
  }

  async connect() {
    if (!this.supported) { this.status = "unsupported"; this._emit(); return false; }
    if (this.status === "ready") { this._refresh(); return true; }
    this.status = "requesting";
    this._emit();
    try {
      this.access = await navigator.requestMIDIAccess({ sysex: false });
    } catch (err) {
      this.error = err;
      this.status = (err && (err.name === "SecurityError" || err.name === "NotAllowedError")) ? "denied" : "error";
      this._emit();
      return false;
    }
    this.status = "ready";
    this.access.onstatechange = () => this._refresh();
    this._refresh();
    return true;
  }

  _refresh() {
    if (!this.access) return;
    this.inputs = Array.from(this.access.inputs.values());
    for (const input of this.inputs) input.onmidimessage = this._handler;
    this._emit();
  }

  deviceNames() {
    return this.inputs.filter((i) => i.state === "connected").map((i) => i.name || i.manufacturer || "MIDI input");
  }

  applyCurve(v) {
    if (this.gamma === 1) return v;
    return pianoClampVelocity(127 * Math.pow(v / 127, this.gamma));
  }

  /** Handles a raw MIDI message (any channel). Exposed so tests can inject messages. */
  handleMessage(e) {
    const d = e && e.data;
    if (!d || d.length < 1) return;
    this.messageCount++;
    const status = d[0] & 0xf0;
    if (status === 0x90 && d.length >= 3 && d[2] > 0) {
      const v = this.applyCurve(d[2]);
      this.lastVelocity = v;
      this.engine.noteOn(d[1], v, "midi");
    } else if (status === 0x80 || (status === 0x90 && d.length >= 3)) {
      this.engine.noteOff(d[1], "midi");
    } else if (status === 0xb0 && d.length >= 3) {
      this.handleControlChange(d[1], d[2]);
    }
  }

  handleControlChange(cc, value) {
    switch (cc) {
      case 64: this.engine.setSustain(value >= 64); break;
      case 66: this.engine.setSostenuto(value >= 64); break;
      case 67: this.engine.setSoft(value >= 64); break;
      case 120: case 123: this.engine.allNotesOff(); break;
      case 121: this.engine.setSustain(false); this.engine.setSostenuto(false); this.engine.setSoft(false); break;
      default: break;
    }
  }
}

/* ------------------------------------------------------------------------ */

/**
 * On-screen keys. Velocity comes from where you hit the key (near the front edge
 * is louder, like a real key lever) or from pen pressure when the device has it.
 * Dragging across keys plays a glissando; several fingers work at once.
 */
class PointerInput {
  constructor(engine, container) {
    this.engine = engine;
    this.el = container;
    this.active = new Map();     // pointerId -> { note }
    this.lastVelocity = null;
    this._onDown = (e) => this.handleDown(e);
    this._onMove = (e) => this.handleMove(e);
    this._onUp = (e) => this.handleUp(e);
  }

  attach() {
    const el = this.el;
    el.addEventListener("pointerdown", this._onDown);
    el.addEventListener("pointermove", this._onMove);
    el.addEventListener("pointerup", this._onUp);
    el.addEventListener("pointercancel", this._onUp);
    el.addEventListener("lostpointercapture", this._onUp);
    el.addEventListener("contextmenu", (e) => e.preventDefault());
    return this;
  }

  static keyAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el && el.closest ? el.closest(".key") : null;
  }

  velocityFor(e, keyEl) {
    if (e.pointerType === "pen" && e.pressure > 0) return pianoClampVelocity(1 + 126 * Math.pow(e.pressure, 0.75));
    const r = keyEl.getBoundingClientRect();
    const f = pianoClamp((e.clientY - r.top) / Math.max(1, r.height), 0, 1);
    let v = 24 + 103 * Math.pow(f, 0.9);
    // Safari exposes trackpad force (1 = click, up to 3 = deep press).
    if (typeof e.webkitForce === "number" && e.webkitForce > 1) v = Math.max(v, 60 + 67 * pianoClamp((e.webkitForce - 1) / 2, 0, 1));
    return pianoClampVelocity(v);
  }

  handleDown(e) {
    if (e.button != null && e.button !== 0) return;
    const key = e.target && e.target.closest ? e.target.closest(".key") : null;
    if (!key) return;
    e.preventDefault();
    try { this.el.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    const note = Number(key.dataset.note);
    const velocity = this.velocityFor(e, key);
    this.lastVelocity = velocity;
    this.active.set(e.pointerId, { note });
    this.engine.noteOn(note, velocity, "pointer");
  }

  handleMove(e) {
    const a = this.active.get(e.pointerId);
    if (!a) return;
    const key = PointerInput.keyAt(e.clientX, e.clientY);
    const note = key ? Number(key.dataset.note) : null;
    if (note === a.note) return;
    if (a.note !== null) this.engine.noteOff(a.note, "pointer");
    a.note = note;
    if (note !== null) {
      const velocity = this.velocityFor(e, key);
      this.lastVelocity = velocity;
      this.engine.noteOn(note, velocity, "pointer");
    }
  }

  handleUp(e) {
    const a = this.active.get(e.pointerId);
    if (!a) return;
    this.active.delete(e.pointerId);
    if (a.note !== null) this.engine.noteOff(a.note, "pointer");
  }

  releaseAll() {
    for (const a of this.active.values()) if (a.note !== null) this.engine.noteOff(a.note, "pointer");
    this.active.clear();
  }
}
