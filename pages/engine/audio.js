// Audio in the browser edition: the speech segmenter (same algorithm as the server's), the Whisper / speaker-model worker,
// telling voices apart, and the engine that connects the microphone and the shared tab's sound to a session.
// Everything here runs in the visitor's own browser: audio never leaves the page.

export const SR = 16000, BLOCK = 4000, PARTIAL_EVERY = 1.6;           // 4000 samples = 0.25 s
export const HALLUCINATIONS = new Set(["thank you.", "thanks for watching.", "you", "bye.", "thank you for watching.", ".", ""]);

/** Adaptive speech gate + utterance segmentation for one audio source (port of the server's Segmenter). */
export class Segmenter {
  constructor(on_utterance, on_partial = null) {
    this.on_utterance = on_utterance; this.on_partial = on_partial; this.last_partial = 0;
    this.floor = 0.002; this.acc = []; this.accN = 0; this.buf = []; this.silent = 0; this.start_t = 0; this.level = 0;
  }
  push(samples, now) {
    this.acc.push(samples); this.accN += samples.length;
    while (this.accN >= BLOCK) this._block(this._take(), now);
  }
  _take() {
    const out = new Float32Array(BLOCK); let o = 0;
    while (o < BLOCK) {
      const c = this.acc[0], n = Math.min(c.length, BLOCK - o);
      out.set(c.subarray(0, n), o); o += n;
      if (n === c.length) this.acc.shift(); else this.acc[0] = c.subarray(n);
    }
    this.accN -= BLOCK;
    return out;
  }
  _block(b, now) {
    let s = 0; for (let i = 0; i < b.length; i++) s += b[i] * b[i];
    const rms = Math.sqrt(s / b.length); this.level = rms;
    if (rms < this.floor * 2.5) this.floor = 0.97 * this.floor + 0.03 * rms; else this.floor = Math.min(this.floor * 1.0008, 0.02);
    const speech = rms > Math.max(this.floor * 3.0, 0.006);
    if (speech || this.buf.length) {
      if (!this.buf.length) { this.start_t = now; this.silent = 0; }
      this.buf.push(b);
      if (this.on_partial && speech && this.buf.length >= 4 && now - this.last_partial >= PARTIAL_EVERY) {
        this.last_partial = now; this.on_partial(this._join(this.buf.slice(-48)));       // the last 12 s
      }
      this.silent = speech ? 0 : this.silent + 1;
      if (this.silent >= 3 || this.buf.length >= 100) this.flush();                        // 0.75 s of silence, or 25 s max
    }
  }
  _join(blocks) { const a = new Float32Array(blocks.length * BLOCK); blocks.forEach((b, i) => a.set(b, i * BLOCK)); return a; }
  reset() { this.buf = []; this.acc = []; this.accN = 0; this.silent = 0; }
  flush() {
    if (!this.buf.length) return;
    const audio = this._join(this.buf), start = this.start_t;
    this.buf = [];
    if (audio.length / SR - 0.75 >= 0.4) this.on_utterance(audio, start);                  // drop clicks
  }
}

/** The voices heard so far on one channel of one session (port of shared/core/speakers.py). */
export class SpeakerBook {
  constructor(embed, { threshold = 0.86, max_speakers = 8, min_new_seconds = 1.2, min_seconds = 0.7 } = {}) {
    this.embed = embed; this.threshold = threshold; this.max_speakers = max_speakers; this.min_new = min_new_seconds; this.min_seconds = min_seconds;
    this.centroids = []; this.counts = []; this.last = 0;
  }
  async assign(audio) {
    const seconds = audio.length / SR;
    if (seconds < this.min_seconds && this.last) return this.last;                        // too short to judge: most likely the same person
    const e = await this.embed(audio);
    if (!this.centroids.length) return this._new(e);
    const sims = this.centroids.map((c) => dot(e, c));
    let best = 0; sims.forEach((s, i) => { if (s > sims[best]) best = i; });
    if (sims[best] >= this.threshold) { this._update(best, e, seconds); this.last = best + 1; }
    else if (seconds >= this.min_new && this.centroids.length < this.max_speakers) return this._new(e);
    else this.last = best + 1;                                                            // unsure and short, or too many people: the closest voice
    return this.last;
  }
  _new(e) { this.centroids.push(e); this.counts.push(1); this.last = this.centroids.length; return this.last; }
  _update(i, e, seconds) {
    if (seconds < 1.0) return;                                                            // short clips do not move a voice's centre
    const n = Math.min(this.counts[i], 20), c = this.centroids[i].map((x, k) => x * n + e[k]);
    this.centroids[i] = unit(c); this.counts[i] += 1;
  }
}
const dot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };
export const unit = (a) => { let n = 0; for (const x of a) n += x * x; n = Math.sqrt(n) || 1; return Float32Array.from(a, (x) => x / n); };

