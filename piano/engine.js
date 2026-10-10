/*
 * Piano engine: Web Audio sampled grand piano with a synthesized fallback.
 *
 * Written as a classic script (no ES modules, no fetch) so that index.html can be
 * opened straight from the file system without a web server.
 *
 * Structure
 *   AudioBus      shared output chain: dry/wet reverb mix -> master -> limiter
 *   Instrument    pedal + voice bookkeeping shared by both instruments
 *   Voice         one sounding note (envelope, sources, release / steal logic)
 *   SampledPiano  Salamander Grand Piano samples, 4 velocity layers, crossfaded
 *   SynthPiano    additive synthesis fallback, available instantly
 *   PianoEngine   facade used by the UI and the input handlers
 */
"use strict";

const PIANO_NOTE_MIN = 21;   // A0
const PIANO_NOTE_MAX = 108;  // C8
const PIANO_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

function pianoClamp(x, lo, hi) { return x < lo ? lo : x > hi ? hi : x; }
function pianoClampVelocity(v) { return pianoClamp(Math.round(v), 1, 127); }
function pianoMidiToFreq(n) { return 440 * Math.pow(2, (n - 69) / 12); }
function pianoMidiToName(n) { return PIANO_NOTE_NAMES[n % 12] + (Math.floor(n / 12) - 1); }
function pianoIsBlackKey(n) { const p = n % 12; return p === 1 || p === 3 || p === 6 || p === 8 || p === 10; }

function pianoBase64ToBytes(b64) {
  if (typeof Uint8Array.fromBase64 === "function") return Uint8Array.fromBase64(b64);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/* ------------------------------------------------------------------------ */

class AudioBus {
  constructor(ctx, { reverb = 0.25, volume = 0.8 } = {}) {
    this.ctx = ctx;
    this.input = ctx.createGain();
    this.dry = ctx.createGain();
    this.send = ctx.createGain();
    this.convolver = ctx.createConvolver();
    this.wet = ctx.createGain();
    this.master = ctx.createGain();
    this.limiter = ctx.createDynamicsCompressor();

    // Gentle safety limiter so big chords at ff never clip.
    this.limiter.threshold.value = -8;
    this.limiter.knee.value = 6;
    this.limiter.ratio.value = 12;
    this.limiter.attack.value = 0.002;
    this.limiter.release.value = 0.12;

    this.convolver.buffer = AudioBus.makeImpulseResponse(ctx, 2.4, 12);

    this.input.connect(this.dry);
    this.dry.connect(this.master);
    this.input.connect(this.send);
    this.send.connect(this.convolver);
    this.convolver.connect(this.wet);
    this.wet.connect(this.master);
    this.master.connect(this.limiter);
    this.limiter.connect(ctx.destination);

    this.reverb = reverb;
    this.volume = volume;
    this.pedalBoost = 1;
    this.setReverb(reverb);
    this.setVolume(volume);
  }

  /** amount 0..1 */
  setReverb(amount) {
    this.reverb = pianoClamp(Number(amount) || 0, 0, 1);
    this._applyReverb();
  }

  _applyReverb() {
    const t = this.ctx.currentTime;
    const a = this.reverb;
    this.wet.gain.setTargetAtTime(a * 0.9 * this.pedalBoost, t, 0.05);
    this.dry.gain.setTargetAtTime(1 - a * 0.35, t, 0.05);
  }

  /** A sustained piano has every string resonating; a little extra reverb while the pedal is down mimics that. */
  setPedalResonance(down) {
    this.pedalBoost = down ? 1.35 : 1;
    this._applyReverb();
  }

  /** v 0..1 */
  setVolume(v) {
    this.volume = pianoClamp(Number(v) || 0, 0, 1);
    this.master.gain.setTargetAtTime(this.volume * this.volume, this.ctx.currentTime, 0.02);
  }

  /** Synthetic stereo room impulse response: decaying noise that darkens over time, plus a few early reflections. */
  static makeImpulseResponse(ctx, seconds = 2.4, preDelayMs = 12) {
    const rate = ctx.sampleRate;
    const len = Math.floor(rate * seconds);
    const pre = Math.floor((rate * preDelayMs) / 1000);
    const buffer = ctx.createBuffer(2, len, rate);
    const taps = [[0.009, 0.5], [0.017, 0.42], [0.026, 0.36], [0.038, 0.3], [0.051, 0.24], [0.067, 0.18]];
    let energy = 0;
    for (let ch = 0; ch < 2; ch++) {
      const d = buffer.getChannelData(ch);
      let seed = 1234567 + ch * 999331;
      const rnd = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return (seed / 4294967296) * 2 - 1; };
      let lp = 0;
      for (let i = pre; i < len; i++) {
        const t = (i - pre) / (len - pre);
        const env = Math.exp(-6.9 * t);          // about -60 dB at the end
        const a = 0.12 + 0.8 * t;                // one-pole lowpass: bright early, dark late
        lp += (rnd() - lp) * (1 - a);
        d[i] = lp * env;
      }
      for (let k = 0; k < taps.length; k++) {
        const [sec, g] = taps[k];
        const idx = pre + Math.floor(rate * sec * (ch === 0 ? 1 : 1.07));
        if (idx < len) d[idx] += g * (k % 2 ? -1 : 1) * 0.6;
      }
      for (let i = 0; i < len; i++) energy += d[i] * d[i];
    }
    const norm = 1 / Math.sqrt(energy / 2);
    for (let ch = 0; ch < 2; ch++) {
      const d = buffer.getChannelData(ch);
      for (let i = 0; i < len; i++) d[i] *= norm;
    }
    return buffer;
  }
}

