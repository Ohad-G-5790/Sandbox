/*
 * End-to-end checks for the piano, run in headless Chromium via Playwright.
 *
 *   node test/run.mjs            (after `npm install` in this folder)
 *
 * Opens index.html from file:// exactly as a user double-clicking it would, then:
 *   - checks every sample decodes and the page logs no errors
 *   - plays notes through the computer-keyboard, MIDI and pointer code paths
 *   - renders audio offline to verify that velocity changes loudness and brightness
 *   - saves a screenshot to test-results/piano.png
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const url = pathToFileURL(join(here, "..", "index.html")).href;
const outDir = join(here, "..", "test-results");
mkdirSync(outDir, { recursive: true });

let failures = 0;
function check(name, cond, info = "") {
  if (cond) console.log(`  ok    ${name}`);
  else { failures++; console.log(`  FAIL  ${name}${info ? "  (" + info + ")" : ""}`); }
}
const fmt = (x) => (typeof x === "number" ? x.toFixed(4) : JSON.stringify(x));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

console.log("Loading " + url);
const t0 = Date.now();
await page.goto(url);
const loaded = await page.evaluate(() => window.pianoApp.ready);
console.log(`  samples decoded in ${Date.now() - t0} ms`);

console.log("\nLoading");
check("sample bank decoded", loaded === true);
const info = await page.evaluate(() => {
  const s = window.pianoApp.engine.sampler;
  const offsets = [...s.buffers.values()].map((b) => b.offset);
  return { buffers: s.buffers.size, errors: s.loadErrors, layers: s.layers, maxOffset: Math.max(...offsets), minOffset: Math.min(...offsets) };
});
check("120 sample buffers (30 notes x 4 layers)", info.buffers === 120, fmt(info));
check("no decode errors", info.errors.length === 0, fmt(info.errors));
check("onset trimming within 0..50 ms", info.minOffset >= 0 && info.maxOffset < 0.05, `min ${fmt(info.minOffset)} max ${fmt(info.maxOffset)}`);
check("velocity layers 4, 8, 12, 16", JSON.stringify(info.layers) === "[4,8,12,16]", fmt(info.layers));

console.log("\nStart gesture");
await page.mouse.click(700, 450);   // the overlay hides itself on pointerdown, so click by coordinates
await page.waitForFunction(() => window.pianoApp.engine.ctx.state === "running", null, { timeout: 5000 }).catch(() => {});
check("AudioContext running after the click", await page.evaluate(() => window.pianoApp.engine.ctx.state) === "running");
check("overlay hidden", await page.$eval("#overlay", (el) => el.classList.contains("hidden")));
check("audio status pill shows running", (await page.textContent("#audioStatus")).includes("running"));
check("load status pill shows ready", (await page.textContent("#loadStatus")).includes("ready"));

const voices = () => page.evaluate(() => window.pianoApp.engine.sampler.voices.map((v) => ({ note: v.note, velocity: v.velocity, state: v.state })));

console.log("\nComputer keyboard");
await page.keyboard.down("KeyZ");
let v = await voices();
check("Z plays C3 (MIDI 48) at the default mf (76)", v.length === 1 && v[0].note === 48 && v[0].velocity === 76 && v[0].state === "held", fmt(v));
check("C3 key lit on screen", await page.$eval('[data-note="48"]', (el) => el.classList.contains("down")));
check("velocity meter shows 76", (await page.textContent("#velOut")).startsWith("76"));
await page.keyboard.up("KeyZ");
v = await voices();
check("key up releases the note", v.length === 1 && v[0].state === "released", fmt(v));
check("C3 key unlit", await page.$eval('[data-note="48"]', (el) => !el.classList.contains("down")));

await page.keyboard.down("Shift");
await page.keyboard.down("KeyQ");
v = (await voices()).filter((x) => x.note === 60);
check("Shift accent: Q plays C4 at 104", v.length === 1 && v[0].velocity === 104, fmt(v));
await page.keyboard.up("KeyQ");
await page.keyboard.up("Shift");

await page.keyboard.press("ArrowUp");
check("ArrowUp steps dynamics mf -> f (92)", (await page.evaluate(() => window.pianoApp.keyboard.velocity)) === 92);
check("dynamics slider follows", (await page.inputValue("#dynamics")) === "92");
await page.keyboard.press("ArrowDown");
check("ArrowDown steps back to mf (76)", (await page.evaluate(() => window.pianoApp.keyboard.velocity)) === 76);
await page.keyboard.press("ArrowRight");
check("ArrowRight shifts the Z row up an octave (Z = C4)", (await page.evaluate(() => window.pianoApp.keyboard.noteForCode("KeyZ"))) === 60);
check("C4 key now carries the Z label", (await page.$eval('[data-note="60"] .label', (el) => el.textContent)) === "Z");
await page.keyboard.press("ArrowLeft");
check("ArrowLeft restores (Z = C3)", (await page.evaluate(() => window.pianoApp.keyboard.noteForCode("KeyZ"))) === 48);
check("physical layout: Digit2 is C#4 (61)", (await page.evaluate(() => window.pianoApp.keyboard.noteForCode("Digit2"))) === 61);
check("gap keys (A, F, K) play nothing", (await page.evaluate(() => [window.pianoApp.keyboard.noteForCode("KeyA"), window.pianoApp.keyboard.noteForCode("KeyF"), window.pianoApp.keyboard.noteForCode("KeyK")].every((n) => n === null))));

await page.keyboard.down("Space");
await page.keyboard.press("KeyX");
v = (await voices()).filter((x) => x.note === 50);
check("Space holds a released note (sustain pedal)", v.length === 1 && v[0].state === "sustained", fmt(v));
check("sustain button reflects the pedal", (await page.getAttribute("#sustainBtn", "aria-pressed")) === "true");
check("D3 key shown as held by the pedal", await page.$eval('[data-note="50"]', (el) => el.classList.contains("sus")));
await page.keyboard.up("Space");
v = (await voices()).filter((x) => x.note === 50);
check("pedal up releases it", v.length === 1 && v[0].state === "released", fmt(v));

await page.keyboard.down("KeyB");
await page.evaluate(() => window.dispatchEvent(new Event("blur")));
v = (await voices()).filter((x) => x.note === 55);
check("window blur releases held keys (no stuck notes)", v.length === 1 && v[0].state === "released", fmt(v));
await page.keyboard.up("KeyB");

console.log("\nMIDI");
const midi = (bytes) => page.evaluate((b) => window.pianoApp.midi.handleMessage({ data: new Uint8Array(b) }), bytes);
await midi([0x90, 64, 100]);
v = (await voices()).filter((x) => x.note === 64);
check("note on, velocity 100", v.length === 1 && v[0].velocity === 100 && v[0].state === "held", fmt(v));
check("meter reports the MIDI velocity", (await page.textContent("#velOut")).startsWith("100") && (await page.textContent("#velSource")).includes("MIDI"));
await midi([0xb0, 64, 127]);
await midi([0x80, 64, 0]);
v = (await voices()).filter((x) => x.note === 64);
check("CC64 sustain holds the note after note off", v.length === 1 && v[0].state === "sustained", fmt(v));
await midi([0xb0, 64, 0]);
v = (await voices()).filter((x) => x.note === 64);
check("CC64 off releases it", v.length === 1 && v[0].state === "released", fmt(v));
await midi([0x91, 67, 90]);
await midi([0x91, 67, 0]);
v = (await voices()).filter((x) => x.note === 67);
check("channel 2 works; note on with velocity 0 is a note off", v.length === 1 && v[0].velocity === 90 && v[0].state === "released", fmt(v));
await midi([0xb0, 67, 127]);
await midi([0x90, 62, 100]);
v = (await voices()).filter((x) => x.note === 62);
check("CC67 soft pedal keeps the reported velocity but plays softer", v.length === 1 && v[0].velocity === 100, fmt(v));
check("soft pedal state set", await page.evaluate(() => window.pianoApp.engine.pedals.soft === true && window.pianoApp.engine.sampler.soft === true));
await midi([0xb0, 67, 0]);
await midi([0x80, 62, 0]);
await midi([0x90, 65, 80]);
await midi([0xb0, 66, 127]);     // sostenuto catches F4
await midi([0x90, 69, 80]);      // A4 played after: not caught
await midi([0x80, 65, 0]);
await midi([0x80, 69, 0]);
v = await voices();
check("CC66 sostenuto holds only notes down when pressed", v.find((x) => x.note === 65).state === "sostenuto" && v.find((x) => x.note === 69).state === "released", fmt(v));
await midi([0xb0, 66, 0]);
check("sostenuto off releases", (await voices()).find((x) => x.note === 65).state === "released");
await midi([0x90, 72, 100]);
await midi([0xb0, 123, 0]);
check("CC123 all notes off", (await voices()).every((x) => x.state === "released"));
const curve = await page.evaluate(() => {
  const m = window.pianoApp.midi;
  m.setCurve("soft"); const soft = m.applyCurve(64);
  m.setCurve("hard"); const hard = m.applyCurve(64);
  m.setCurve("linear"); const lin = m.applyCurve(64);
  return { soft, hard, lin };
});
check("velocity curves: soft > linear > hard at v=64", curve.soft > curve.lin && curve.lin === 64 && curve.hard < 64, fmt(curve));

console.log("\nMouse / touch");
const box = await page.$eval('[data-note="60"]', (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
const cx = box.x + box.w / 2;
await page.mouse.move(cx, box.y + box.h * 0.08);
await page.mouse.down();
const softV = await page.evaluate(() => window.pianoApp.pointer.lastVelocity);
await page.mouse.up();
await page.mouse.move(cx, box.y + box.h * 0.95);
await page.mouse.down();
const loudV = await page.evaluate(() => window.pianoApp.pointer.lastVelocity);
v = (await voices()).filter((x) => x.note === 60 && x.state === "held");
check("click near the front edge is loud, near the top is soft", softV < 45 && loudV > 110, `top ${softV}, bottom ${loudV}`);
check("the clicked note is sounding", v.length === 1 && v[0].velocity === loudV, fmt(v));
const box2 = await page.$eval('[data-note="62"]', (el) => { const r = el.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height * 0.95 }; });
await page.mouse.move(box2.x, box2.y, { steps: 4 });
v = await voices();
check("dragging to the next key plays a glissando", v.some((x) => x.note === 60 && x.state === "released") && v.some((x) => x.note === 62 && x.state === "held"), fmt(v));
await page.mouse.up();
check("mouse up releases", (await voices()).every((x) => x.state === "released"));
const bk = await page.$eval('[data-note="61"]', (el) => { const r = el.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height * 0.5 }; });
await page.mouse.click(bk.x, bk.y);
check("black keys are clickable (C#4)", (await voices()).some((x) => x.note === 61), fmt(await voices()));

console.log("\nVoice housekeeping");
await page.evaluate(() => { for (let n = 48; n < 60; n++) window.pianoApp.engine.noteOn(n, 100, "app"); });
check("12 notes sounding", (await voices()).filter((x) => x.state === "held").length === 12);
await page.evaluate(() => { for (let n = 48; n < 60; n++) window.pianoApp.engine.noteOff(n, "app"); });
await page.waitForFunction(() => window.pianoApp.engine.voiceCount === 0, null, { timeout: 8000 }).catch(() => {});
check("released voices are cleaned up (no leak)", (await page.evaluate(() => window.pianoApp.engine.voiceCount)) === 0, fmt(await voices()));
const stolen = await page.evaluate(() => {
  const s = window.pianoApp.engine.sampler;
  for (let n = 21; n <= 108; n++) window.pianoApp.engine.noteOn(n, 90, "app");
  const live = s.voices.filter((x) => !x.dying).length;
  for (let n = 21; n <= 108; n++) window.pianoApp.engine.noteOff(n, "app");
  return { live, max: s.maxVoices };
});
check("polyphony limit enforced by voice stealing", stolen.live <= stolen.max, fmt(stolen));

console.log("\nOffline render: does velocity change the sound?");
const render = await page.evaluate(async () => {
  function fft(re, im) {
    const n = re.length;
    for (let i = 1, j = 0; i < n; i++) {
      let bit = n >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
    }
    for (let len = 2; len <= n; len <<= 1) {
      const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang);
      for (let i = 0; i < n; i += len) {
        let cr = 1, ci = 0;
        for (let j = 0; j < len / 2; j++) {
          const a = i + j, b = a + len / 2;
          const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
          re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
          const nr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = nr;
        }
      }
    }
  }
  function analyze(buf) {
    const rate = buf.sampleRate, n = buf.length, mono = new Float32Array(n);
    for (let c = 0; c < buf.numberOfChannels; c++) { const d = buf.getChannelData(c); for (let i = 0; i < n; i++) mono[i] += d[i] / buf.numberOfChannels; }
    const rms = (a, b) => { let s = 0; const i0 = Math.floor(a * rate), i1 = Math.min(n, Math.floor(b * rate)); for (let i = i0; i < i1; i++) s += mono[i] * mono[i]; return Math.sqrt(s / Math.max(1, i1 - i0)); };
    let peak = 0, nan = false;
    for (let i = 0; i < n; i++) { const a = Math.abs(mono[i]); if (a > peak) peak = a; if (Number.isNaN(mono[i])) nan = true; }
    const N = 4096; let num = 0, den = 0;
    for (let start = 0; start + N <= Math.min(n, rate * 0.5); start += N / 2) {
      const re = new Float64Array(N), im = new Float64Array(N);
      for (let i = 0; i < N; i++) re[i] = mono[start + i] * (0.5 - 0.5 * Math.cos((2 * Math.PI * i) / N));
      fft(re, im);
      for (let k = 1; k < N / 2; k++) { const mag = Math.hypot(re[k], im[k]); num += (k * rate / N) * mag; den += mag; }
    }
    return { rms: rms(0, buf.duration), peak, nan, centroid: den ? num / den : 0, rmsAt: (a, b) => rms(a, b), _rms: rms };
  }
  async function renderNotes(kind, notes, seconds, script) {
    const ctx = new OfflineAudioContext(2, Math.round(44100 * seconds), 44100);
    const bus = new AudioBus(ctx, { reverb: 0, volume: 1 });
    const inst = kind === "sampled" ? window.pianoApp.engine.sampler.clone(ctx, bus.input) : new SynthPiano(ctx, bus.input);
    script(inst, notes);
    const buf = await ctx.startRendering();
    const a = analyze(buf);
    return { rms: a.rms, peak: a.peak, nan: a.nan, centroid: a.centroid, early: a._rms(0.4, 0.5), late: a._rms(0.9, 1.0), voicesLeft: inst.voices.length };
  }
  const out = {};
  for (const kind of ["sampled", "synth"]) {
    out[kind] = {};
    for (const vel of [20, 64, 120]) out[kind]["v" + vel] = await renderNotes(kind, [60], 1.0, (inst) => inst.noteOn(60, vel, 0));
    out[kind].bass = await renderNotes(kind, [28], 1.0, (inst) => inst.noteOn(28, 100, 0));
    out[kind].treble = await renderNotes(kind, [100], 1.0, (inst) => inst.noteOn(100, 100, 0));
    out[kind].chord = await renderNotes(kind, [], 1.0, (inst) => { for (const n of [36, 43, 48, 52, 55, 60, 64, 67, 72, 76]) inst.noteOn(n, 127, 0); });
    out[kind].held = await renderNotes(kind, [60], 1.5, (inst) => { inst.noteOn(60, 100, 0); });
    out[kind].released = await renderNotes(kind, [60], 1.5, (inst) => { inst.noteOn(60, 100, 0); inst.noteOff(60, 0.5); });
    out[kind].sustained = await renderNotes(kind, [60], 1.5, (inst) => { inst.setSustain(true, 0); inst.noteOn(60, 100, 0); inst.noteOff(60, 0.5); });
  }
  const s = window.pianoApp.engine.sampler;
  out.plan = { pp: s.plan(60, 20), mf: s.plan(60, 76), ff: s.plan(60, 127), treble: s.plan(100, 60) };
  return out;
});
for (const kind of ["sampled", "synth"]) {
  const r = render[kind];
  const line = (k) => `${k}: rms ${fmt(r[k].rms)} peak ${fmt(r[k].peak)} centroid ${Math.round(r[k].centroid)} Hz`;
  console.log(`  ${kind}: ${line("v20")} | ${line("v64")} | ${line("v120")}`);
  check(`${kind}: louder with velocity (20 < 64 < 120)`, r.v20.rms * 1.3 < r.v64.rms && r.v64.rms * 1.3 < r.v120.rms);
  check(`${kind}: brighter with velocity (spectral centroid rises)`, r.v20.centroid < r.v64.centroid && r.v64.centroid < r.v120.centroid, `${Math.round(r.v20.centroid)} / ${Math.round(r.v64.centroid)} / ${Math.round(r.v120.centroid)} Hz`);
  check(`${kind}: wide dynamic range (>= 12 dB between v20 and v120)`, 20 * Math.log10(r.v120.rms / r.v20.rms) >= 12, `${(20 * Math.log10(r.v120.rms / r.v20.rms)).toFixed(1)} dB`);
  check(`${kind}: bass and treble notes both sound`, r.bass.rms > 0.01 && r.treble.rms > 0.003, `bass ${fmt(r.bass.rms)} treble ${fmt(r.treble.rms)}`);
  check(`${kind}: 10-note ff chord does not clip (limiter)`, r.chord.peak < 1 && !r.chord.nan, `peak ${fmt(r.chord.peak)}`);
  // A real piano tone decays quickly at first, so compare against a note that is simply held.
  check(`${kind}: note off damps the note (vs held)`, r.released.late < r.held.late * 0.1, `held ${fmt(r.held.late)} released ${fmt(r.released.late)}`);
  check(`${kind}: sustain pedal keeps it ringing like a held note`, Math.abs(r.sustained.late - r.held.late) < r.held.late * 0.05, `held ${fmt(r.held.late)} sustained ${fmt(r.sustained.late)}`);
  check(`${kind}: no NaN in output`, !r.v120.nan && !r.chord.nan);
}
const p = render.plan;
check("plan: pp uses only the softest layer, darkened", p.pp.layers.length === 1 && p.pp.layers[0].layer === 4 && p.pp.cutoff < 20000, fmt(p.pp));
check("plan: mf crossfades layers 8 and 12", p.mf.layers.map((l) => l.layer).join(",") === "8,12" && Math.abs(p.mf.layers[0].gain ** 2 + p.mf.layers[1].gain ** 2 - 1) < 1e-6, fmt(p.mf));
check("plan: ff uses only the hardest layer", p.ff.layers.length === 1 && p.ff.layers[0].layer === 16 && p.ff.gain === 1, fmt(p.ff));
check("plan: nearest sample is at most 1 semitone away", Math.abs(p.treble.sampleMidi - 100) <= 1, fmt(p.treble.sampleMidi));

console.log("\nScreenshot");
await page.evaluate(() => { window.pianoApp.engine.setSustain(true); });
for (const key of ["KeyZ", "KeyC", "KeyB", "KeyE", "KeyT", "KeyU"]) await page.keyboard.down(key);
await page.evaluate(() => { window.pianoApp.midi.handleMessage({ data: new Uint8Array([0x90, 86, 120]) }); });
await page.waitForTimeout(100);
await page.screenshot({ path: join(outDir, "piano.png") });
for (const key of ["KeyZ", "KeyC", "KeyB", "KeyE", "KeyT", "KeyU"]) await page.keyboard.up(key);
await page.evaluate(() => window.pianoApp.engine.panic());
console.log("  saved " + join(outDir, "piano.png"));

console.log("\nPage health");
check("no page errors or console errors", errors.length === 0, errors.join(" | "));

await browser.close();
console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
process.exit(failures ? 1 : 0);
