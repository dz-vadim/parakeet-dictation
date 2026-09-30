#!/usr/bin/env python3
"""Exercise the reworked offline ASR path in parakeet_dictation against the real model.

Drives the real `ASREngine._run_offline` with a fake input stream and a scripted
VAD, so the real decode queue, worker thread, length floor, trailing pad and
diagnostics all run.  Checks:

  * a 0.2 s segment is rejected before it reaches the model
  * a segment decodes once the trailing pad is applied
  * emitted text keeps segment order across several queued segments
  * an overflowed read no longer throws away the audio it returned
  * the diagnostics log stays under its size cap with whole lines only
  * the recognizer is loaded once and reused by a second session

Run:  .venv/bin/python tests/test_pipeline.py
"""

import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"   # fixture audio lives with the benchmark data
sys.path.insert(0, str(HERE.parent))

from parakeet_dictation import (audio as da_audio, config as da_config,  # noqa: E402
                                diagnostics as da_diagnostics, engine as da_engine,
                                models as da_models)

SR = da_audio.SAMPLE_RATE
FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# --------------------------------------------------------------------------
# Harness: synchronous GLib, silent beeps, scripted audio + VAD
# --------------------------------------------------------------------------

class GLibShim:
    """idle_add runs inline, so callback order is the order it was queued in."""
    SOURCE_CONTINUE = True
    PRIORITY_DEFAULT = 0

    @staticmethod
    def idle_add(fn, *args):
        fn(*args)
        return 0


da_engine.GLib = GLibShim
da_engine.play_beep_start = lambda *a, **k: None
da_engine.play_beep_stop = lambda *a, **k: None
da_engine.play_beep_pause = lambda *a, **k: None

DIAG_PATH = HERE / ".test-diagnostics.log"
DIAG_PATH.unlink(missing_ok=True)
da_engine.DIAG = da_diagnostics.DiagnosticLog(DIAG_PATH, max_bytes=5 * 1024 * 1024)


class FakeStream:
    """Hands out prepared blocks, optionally flagging overflow on some reads."""

    def __init__(self, blocks, on_exhausted, overflow_every=0):
        self._blocks = blocks
        self._i = 0
        self._on_exhausted = on_exhausted
        self._overflow_every = overflow_every
        self.reads = 0
        self.samples_returned = 0
        self.overflows = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self.reads += 1
        if self._i >= len(self._blocks):
            self._on_exhausted()
            return np.zeros((n, 1), dtype=np.float32), False
        block = self._blocks[self._i]
        self._i += 1
        if self._i >= len(self._blocks):
            self._on_exhausted()  # loop breaks at the next top-of-loop check
        overflowed = bool(self._overflow_every and self.reads % self._overflow_every == 0)
        self.overflows += int(overflowed)
        self.samples_returned += block.shape[0]
        return block, overflowed


class ScriptedVad:
    """Emits a fixed list of segments so segmentation is out of the picture."""

    def __init__(self, segments, every=3):
        self._pending = list(segments)
        self._ready = []
        self._every = every
        self._calls = 0
        self.samples_seen = 0

    def accept_waveform(self, samples):
        self.samples_seen += len(samples)
        self._calls += 1
        if self._pending and self._calls % self._every == 0:
            self._ready.append(da_audio._SpeechSegment(self._pending.pop(0)))

    def is_speech_detected(self):
        return False

    def empty(self):
        return not self._ready

    @property
    def front(self):
        return self._ready[0]

    def pop(self):
        self._ready.pop(0)

    def flush(self):
        while self._pending:
            self._ready.append(da_audio._SpeechSegment(self._pending.pop(0)))


def load_wav(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR, f"{path} is not {SR} Hz"
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0)