/* ------------------------------------------------------------------------ */

class Voice {
  /**
   * @param instrument  owning Instrument
   * @param env         GainNode that scales the whole voice (attack + release live here)
   * @param sources     started AudioScheduledSourceNodes
   * @param nodes       extra nodes to disconnect when the voice ends
   * @param releaseSeconds  damper release time
   * @param hasDamper   false for the top of the keyboard, where real pianos have no dampers
   * @param naturalEnd  context time at which every source has finished anyway
   */
  constructor(instrument, { env, sources, nodes = [], releaseSeconds, hasDamper = true, naturalEnd }) {
    this.instrument = instrument;
    this.env = env;
    this.sources = sources;
    this.nodes = nodes;
    this.releaseSeconds = releaseSeconds;
    this.hasDamper = hasDamper;
    this.state = "held";      // held | sustained | sostenuto | released
    this.dying = false;       // quick fade in progress (retrigger or voice stealing)
    this.note = 0;
    this.velocity = 0;
    this.startTime = 0;
    this.stopTime = naturalEnd;
    this.disposed = false;
    let pending = sources.length;
    const done = () => { if (--pending <= 0) this._dispose(); };
    for (const s of sources) {
      s.onended = done;
      try { s.stop(naturalEnd); } catch (_) { /* already stopped */ }
    }
  }

  /** Damper falls: natural release. */
  release(time) {
    if (this.state === "released") return;
    this.state = "released";
    if (!this.hasDamper || this.dying) return;
    const rel = this.releaseSeconds;
    this.env.gain.setTargetAtTime(0, time, rel / 4);
    this._stopAt(time + rel * 1.6);
  }

  /** Quick fade, used when the same note is struck again or a voice is stolen. */
  fade(time, seconds = 0.06) {
    if (this.dying) return;
    this.dying = true;
    this.state = "released";
    this.env.gain.cancelScheduledValues(time);
    this.env.gain.setTargetAtTime(0, time, seconds / 4);
    this._stopAt(time + seconds * 1.6);
  }

  kill(time) {
    this.dying = true;
    this.state = "released";
    this.env.gain.cancelScheduledValues(time);
    this.env.gain.setValueAtTime(0, time);
    this._stopAt(time);
  }

  _stopAt(t) {
    if (t >= this.stopTime) return;
    this.stopTime = t;
    for (const s of this.sources) {
      try { s.stop(t); } catch (_) { /* some engines refuse a second stop(); the gain is already zero */ }
    }
  }

  _dispose() {
    if (this.disposed) return;
    this.disposed = true;
    try { this.env.disconnect(); } catch (_) { /* ignore */ }
    for (const n of this.nodes) { try { n.disconnect(); } catch (_) { /* ignore */ } }
    this.instrument._removeVoice(this);
  }
}

