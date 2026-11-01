// Runs Whisper and the speaker model in the visitor's browser (WebGPU if available, else WASM). Audio never leaves the page.
import { pipeline, env, AutoProcessor, AutoModelForXVector } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2/dist/transformers.min.js";
env.allowLocalModels = false;
const SPEAKER_MODEL = "Xenova/wavlm-base-plus-sv";          // about 400 MB, downloaded once and cached, only when speakers are told apart
let asr = null, proc = null, xvec = null, queue = Promise.resolve();

async function load(model) {
  const gpu = !!self.navigator.gpu;
  const progress_callback = (p) => { if (p.status === "progress") self.postMessage({ type: "progress", file: p.file, pct: p.progress }); };
  try {
    asr = await pipeline("automatic-speech-recognition", model, gpu
      ? { device: "webgpu", dtype: { encoder_model: "fp32", decoder_model_merged: "q4" }, progress_callback }
      : { dtype: "q8", progress_callback });
    self.postMessage({ type: "ready", device: gpu ? "WebGPU" : "WASM" });
  } catch (e) {
    if (!gpu) throw e;                                       // WebGPU can fail on some drivers: fall back to WASM
    asr = await pipeline("automatic-speech-recognition", model, { dtype: "q8", progress_callback });
    self.postMessage({ type: "ready", device: "WASM (WebGPU failed)" });
  }
}
async function loadSpeaker() {
  if (xvec) return;
  proc = await AutoProcessor.from_pretrained(SPEAKER_MODEL);
  xvec = await AutoModelForXVector.from_pretrained(SPEAKER_MODEL, { dtype: "fp32" });          // the 8-bit version does not tell voices apart (all scores 0.9+), the full one does
}

// speech jobs and speaker jobs have their own queues, so a long speaker-model download never holds up the transcript
let squeue = Promise.resolve();
self.onmessage = (e) => {
  const m = e.data, speaker = m.type === "embed" || m.type === "warm";
  const run = async () => {
    try {
      if (m.type === "load") await load(m.model);
      else if (m.type === "transcribe") {
        const out = await asr(m.audio, { chunk_length_s: 30, return_timestamps: false, ...(m.fast ? { num_beams: 1 } : {}) });
        self.postMessage({ type: "text", id: m.id, text: (out.text || "").trim() });
      } else if (m.type === "warm") { await loadSpeaker(); self.postMessage({ type: "embedding", id: m.id, data: new Float32Array(0) }); }
      else if (m.type === "embed") {
        await loadSpeaker();
        const { embeddings } = await xvec(await proc(m.audio));
        self.postMessage({ type: "embedding", id: m.id, data: Float32Array.from(embeddings.data) });
      }
    } catch (err) {
      self.postMessage({ type: "error", id: m.id, message: String((err && err.message) || err) });
    }
  };
  if (speaker) squeue = squeue.then(run); else queue = queue.then(run);
};
