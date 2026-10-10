/* UI wiring: builds the on-screen keyboard, connects the inputs to the engine, shows status. */
"use strict";
(function () {
  const $ = (sel) => document.querySelector(sel);
  const SETTINGS_KEY = "piano.settings.v1";
  const DEFAULTS = { volume: 80, reverb: 25, dynamics: 76, midiCurve: "linear", octave: 0, instrument: "sampled" };
  const UPPER_ROW = new Set(["KeyQ", "Digit2", "KeyW", "Digit3", "KeyE", "KeyR", "Digit5", "KeyT", "Digit6", "KeyY", "Digit7",
    "KeyU", "KeyI", "Digit9", "KeyO", "Digit0", "KeyP", "BracketLeft", "Equal", "BracketRight"]);
  const SOURCE_NAMES = { midi: "MIDI keyboard", keyboard: "computer keyboard", pointer: "mouse / touch / pen" };

  function loadSettings() {
    try { return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}) }; } catch (_) { return { ...DEFAULTS }; }
  }
  function saveSettings() { try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch (_) { /* private mode etc. */ } }

  const settings = loadSettings();
  const engine = new PianoEngine({ volume: settings.volume / 100, reverb: settings.reverb / 100 });
  const keyboard = new ComputerKeyboardInput(engine, { onChange: () => { renderLabels(); syncKeyboardControls(); } });
  const midi = new MidiInput(engine);
  const keysEl = $("#keys");
  const keyEls = buildKeyboard(keysEl);
  const pointer = new PointerInput(engine, keysEl).attach();

  const overlay = $("#overlay");
  const audioStatus = $("#audioStatus"), loadStatus = $("#loadStatus"), midiStatus = $("#midiStatus"), midiConnect = $("#midiConnect"), midiHelp = $("#midiHelp");
  const instrumentSel = $("#instrument"), volume = $("#volume"), reverb = $("#reverb"), dynamics = $("#dynamics"), midiCurve = $("#midiCurve");
  const sustainBtn = $("#sustainBtn"), velBar = $("#velBar"), velOut = $("#velOut"), velSource = $("#velSource");

  /* ---------- on-screen keyboard ---------- */

  function buildKeyboard(root) {
    const map = new Map();
    let whites = 0;
    for (let n = PIANO_NOTE_MIN; n <= PIANO_NOTE_MAX; n++) {
      const black = pianoIsBlackKey(n);
      const el = document.createElement("div");
      el.className = "key " + (black ? "black" : "white");
      el.dataset.note = String(n);
      el.title = pianoMidiToName(n);
      if (black) el.style.left = `calc(var(--ww) * ${whites} - var(--bw) / 2)`;
      else whites++;
      if (!black && n % 12 === 0) {
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = pianoMidiToName(n);
        el.appendChild(name);
        if (n === 60) el.classList.add("c4");
      }
      const label = document.createElement("span");
      label.className = "label";
      el.appendChild(label);
      root.appendChild(el);
      map.set(n, el);
    }
    return map;
  }

  function renderLabels() {
    for (const el of keyEls.values()) {
      el.classList.remove("mapped", "upper", "lower");
      el.querySelector(".label").textContent = "";
    }
    // Lower row first, so where the rows overlap (C4-E4) the upper row's label wins.
    for (const { code, note, label } of keyboard.mapping()) {
      const el = keyEls.get(note);
      if (!el) continue;
      const upper = UPPER_ROW.has(code);
      el.classList.remove("upper", "lower");
      el.classList.add("mapped", upper ? "upper" : "lower");
      el.querySelector(".label").textContent = label;
    }
  }

  /** Cool blue for a soft touch, through violet and red to hot orange for a hard one. */
  function velocityColor(v) {
    const t = (v - 1) / 126;
    return `hsl(${Math.round((205 + 175 * t) % 360)} 80% ${Math.round(60 - 6 * t)}%)`;
  }

  function showVelocity(v, source) {
    velBar.style.width = `${(v / 127) * 100}%`;
    velBar.style.background = velocityColor(v);
    velOut.textContent = `${v} · ${pianoDynamicName(v)}`;
    velSource.textContent = "from " + (SOURCE_NAMES[source] || source);
  }

  engine.on("noteon", ({ note, velocity, source }) => {
    const el = keyEls.get(note);
    if (el) {
      el.classList.remove("sus");
      el.classList.add("down");
      el.style.setProperty("--vc", velocityColor(velocity));
    }
    showVelocity(velocity, source);
  });
  engine.on("noteoff", ({ note, sustained }) => {
    const el = keyEls.get(note);
    if (!el) return;
    el.classList.remove("down");
    if (sustained) el.classList.add("sus");
  });
  engine.on("pedal", (p) => {
    sustainBtn.setAttribute("aria-pressed", String(p.sustain));
    if (!p.sustain && !p.sostenuto) for (const el of keyEls.values()) el.classList.remove("sus");
  });
  engine.on("allnotesoff", () => { for (const el of keyEls.values()) el.classList.remove("down", "sus"); });
  engine.on("instrument", ({ name }) => { instrumentSel.value = name; });

  /* ---------- controls ---------- */

  function setPill(el, state, text) { el.dataset.state = state; el.textContent = text; }

  function syncKeyboardControls() {
    dynamics.value = keyboard.velocity;
    $("#dynamicsOut").textContent = `${pianoDynamicName(keyboard.velocity)} · ${keyboard.velocity}`;
    $("#octaveVal").textContent = (keyboard.octave > 0 ? "+" : "") + keyboard.octave;
    $("#octaveOut").textContent = `${pianoMidiToName(keyboard.noteForCode("KeyZ"))} – ${pianoMidiToName(keyboard.noteForCode("BracketRight"))}`;
    settings.dynamics = keyboard.velocity;
    settings.octave = keyboard.octave;
    saveSettings();
  }

  volume.value = settings.volume;
  $("#volumeOut").textContent = settings.volume;
  volume.addEventListener("input", () => {
    settings.volume = Number(volume.value);
    $("#volumeOut").textContent = volume.value;
    engine.setVolume(settings.volume / 100);
    saveSettings();
  });

  reverb.value = settings.reverb;
  $("#reverbOut").textContent = settings.reverb;
  reverb.addEventListener("input", () => {
    settings.reverb = Number(reverb.value);
    $("#reverbOut").textContent = reverb.value;
    engine.setReverb(settings.reverb / 100);
    saveSettings();
  });

  dynamics.addEventListener("input", () => keyboard.setVelocity(Number(dynamics.value)));

  midiCurve.value = settings.midiCurve;
  midi.setCurve(settings.midiCurve);
  midiCurve.addEventListener("change", () => {
    settings.midiCurve = midiCurve.value;
    midi.setCurve(settings.midiCurve);
    saveSettings();
  });

  instrumentSel.value = settings.instrument;
  if (settings.instrument === "synth") engine.setInstrument("synth");
  instrumentSel.addEventListener("change", () => {
    engine.setInstrument(instrumentSel.value);
    settings.instrument = instrumentSel.value;
    saveSettings();
  });

  $("#octDown").addEventListener("click", () => keyboard.setOctave(keyboard.octave - 1));
  $("#octUp").addEventListener("click", () => keyboard.setOctave(keyboard.octave + 1));
  sustainBtn.addEventListener("click", () => engine.setSustain(!engine.pedals.sustain));
  midiConnect.addEventListener("click", () => midi.connect());

  // Give focus back to the page after using a control, so typing keeps playing notes.
  for (const el of document.querySelectorAll("button, select, input[type=range]")) {
    el.addEventListener(el.tagName === "BUTTON" ? "click" : "change", () => el.blur());
  }

  keyboard.velocity = pianoClampVelocity(settings.dynamics);
  keyboard.octave = pianoClamp(Math.round(settings.octave) || 0, -2, 2);
  keyboard.attach();
  renderLabels();
  syncKeyboardControls();
  keyboard.loadLabels();

  /* ---------- status ---------- */

  function updateAudioStatus() {
    const running = engine.ctx.state === "running";
    setPill(audioStatus, running ? "ok" : "idle", running ? "Audio: running" : "Audio: click to start");
  }
  engine.ctx.addEventListener("statechange", updateAudioStatus);
  updateAudioStatus();

  function updateMidiStatus() {
    const names = midi.deviceNames();
    switch (midi.status) {
      case "unsupported": setPill(midiStatus, "warn", "MIDI: not supported in this browser (use Chrome, Edge or Firefox)"); break;
      case "requesting": setPill(midiStatus, "loading", "MIDI: waiting for permission…"); break;
      case "denied": setPill(midiStatus, "bad", "MIDI: blocked. Allow MIDI for this site, then retry"); break;
      case "framed": setPill(midiStatus, "warn", "MIDI: unavailable inside this embedded page"); break;
      case "error": setPill(midiStatus, "bad", "MIDI: " + ((midi.error && midi.error.message) || "error")); break;
      case "ready":
        if (names.length) setPill(midiStatus, "ok", "MIDI: " + names.join(", "));
        else setPill(midiStatus, "warn", "MIDI: no keyboard found. Plug one in; it connects automatically");
        break;
      default: setPill(midiStatus, "idle", "MIDI: not connected");
    }
    keysEl.classList.toggle("midi-active", midi.status === "ready" && names.length > 0);
    midiConnect.hidden = midi.status === "ready" || midi.status === "unsupported" || midi.status === "requesting" || midi.status === "framed";
    midiHelp.hidden = midi.status !== "framed";
  }
  midi.onChange(updateMidiStatus);
  updateMidiStatus();

  /* ---------- samples ---------- */

  let readyResolve;
  const ready = new Promise((resolve) => { readyResolve = resolve; });
  const bank = window.PIANO_SAMPLE_BANK;
  if (bank) {
    setPill(loadStatus, "loading", "Loading samples 0%");
    engine.on("load", ({ progress }) => setPill(loadStatus, "loading", `Loading samples ${Math.round(progress * 100)}%`));
    engine.loadBank(bank).then((ok) => {
      const failed = engine.sampler.loadErrors.length;
      if (ok) setPill(loadStatus, failed ? "warn" : "ok", failed ? `Grand piano ready (${failed} samples failed)` : "Grand piano ready");
      else { setPill(loadStatus, "bad", "Samples failed to decode. Using the synth piano"); engine.setInstrument("synth"); }
      if (failed) console.warn("Sample decode errors", engine.sampler.loadErrors);
      readyResolve(ok);
    });
  } else {
    setPill(loadStatus, "warn", "samples.js not found. Synth piano only");
    engine.setInstrument("synth");
    readyResolve(false);
  }

  /* ---------- start ---------- */

  let started = false;
  async function start() {
    if (started) return;
    started = true;
    overlay.classList.add("hidden");
    await engine.resume();
    updateAudioStatus();
    if (midi.status === "idle") midi.connect();
  }
  window.addEventListener("pointerdown", start, { capture: true });
  window.addEventListener("keydown", start, { capture: true });
  // Mobile browsers can suspend the context again (e.g. after a phone call); any tap brings it back.
  window.addEventListener("pointerdown", () => { if (started && engine.ctx.state !== "running") engine.resume(); }, { capture: true });
  window.addEventListener("blur", () => pointer.releaseAll());

  window.pianoApp = { engine, keyboard, midi, pointer, keyEls, settings, start, ready, velocityColor };
})();