/* ------------------------------------------------------------------------ */

class Instrument {
  constructor(ctx, destination, { maxVoices = 48 } = {}) {
    this.ctx = ctx;
    this.output = ctx.createGain();
    if (destination) this.output.connect(destination);
    this.maxVoices = maxVoices;
    this.voices = [];
    this.sustain = false;
    this.sostenuto = false;
    this.soft = false;
    this.heldNotes = new Set();
    this.sostenutoNotes = new Set();
  }

  /** Subclasses build the audio graph for one note and return a Voice (or null if they cannot play it). */
  createVoice(/* note, velocity, time */) { throw new Error("createVoice is abstract"); }
  canPlay(/* note */) { return true; }

  noteOn(note, velocity, time = this.ctx.currentTime) {
    if (!(note >= PIANO_NOTE_MIN && note <= PIANO_NOTE_MAX)) return null;
    velocity = pianoClampVelocity(velocity);
    const played = this.soft ? pianoClampVelocity(velocity * 0.72) : velocity; // una corda: fewer strings struck
    for (const v of this.voices) if (v.note === note && !v.dying) v.fade(time, 0.08);
    const voice = this.createVoice(note, played, time);
    if (!voice) return null;
    voice.note = note;
    voice.velocity = velocity;
    voice.startTime = time;
    this.voices.push(voice);
    this.heldNotes.add(note);
    this._enforcePolyphony(time);
    return voice;
  }

  noteOff(note, time = this.ctx.currentTime) {
    this.heldNotes.delete(note);
    for (const v of this.voices) {
      if (v.note !== note || v.state !== "held") continue;
      if (this.sostenutoNotes.has(note)) v.state = "sostenuto";
      else if (this.sustain) v.state = "sustained";
      else v.release(time);
    }
  }

  setSustain(down, time = this.ctx.currentTime) {
    down = !!down;
    if (down === this.sustain) return;
    this.sustain = down;
    if (!down) for (const v of this.voices) if (v.state === "sustained") v.release(time);
  }

  setSostenuto(down, time = this.ctx.currentTime) {
    down = !!down;
    if (down === this.sostenuto) return;
    this.sostenuto = down;
    if (down) {
      this.sostenutoNotes = new Set(this.heldNotes);
      return;
    }
    for (const v of this.voices) {
      if (v.state !== "sostenuto") continue;
      if (this.sustain) v.state = "sustained";
      else v.release(time);
    }
    this.sostenutoNotes.clear();
  }

  setSoft(down) { this.soft = !!down; }

  allNotesOff(time = this.ctx.currentTime) {
    for (const v of this.voices) v.release(time);
    this.heldNotes.clear();
    this.sostenutoNotes.clear();
  }

  panic() {
    const t = this.ctx.currentTime;
    for (const v of this.voices.slice()) v.kill(t);
    this.heldNotes.clear();
    this.sostenutoNotes.clear();
  }

  get voiceCount() { return this.voices.length; }

  _enforcePolyphony(time) {
    const live = this.voices.filter((v) => !v.dying);
    const over = live.length - this.maxVoices;
    if (over <= 0) return;
    const rank = { released: 0, sustained: 1, sostenuto: 2, held: 3 };
    live.sort((a, b) => rank[a.state] - rank[b.state] || a.startTime - b.startTime);
    for (let i = 0; i < over; i++) live[i].fade(time, 0.05);
  }

  _removeVoice(voice) {
    const i = this.voices.indexOf(voice);
    if (i >= 0) this.voices.splice(i, 1);
  }
}

/* ------------------------------------------------------------------------ */

/**
 * Salamander Grand Piano V3 (Alexander Holm, CC BY 3.0), four velocity layers.
 * Velocity picks the layer pair and crossfades between them, so soft notes use a
 * genuinely soft recording rather than a quiet loud one.
 */
