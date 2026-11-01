"""Telling the people on a call apart, desktop edition (scratch folder, mock analyzer, no audio devices).

Usage:  desktop\\.venv\\Scripts\\python desktop\\tests\\speakers_test.py
Part 1 uses a stand-in for the voice model. Part 2 uses the real model with two real Windows voices (David and Zira)
and needs desktop\\models\\wespeaker_en_voxceleb_resnet34_LM.onnx (downloaded once with ALLOW_MODEL_DOWNLOAD=1).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="dbf-spk-"))
os.environ.update(SESSIONS_DIR=str(SCRATCH / "sessions"), RECORDINGS_DIR=str(SCRATCH / "rec"), MOCK_ANALYZER="1", HF_HUB_OFFLINE="1")
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT.parent / "shared" / "core"))

import analyzer  # noqa: E402
import config  # noqa: E402
import session as S  # noqa: E402
import speakers  # noqa: E402

PROFILE = config.load_profiles()["meeting"]


def noise(std, seconds=2.0, seed=0):
    return (np.random.default_rng(seed).standard_normal(int(16000 * seconds)) * std).astype(np.float32)


def fake(audio):
    rms = float(np.sqrt(np.mean(np.square(audio))))
    return np.eye(8, dtype=np.float32)[1 if rms < 0.15 else 2]


def say(s, who, text, audio, ts):
    sp = s._speaker_of(who, audio)
    return s.on_utterance(who, text, ts, speaker=sp), sp


def part1():
    speakers.set_embedder(fake)
    s = S.Session(PROFILE, "Spk test", speakers=True)
    S.SESSIONS[s.sid] = s
    ids = [say(s, "other", f"words alpha{i} beta{i}", noise(std), i)[1] for i, std in enumerate((0.09, 0.27, 0.09, 0.27))]
    assert ids == [1, 2, 1, 2], ids
    assert say(s, "you", "my own line", noise(0.09), 9)[1] is None, "the microphone is always you"
    assert s.speaker_info() == {"names": {}, "multi": True}
    d = s.detail_block()
    assert d["speakers"] == {"on": True, "names": {}}
    s.set_speaker_name(2, "Priya")
    assert s.detail_block()["speakers"]["names"] == {"2": "Priya"}
    ctx = analyzer.build_context(s.ctx())
    assert "SEVERAL PEOPLE ARE ON THE OTHERS SIDE" in ctx.upper() or "SEVERAL PEOPLE" in ctx, ctx[:300]
    assert "(Priya)" in ctx and "(Speaker 1)" in ctx
    s.save()
    text = (s.dir / "transcript.txt").read_text(encoding="utf-8")
    assert "(Priya)" in text and "(Speaker 1)" in text
    again = S.Session.load(s.dir, {PROFILE["id"]: PROFILE})
    assert again.speakers_on and again.speaker_names == {"2": "Priya"} and [ln["speaker"] for ln in again.lines if ln["label"] == "other"] == [1, 2, 1, 2]
    off = S.Session(PROFILE, "No speakers", speakers=False)
    assert say(off, "other", "x y z", noise(0.09), 1)[1] is None
    speakers.set_embedder(None)
    print("part 1 ok: tags, naming, AI context, saved transcript, reload")


def speak(voice, text, out):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{out}', $f); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    with wave.open(str(out)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def part2():
    if not config.SPEAKER_MODEL.exists():
        print("part 2 skipped: speaker model not downloaded")
        return
    sents = ["Thanks for joining today and welcome to the team.", "Could you walk me through how a hash map handles collisions?",
             "I would use the saga pattern with compensating actions for failures.", "Yes I led a team of five engineers on that migration."]
    s = S.Session(PROFILE, "Real voices", speakers=True)
    got, said = [], []
    for i, text in enumerate(sents):
        for voice in ("Microsoft David Desktop", "Microsoft Zira Desktop"):
            audio = speak(voice, text, SCRATCH / f"{voice.split()[1]}{i}.wav")
            said.append(voice.split()[1])
            got.append(s._speaker_of("other", audio))
    assert s.speakers_on, "the speaker model should have loaded"
    by_voice = {}
    for v, g in zip(said, got):
        by_voice.setdefault(v, set()).add(g)
    assert all(len(x) == 1 for x in by_voice.values()) and len({next(iter(x)) for x in by_voice.values()}) == 2, (said, got)
    print("part 2 ok: real model told David and Zira apart:", dict(zip(said, got)) if len(said) < 4 else list(zip(said, got)))


try:
    part1()
    part2()
    print("DESKTOP SPEAKERS OK")
finally:
    shutil.rmtree(SCRATCH, ignore_errors=True)