def make_blocks(total_samples, block=1600):
    """Filler capture blocks — content is irrelevant with a scripted VAD."""
    return [np.zeros((block, 1), dtype=np.float32) for _ in range(total_samples // block)]


def new_engine(config=None):
    config = config or da_config.AppConfig()
    profile = da_models.load_model_profiles()["profiles"][config.model_profile]
    emitted = []
    errors = []
    engine = da_engine.ASREngine(
        config, profile,
        on_text=emitted.append,
        on_partial=lambda t: None,
        on_error=errors.append,
    )
    return engine, emitted, errors


def run_session(engine, blocks, vad, overflow_every=0):
    engine._stop_event.clear()
    engine._pause_event.set()
    engine._paused = False
    engine._build_vad = lambda: vad
    holder = {}

    def factory(**_kw):
        holder["stream"] = FakeStream(blocks, engine._stop_event.set, overflow_every)
        return holder["stream"]

    da_engine.sd.InputStream = factory
    engine._run_offline(engine._stop_event)
    return holder["stream"]


def diag_lines():
    return DIAG_PATH.read_text().splitlines() if DIAG_PATH.exists() else []


# --------------------------------------------------------------------------

def main():
    seg_files = sorted((BENCH / "segs").glob("*.wav"))
    if len(seg_files) < 4:
        sys.exit(f"need at least 4 wavs in {BENCH / 'segs'}")
    print(f"segments on disk: {[p.name for p in seg_files]}")

    config = da_config.AppConfig()
    print(f"profile={config.model_profile} threads={config.num_threads}")

    # ---- 1. model lifecycle: load once, reuse afterwards ------------------
    print("\n[1] recognizer cache")
    engine, emitted, errors = new_engine(config)
    t0 = time.perf_counter()
    recognizer, load_ms = engine._acquire_offline_recognizer()
    first_wall = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    _, load_ms2 = engine._acquire_offline_recognizer()
    second_wall = (time.perf_counter() - t0) * 1000
    print(f"  first  acquire: {first_wall:8.1f} ms (build {load_ms:.1f} ms + warm-up)")
    print(f"  second acquire: {second_wall:8.3f} ms (reported load {load_ms2:.1f} ms)")
    check("recognizer reused on second acquire", load_ms2 == 0.0 and second_wall < 10)
    check("second engine object shares the cached recognizer",
          new_engine(config)[0]._acquire_offline_recognizer()[0] is recognizer)

    # ---- 2. trailing pad -------------------------------------------------
    print("\n[2] trailing silence pad")
    def bare_decode(samples):
        """Decode without the trailing pad, for contrast."""
        with da_engine.INFERENCE_LOCK:
            s = recognizer.create_stream()
            s.accept_waveform(SR, np.asarray(samples, dtype=np.float32))
            recognizer.decode_stream(s)
            return s.result.text.strip()

    # Each segment is cut at the last audible sample — the case where TDT has no
    # trailing encoder frames left — then decoded with and without the pad.
    # Segments are split by level: in this recording only 00/01 are speech
    # (-15 dBFS), the rest is noise floor (-36 to -45 dBFS) where Parakeet
    # hallucinates "Yeah."/"Okay.".  The level split is analysis only; the
    # pipeline itself applies no energy gate.
    SPEECH_RMS = 0.05
    speech_ok = 0
    speech_shortened = 0
    quiet_suppressed = 0
    print(f"  {'file':9} {'dur':>6} {'rms':>8} {'decode':>8}  pad effect")
    for path in seg_files:
        audio = load_wav(path)
        loud = np.nonzero(np.abs(audio) > 0.01)[0]
        if not len(loud):
            continue
        trimmed = audio[: loud[-1] + 1]
        rms = float(np.sqrt((trimmed ** 2).mean()))
        padded_text, ms = da_engine.ASREngine._decode_segment(recognizer, trimmed)
        bare_text = bare_decode(trimmed)
        is_speech = rms >= SPEECH_RMS
        if is_speech:
            speech_ok += int(bool(padded_text))
            speech_shortened += int(len(padded_text.split()) < len(bare_text.split()))
        elif bare_text and not padded_text:
            quiet_suppressed += 1
        effect = "unchanged" if padded_text == bare_text else "CHANGED"
        print(f"  {path.name:9} {len(trimmed)/SR:5.2f}s {rms:8.4f} {ms:7.0f}ms  "
              f"{'speech' if is_speech else 'noise '} {effect}")
        if padded_text != bare_text:
            print(f"      no pad: {bare_text!r}")
            print(f"      padded: {padded_text!r}")
    check("speech segments decode with the pad", speech_ok == 2,
          f"{speech_ok} of 2 speech segments produced text")
    check("pad never shortens a speech segment", speech_shortened == 0)
    print(f"  noise-floor segments whose hallucination the pad suppressed: "
          f"{quiet_suppressed}")

    # ---- 3. length floor -------------------------------------------------
    print("\n[3] minimum duration floor")
    check("0.2 s (3200 samples) rejected", da_engine.segment_too_short(3200) is True)
    check("0.3 s (4800 samples) accepted", da_engine.segment_too_short(4800) is False)
    check("4799 samples rejected", da_engine.segment_too_short(4799) is True)

    # ---- 4. queue path: ordering, rejection, overflow --------------------
    print("\n[4] decode queue end to end")
    segments = [load_wav(p) for p in seg_files[:5]]
    short = segments[0][:3200]               # 0.2 s of real speech
    scripted = segments[:2] + [short] + segments[2:5]

    references = [da_engine.ASREngine._decode_segment(recognizer, s)[0] for s in segments[:5]]
    # Sub-second segments of this recording are noise floor and decode to "";
    # the worker only emits non-empty text, so that is what we compare against.
    expected = [t for t in references if t]
    print("  per-segment references: " + ", ".join(
        f"{p.name}={'<empty>' if not t else repr(t[:28])}"
        for p, t in zip(seg_files[:5], references)))

    before = len(diag_lines())
    engine, emitted, errors = new_engine(config)
    vad = ScriptedVad(scripted, every=3)
    blocks = make_blocks(1600 * 30)
    stream = run_session(engine, blocks, vad, overflow_every=7)
    new_lines = diag_lines()[before:]

    print(f"  reads={stream.reads} overflows_flagged={stream.overflows} "
          f"samples_returned={stream.samples_returned} vad_saw={vad.samples_seen}")
    for i, t in enumerate(emitted):
        print(f"  [{i}] {t[:64]!r}")

    check("no engine errors", not errors, str(errors))
    check("one text per accepted non-empty segment, short one dropped",
          len(emitted) == len(expected),
          f"{len(emitted)} texts, {len(expected)} expected, 6 segments submitted")
    check("text order matches segment order", emitted == expected,
          f"{len(emitted)} texts in reference order")
    check("too-short segment logged as discarded",
          any("event=segment_discarded" in l and "reason=too_short" in l for l in new_lines))
    check("overflow counted, not fatal",
          any("event=audio_overflow" in l for l in new_lines))
    check("no audio dropped on an overflowed read",
          vad.samples_seen == stream.samples_returned,
          f"{vad.samples_seen} == {stream.samples_returned}")
    check("segment durations and decode times logged",
          sum(1 for l in new_lines if "event=segment " in l) == 5)
    check("session start and stop logged",
          any("event=session_start" in l for l in new_lines)
          and any("event=session_stop" in l for l in new_lines))
    first_word = references[0].split()[0] if references[0] else ""
    check("no transcript text in the diagnostics log",
          bool(first_word) and not any(first_word in l for l in new_lines))

    # ---- 5. flush drains the queue before the session ends ---------------
    print("\n[5] flush at stop")
    engine, emitted, errors = new_engine(config)
    # `every` larger than the block count: every segment is emitted by flush()
    vad = ScriptedVad(segments[:3], every=1000)
    run_session(engine, make_blocks(1600 * 6), vad)
    check("segments queued only at flush still get decoded",
          emitted == [t for t in references[:3] if t], f"{len(emitted)} texts")

    # ---- 6. real VAD smoke pass -----------------------------------------
    print("\n[6] real TEN VAD over real_uk_norm.wav")
    full = load_wav(BENCH / "real_uk_norm.wav")
    blocks = [full[i:i + 1600].reshape(-1, 1) for i in range(0, len(full) - 1600, 1600)]
    engine, emitted, errors = new_engine(config)
    engine._stop_event.clear()
    engine._pause_event.set()
    engine._paused = False
    holder = {}

    def factory(**_kw):
        holder["s"] = FakeStream(blocks, engine._stop_event.set)
        return holder["s"]

    da_engine.sd.InputStream = factory
    t0 = time.perf_counter()
    engine._run_offline(engine._stop_event)
    elapsed = time.perf_counter() - t0
    words = sum(len(t.split()) for t in emitted)
    print(f"  {len(full)/SR:.1f}s audio -> {len(emitted)} blocks with text, "
          f"{words} words, {elapsed:.1f}s wall "
          f"({len(full)/SR/elapsed:.1f}x realtime)")
    print(f"  first block: {(emitted[0] if emitted else '')[:70]!r}")
    # Baseline for this recording, from bench/segment_and_parakeet.py: 7 VAD
    # segments, 19 words — most of the 58 s is noise floor, not speech.
    # Informational only — the assertion below does not depend on it, so a
    # missing fixture must not take the whole suite down with it.
    # Block count, not segment count: at the default coalesce_target_s the VAD
    # segments of this recording are merged before decoding (coalescing itself
    # is covered in tests/test_hold_take.py), so what this pass still
    # guarantees is that the real VAD path transcribes the speech in it.
    baseline_path = BENCH / "out-parakeet.txt"
    baseline = baseline_path.read_text() if baseline_path.exists() else ""
    joined = " ".join(emitted)
    print(f"  baseline (out-parakeet.txt): {len(baseline.split())} words")
    check("real VAD path still transcribes", len(emitted) >= 1 and words >= 15,
          f"{len(emitted)} blocks, {words} words vs {len(baseline.split())} baseline")
    # Case-insensitive: which words the model capitalises shifts with the exact
    # audio it is handed, and this check is about the phrases being there.
    check("both spoken phrases recovered",
          "open the settings" in joined.lower() and "посередині думки" in joined,
          joined[:90])
    check("no engine errors on the real pass", not errors, str(errors))

    # ---- 7. diagnostics size cap ----------------------------------------
    print("\n[7] diagnostics size cap")
    cap_path = HERE / ".test-diag-cap.log"
    cap_path.unlink(missing_ok=True)
    small = da_diagnostics.DiagnosticLog(cap_path, max_bytes=4096)
    for i in range(400):
        small.log("filler", i=i, pad="x" * 40)
    size = cap_path.stat().st_size
    lines = cap_path.read_text().splitlines()
    check("log stays under the cap", size <= 4096, f"{size} bytes")
    check("only whole lines kept", all(l.startswith("ts=") for l in lines),
          f"{len(lines)} lines, first: {lines[0][:40]!r}")
    check("newest lines kept", "i=399" in lines[-1], lines[-1][-30:])
    cap_path.unlink(missing_ok=True)

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