class SampledPiano extends Instrument {
  constructor(ctx, destination, options = {}) {
    super(ctx, destination, { maxVoices: options.maxVoices ?? 40 });
    this.output.gain.value = 0.6;
    this.buffers = options.buffers || new Map();      // "midi/layer" -> { buffer, offset }
    this.layers = options.layers || [];
    this.sampleNotes = options.sampleNotes || [];
    this.ready = options.ready || false;
    this.progress = this.ready ? 1 : 0;
    this.loadErrors = [];
    this.bank = null;
  }

  static key(midi, layer) { return midi + "/" + layer; }

  /** Nominal MIDI velocity at the centre of a Salamander layer (1..16). */
  static layerCenter(layer) { return (layer - 0.5) * 8; }

  static releaseSeconds(note) { return 0.08 + 0.32 * (1 - (note - PIANO_NOTE_MIN) / (PIANO_NOTE_MAX - PIANO_NOTE_MIN)); }

  static panForNote(note) { return pianoClamp(((note - 64) / 44) * 0.6, -0.6, 0.6); }

  /** Time (s) of the first audible sample, so playback starts right at the attack. */
  static detectOnset(buffer) {
    const d = buffer.getChannelData(0);
    let peak = 0;
    for (let i = 0; i < d.length; i++) { const a = Math.abs(d[i]); if (a > peak) peak = a; }
    const thr = peak * 0.02;
    for (let i = 0; i < d.length; i++) {
      if (Math.abs(d[i]) > thr) return Math.max(0, i - Math.round(buffer.sampleRate * 0.001)) / buffer.sampleRate;
    }
    return 0;
  }

  /** Same decoded buffers, different context (used by the tests to render offline). */
  clone(ctx, destination) {
    return new SampledPiano(ctx, destination, {
      buffers: this.buffers, layers: this.layers, sampleNotes: this.sampleNotes, ready: this.ready,
    });
  }

  async load(bank, onProgress = () => {}, { concurrency = 4 } = {}) {
    this.bank = bank;
    this.layers = bank.layers.slice().sort((a, b) => a - b);
    this.sampleNotes = bank.notes.slice();
    const jobs = [];
    for (let i = 0; i < bank.notes.length; i++) {
      for (const layer of bank.layers) jobs.push({ midi: bank.notes[i], name: bank.noteNames[i], layer });
    }
    // Middle of the keyboard first so it becomes playable within a second or so.
    jobs.sort((a, b) => Math.abs(a.midi - 60) - Math.abs(b.midi - 60) || Math.abs(a.layer - 12) - Math.abs(b.layer - 12));
    const total = jobs.length;
    let done = 0;
    const errors = [];
    const worker = async () => {
      for (;;) {
        const job = jobs.shift();
        if (!job) return;
        try {
          const b64 = bank.samples[job.name + "v" + job.layer];
          if (!b64) throw new Error("missing sample " + job.name + "v" + job.layer);
          const bytes = pianoBase64ToBytes(b64);
          const buffer = await this.ctx.decodeAudioData(bytes.buffer);
          this.buffers.set(SampledPiano.key(job.midi, job.layer), { buffer, offset: SampledPiano.detectOnset(buffer) });
        } catch (err) {
          errors.push({ sample: job.name + "v" + job.layer, error: String(err && err.message || err) });
        }
        done++;
        this.progress = done / total;
        onProgress(this.progress, done, total);
      }
    };
    await Promise.all(Array.from({ length: concurrency }, worker));
    this.loadErrors = errors;
    this.ready = this.buffers.size > 0;
    return this.ready;
  }

  _sampleIndex(note) {
    if (!this.sampleNotes.length) return -1;
    let best = 0;
    for (let i = 1; i < this.sampleNotes.length; i++) {
      if (Math.abs(this.sampleNotes[i] - note) < Math.abs(this.sampleNotes[best] - note)) best = i;
    }
    return best;
  }

  canPlay(note) {
    const i = this._sampleIndex(note);
    if (i < 0) return false;
    const midi = this.sampleNotes[i];
    return this.layers.some((l) => this.buffers.has(SampledPiano.key(midi, l)));
  }