/** Whisper and the speaker model run in a worker so the page stays responsive. */
export class AsrWorker {
  constructor(url) {
    this.w = new Worker(url, { type: "module" }); this.pending = new Map(); this.next = 1;
    this.status = "idle"; this.error = ""; this.device = ""; this.progress = 0; this.on_change = () => {};
    this.w.onmessage = (e) => this._msg(e.data);
  }
  _msg(m) {
    if (m.type === "progress") { this.progress = m.pct || 0; this.on_change(); }
    else if (m.type === "ready") { this.status = "ready"; this.device = m.device; this.error = ""; this.on_change(); this._settle(0, m); }
    else if (m.type === "text" || m.type === "embedding") this._settle(m.id, m);
    else if (m.type === "error") {
      if (m.id) this._settle(m.id, m, true);
      else { this.error = m.message; this.status = "error"; this.on_change(); this._settle(0, m, true); }
    }
  }
  _settle(id, m, bad = false) { const p = this.pending.get(id); if (!p) return; this.pending.delete(id); bad ? p.rej(new Error(m.message)) : p.res(m); }
  _ask(msg, transfer) { return new Promise((res, rej) => { const id = msg.id ?? this.next++; this.pending.set(id, { res, rej }); this.w.postMessage({ ...msg, id }, transfer || []); }); }
  load(model) { this.status = "loading"; this.on_change(); return this._ask({ type: "load", model, id: 0 }); }
  async transcribe(audio, fast = false) { return (await this._ask({ type: "transcribe", audio, fast })).text; }
  warm() { return this._ask({ type: "warm" }).catch(() => {}); }          // start downloading the speaker model in the background
  async embed(audio) { return unit((await this._ask({ type: "embed", audio })).data); }
}

/** Connects the microphone (you) and the sound of a shared tab (the other side) to a session. */
export class AudioEngine {
  constructor(asr, { now = () => Date.now() / 1000, partials = true } = {}) {
    this.asr = asr; this.now = now; this.partials = partials; this.ctx = null; this.mix = null; this.streams = []; this.names = []; this.session = null;
    this.segs = { you: null, other: null }; this.inflight = new Set(); this.timer = null;
  }
  segmenter(role) {
    const label = role, s = this.session;
    return new Segmenter(
      (audio, start_t) => this._utterance(label, audio, start_t),
      this.partials ? (audio) => this._partial(label, audio) : null);
  }
  async _utterance(label, audio, start_t) {
    const s = this.session;
    if (!s || s.state !== "live" || this.asr.status !== "ready") return;
    try {
      const text = await this.asr.transcribe(audio);
      s.on_partial(label, label, null);
      if (!text || HALLUCINATIONS.has(text.toLowerCase())) return;
      const speaker = await s._speaker_of(label, audio);
      s.on_utterance(label, text, Math.max(0, start_t - s.started_wall), speaker);
    } catch (e) { s.event(`Transcription error: ${e.message}`); }
  }
  async _partial(label, audio) {
    const s = this.session;
    if (!s || this.inflight.has(label) || this.asr.pending.size > 3 || this.asr.status !== "ready") return;
    this.inflight.add(label);
    try {
      const text = await this.asr.transcribe(audio, true);
      if (text && !HALLUCINATIONS.has(text.toLowerCase()) && this.segs[label] && this.segs[label].buf.length) s.on_partial(label, label, text);
    } catch (e) { /* a missed draft does not matter */ } finally { this.inflight.delete(label); }
  }
  /** Feed audio into a channel directly (used by the microphone taps, and by tests). */
  feed(role, samples) {
    const s = this.session;
    if (!s || !this.segs[role]) return;
    if (s.state !== "live" || s.paused || !s.listening) this.segs[role].reset();
    else this.segs[role].push(samples, this.now());
  }
  async start(session, { mic = true } = {}) {
    this.session = session; this.segs = { you: this.segmenter("you"), other: this.segmenter("other") };
    if (!this.ctx) {
      this.ctx = new AudioContext({ sampleRate: SR });
      const worklet = "class Tap extends AudioWorkletProcessor{process(i){const c=i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor('tap',Tap);";
      await this.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([worklet], { type: "text/javascript" })));
      this.mix = this.ctx.createMediaStreamDestination();
    }
    session.audio_clients = 1;
    if (mic) this.attach(await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }), "you", "Microphone (browser)");
    this.timer = setInterval(() => { session.levels = { you: +(this.segs.you.level || 0).toFixed(4), other: +(this.segs.other.level || 0).toFixed(4) }; }, 250);
  }
  attach(stream, role, name) {
    this.streams.push(stream); this.names.push(name);
    this.session.sources = [...this.session.sources.filter((x) => !(x.role === role)), { role, name }];
    const node = new AudioWorkletNode(this.ctx, "tap");
    node.port.onmessage = (e) => this.feed(role, e.data);
    const src = this.ctx.createMediaStreamSource(stream);
    src.connect(node); src.connect(this.mix);
    this.session.touch(false);
  }
  has_other() { return this.session && this.session.sources.some((x) => x.role === "other"); }
  async add_other() {
    const d = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    d.getVideoTracks().forEach((t) => t.stop());                                         // only the sound is wanted
    if (!d.getAudioTracks().length) throw new Error("No sound was shared. In the picker choose a tab (or Entire Screen) and tick “Share audio”.");
    this.attach(new MediaStream(d.getAudioTracks()), "other", "Shared tab / system audio");
  }
  stop() {
    for (const k of ["you", "other"]) if (this.segs[k]) this.segs[k].flush();
    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop())); this.streams = []; this.names = [];
    clearInterval(this.timer);
    if (this.session) { this.session.audio_clients = 0; this.session.sources = []; this.session.levels = { you: 0, other: 0 }; }
    if (this.ctx) { const c = this.ctx; this.ctx = null; setTimeout(() => c.close().catch(() => {}), 1500); }
    this.session = null;
  }
}