  /**
   * Decide which sample(s) play for a note at a velocity.
   * Returns { sampleMidi, layers: [{ layer, gain }], gain, cutoff } or null.
   */
  plan(note, velocity) {
    const i = this._sampleIndex(note);
    if (i < 0) return null;
    const sampleMidi = this.sampleNotes[i];
    const decoded = this.layers.filter((l) => this.buffers.has(SampledPiano.key(sampleMidi, l)));
    if (!decoded.length) return null;
    const centers = decoded.map(SampledPiano.layerCenter);
    const f0 = pianoMidiToFreq(note);
    let layers;
    let gain = Math.pow(velocity / 127, 0.5);   // mild tilt on top of the layers' own natural levels
    let cutoff = 20000;
    if (velocity <= centers[0]) {
      // Below the softest layer: scale it down and darken it.
      const t = velocity / centers[0];
      layers = [{ layer: decoded[0], gain: 1 }];
      gain *= Math.pow(t, 1.6);
      cutoff = Math.max(f0 * 1.8, 900 * Math.pow(16000 / 900, t));
    } else if (velocity >= centers[centers.length - 1]) {
      layers = [{ layer: decoded[decoded.length - 1], gain: 1 }];
    } else {
      let k = 0;
      while (velocity > centers[k + 1]) k++;
      const t = (velocity - centers[k]) / (centers[k + 1] - centers[k]);
      layers = [
        { layer: decoded[k], gain: Math.cos((t * Math.PI) / 2) },
        { layer: decoded[k + 1], gain: Math.sin((t * Math.PI) / 2) },
      ];
    }
    return { sampleMidi, layers, gain, cutoff: Math.min(cutoff, 20000) };
  }

  createVoice(note, velocity, time) {
    const plan = this.plan(note, velocity);
    if (!plan) return null;
    const ctx = this.ctx;
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.Q.value = 0.5;
    filter.frequency.value = plan.cutoff;
    const env = ctx.createGain();
    const panner = ctx.createStereoPanner();
    panner.pan.value = SampledPiano.panForNote(note);
    filter.connect(env);
    env.connect(panner);
    panner.connect(this.output);

    const rate = Math.pow(2, (note - plan.sampleMidi) / 12);
    const sources = [];
    const nodes = [filter, panner];
    let naturalEnd = time;
    for (const { layer, gain } of plan.layers) {
      const s = this.buffers.get(SampledPiano.key(plan.sampleMidi, layer));
      const src = ctx.createBufferSource();
      src.buffer = s.buffer;
      src.playbackRate.value = rate;
      const g = ctx.createGain();
      g.gain.value = gain;
      src.connect(g);
      g.connect(filter);
      src.start(time, s.offset);
      naturalEnd = Math.max(naturalEnd, time + (s.buffer.duration - s.offset) / rate + 0.05);
      sources.push(src);
      nodes.push(g);
    }
    // A hair of attack: softer touches have a slightly slower hammer contact.
    const attack = 0.002 + 0.008 * (1 - velocity / 127);
    env.gain.setValueAtTime(0, time);
    env.gain.linearRampToValueAtTime(plan.gain, time + attack);
    return new Voice(this, {
      env, sources, nodes,
      releaseSeconds: SampledPiano.releaseSeconds(note),
      hasDamper: note < 89,
      naturalEnd,
    });
  }
}

/* ------------------------------------------------------------------------ */

/**
 * Additive piano-like synth: inharmonic partials with two-stage decay, detuned
 * unisons on the low partials, a velocity-dependent brightness filter and a short
 * hammer noise. Instant to start and used while samples decode, or if they fail.
 */
class SynthPiano extends Instrument {
  constructor(ctx, destination, options = {}) {
    super(ctx, destination, { maxVoices: options.maxVoices ?? 24 });
    this.output.gain.value = 0.7;
    this.ready = true;
    this.progress = 1;
    this.noise = SynthPiano.makeNoise(ctx, 0.06);
  }

  static makeNoise(ctx, seconds) {
    const b = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * seconds), ctx.sampleRate);
    const d = b.getChannelData(0);
    let seed = 99991;
    for (let i = 0; i < d.length; i++) { seed = (seed * 1664525 + 1013904223) >>> 0; d[i] = (seed / 4294967296) * 2 - 1; }
    return b;
  }

  canPlay() { return true; }

  createVoice(note, velocity, time) {
    const ctx = this.ctx;
    const f0 = pianoMidiToFreq(note);
    const vn = velocity / 127;
    const pos = (note - PIANO_NOTE_MIN) / (PIANO_NOTE_MAX - PIANO_NOTE_MIN);   // 0 = bass, 1 = treble
    const brightness = Math.pow(vn, 1.3);
    const rolloff = 2.6 - 1.5 * brightness;             // partial amplitude ~ k^-rolloff
    const inharm = 0.00012 * Math.pow(2, pos * 5);      // stiffness: partials sharpen toward the treble
    const nPartials = pianoClamp(Math.floor(12000 / f0), 2, 10);

    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.Q.value = 0.6;
    filter.frequency.value = Math.min(18000, Math.max(f0 * 2.2, 1000 * Math.pow(16, brightness)));
    const env = ctx.createGain();
    const panner = ctx.createStereoPanner();
    panner.pan.value = SampledPiano.panForNote(note);
    filter.connect(env);
    env.connect(panner);
    panner.connect(this.output);

    const sources = [];
    const nodes = [filter, panner];
    const tau1 = 4.5 * Math.pow(0.18, pos);             // aftersound of the fundamental: ~4.5 s bass, ~0.8 s treble
    let ampSum = 0;
    for (let k = 1; k <= nPartials; k++) {
      const fk = k * f0 * Math.sqrt(1 + inharm * k * k);
      if (fk > 18500) break;
      const amp = Math.pow(k, -rolloff);
      ampSum += amp;
      const tauSlow = tau1 / (1 + 0.3 * (k - 1));
      const tauFast = Math.max(0.03, tauSlow * 0.1);
      const unison = k <= 3 ? [-1, 1] : [0];
      for (const u of unison) {
        const osc = ctx.createOscillator();
        osc.type = "sine";
        osc.frequency.value = fk;
        osc.detune.value = u * (1.0 + 0.8 * pos);     // cents; slow beating like a real unison
        const g = ctx.createGain();
        const a = amp / unison.length;
        g.gain.setValueAtTime(a, time);
        g.gain.setTargetAtTime(a * 0.45, time + 0.004, tauFast);   // prompt sound
        g.gain.setTargetAtTime(0, time + tauFast * 3, tauSlow);    // aftersound
        osc.connect(g);
        g.connect(filter);
        osc.start(time);
        sources.push(osc);
        nodes.push(g);
      }
    }

    // Hammer / key noise, louder and brighter when struck hard.
    const noise = ctx.createBufferSource();
    noise.buffer = this.noise;
    const nf = ctx.createBiquadFilter();
    nf.type = "bandpass";
    nf.frequency.value = pianoClamp(f0 * 2.5, 250, 5000);
    nf.Q.value = 0.7;
    const ng = ctx.createGain();
    ng.gain.setValueAtTime(0.5 * vn * vn, time);
    ng.gain.exponentialRampToValueAtTime(0.001, time + 0.03);
    noise.connect(nf);
    nf.connect(ng);
    ng.connect(env);
    noise.start(time);
    sources.push(noise);
    nodes.push(nf, ng);

    const level = (0.55 * Math.pow(vn, 1.35)) / Math.sqrt(Math.max(1, ampSum));
    env.gain.setValueAtTime(0, time);
    env.gain.linearRampToValueAtTime(level, time + 0.003);
    return new Voice(this, {
      env, sources, nodes,
      releaseSeconds: 0.1 + 0.25 * (1 - pos),
      hasDamper: note < 89,
      naturalEnd: time + tau1 * 5 + 0.5,
    });
  }
}

/* ------------------------------------------------------------------------ */

class PianoEngine {
  constructor(options = {}) {
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = options.context || new AC({ latencyHint: "interactive" });
    this.bus = new AudioBus(this.ctx, options);
    this.sampler = new SampledPiano(this.ctx, this.bus.input);
    this.synth = new SynthPiano(this.ctx, this.bus.input);
    this.instruments = { sampled: this.sampler, synth: this.synth };
    this.preferred = "sampled";
    this.pedals = { sustain: false, sostenuto: false, soft: false };
    this._owners = new Map();     // note -> instrument currently sounding it
    this._listeners = new Map();
  }

  on(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(fn);
    return () => this.off(type, fn);
  }

  off(type, fn) { const s = this._listeners.get(type); if (s) s.delete(fn); }

  _emit(type, detail) {
    const s = this._listeners.get(type);
    if (!s) return;
    for (const fn of s) { try { fn(detail); } catch (err) { console.error(err); } }
  }

  /** Name of the instrument that would currently sound for middle C. */
  get activeInstrument() { return this.preferred === "sampled" && this.sampler.canPlay(60) ? "sampled" : "synth"; }

  instrumentFor(note) {
    if (this.preferred === "sampled" && this.sampler.canPlay(note)) return this.sampler;
    return this.synth;
  }

  noteOn(note, velocity, source = "app") {
    note = Math.round(note);
    if (!(note >= PIANO_NOTE_MIN && note <= PIANO_NOTE_MAX)) return null;
    velocity = pianoClampVelocity(velocity);
    const t = this.ctx.currentTime;
    const inst = this.instrumentFor(note);
    const prev = this._owners.get(note);
    if (prev && prev !== inst) prev.noteOff(note, t);
    this._owners.set(note, inst);
    const voice = inst.noteOn(note, velocity, t);
    this._emit("noteon", { note, velocity, source, instrument: inst === this.sampler ? "sampled" : "synth" });
    return voice;
  }

  noteOff(note, source = "app") {
    note = Math.round(note);
    const t = this.ctx.currentTime;
    const inst = this._owners.get(note);
    if (inst) inst.noteOff(note, t);
    else { this.sampler.noteOff(note, t); this.synth.noteOff(note, t); }
    this._owners.delete(note);
    this._emit("noteoff", { note, source, sustained: this.pedals.sustain || this.pedals.sostenuto });
  }

  setSustain(down) {
    down = !!down;
    if (this.pedals.sustain === down) return;
    this.pedals.sustain = down;
    const t = this.ctx.currentTime;
    this.sampler.setSustain(down, t);
    this.synth.setSustain(down, t);
    this.bus.setPedalResonance(down);
    this._emit("pedal", { ...this.pedals });
  }

  setSostenuto(down) {
    down = !!down;
    if (this.pedals.sostenuto === down) return;
    this.pedals.sostenuto = down;
    const t = this.ctx.currentTime;
    this.sampler.setSostenuto(down, t);
    this.synth.setSostenuto(down, t);
    this._emit("pedal", { ...this.pedals });
  }

  setSoft(down) {
    down = !!down;
    if (this.pedals.soft === down) return;
    this.pedals.soft = down;
    this.sampler.setSoft(down);
    this.synth.setSoft(down);
    this._emit("pedal", { ...this.pedals });
  }

  allNotesOff() {
    const t = this.ctx.currentTime;
    this.sampler.allNotesOff(t);
    this.synth.allNotesOff(t);
    this._owners.clear();
    this._emit("allnotesoff", {});
  }

  panic() {
    this.sampler.panic();
    this.synth.panic();
    this._owners.clear();
    this.setSustain(false);
    this.setSostenuto(false);
    this._emit("allnotesoff", {});
  }

  setInstrument(name) {
    if (!this.instruments[name]) throw new Error("unknown instrument " + name);
    if (name === this.preferred) return;
    this.allNotesOff();
    this.preferred = name;
    this._emit("instrument", { name });
  }

  setVolume(v) { this.bus.setVolume(v); }
  setReverb(a) { this.bus.setReverb(a); }

  async loadBank(bank, onProgress) {
    const ok = await this.sampler.load(bank, (p, done, total) => {
      if (onProgress) onProgress(p, done, total);
      this._emit("load", { progress: p, done, total });
    });
    this._emit("ready", { ok, errors: this.sampler.loadErrors });
    return ok;
  }

  async resume() {
    if (this.ctx.state !== "running") { try { await this.ctx.resume(); } catch (err) { console.warn("AudioContext resume failed", err); } }
    return this.ctx.state;
  }

  get voiceCount() { return this.sampler.voices.length + this.synth.voices.length; }
}
