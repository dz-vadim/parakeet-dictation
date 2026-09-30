#!/usr/bin/env python3
"""Regression checks for the 2026-09-30 code review.

One section per finding of docs/reviews/2026-09-30-code-review.md (numbering
kept), each added in the commit that fixed it and written to fail on the
commit before.  No model is loaded and nothing touches the real clipboard,
config, D-Bus names or diagnostics log.

Run:  .venv/bin/python tests/test_review.py            # every section
      .venv/bin/python tests/test_review.py 1 7        # sections 1 and 7 only
"""

import io
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from parakeet_dictation import (audio as da_audio, config as da_config,  # noqa: E402
                                diagnostics as da_diagnostics)

SR = da_audio.SAMPLE_RATE
HOP = 256                      # TenVadDetector's hop: one VAD decision per 16 ms
FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


DIAG_PATH = HERE / ".test-review.log"
DIAG_PATH.unlink(missing_ok=True)
TEST_DIAG = da_diagnostics.DiagnosticLog(DIAG_PATH)


def diag_lines():
    return DIAG_PATH.read_text().splitlines() if DIAG_PATH.exists() else []


# ---------------------------------------------------------------------------
# #1  TenVadDetector: the min-speech floor must count SPEECH, not the buffer.
#     The buffer always holds the 0.8 s closing silence, so a one-hop VAD blip
#     used to become a ~0.8 s near-silent segment (and, +20 dB later, words).
# ---------------------------------------------------------------------------

class ScriptedTenVad:
    """Stands in for ten_vad.TenVad: one scripted probability per hop."""

    def __init__(self, probs):
        self._probs = list(probs)
        self.calls = 0

    def process(self, _chunk):
        p = self._probs[self.calls] if self.calls < len(self._probs) else 0.0
        self.calls += 1
        return p, int(p >= 0.5)


def scripted_detector(script, min_silence=0.8, min_speech=0.25):
    """A real TenVadDetector whose VAD decisions come from `script`, fed a
    take whose sample values encode their own position (so contiguity of what
    comes out can be checked against what went in)."""
    hops = sum(n for n, _speech in script)
    probs = []
    for n, speech in script:
        probs += [0.9 if speech else 0.0] * n
    vad = da_audio.TenVadDetector(threshold=0.5, min_silence_duration=min_silence,
                                  min_speech_duration=min_speech)
    vad._vad = ScriptedTenVad(probs)
    take = (np.arange(hops * HOP, dtype=np.float32) + 1.0) / (hops * HOP * 4.0)
    for i in range(0, len(take), 1600):
        vad.accept_waveform(take[i:i + 1600].tolist())
    out = []
    while not vad.empty():
        out.append(vad.front)
        vad.pop()
    return out, take, vad


def review1_vad_speech_floor():
    print("\n[#1] the VAD's min-speech floor counts speech, not buffered silence")
    silence_hops = int(0.8 * SR) // HOP + 10       # closes the segment, plus slack

    segs, _take, _vad = scripted_detector([(1, True), (silence_hops, False)])
    check("a one-hop blip (16 ms) followed by the closing silence emits NOTHING",
          segs == [], f"{len(segs)} segment(s)"
          + (f", {len(segs[0].samples) / SR:.2f} s long" if segs else ""))

    segs, _take, _vad = scripted_detector([(32, True), (silence_hops, False)])
    check("a real 0.5 s phrase followed by silence still emits one segment",
          len(segs) == 1, f"{len(segs)} segment(s)")
    check("and that segment holds the phrase plus its closing silence",
          bool(segs) and len(segs[0].samples) == (32 + int(0.8 * SR) // HOP) * HOP,
          f"{len(segs[0].samples) if segs else 0} samples")

    segs, _take, _vad = scripted_detector([(15, True), (silence_hops, False)])
    check("a 0.24 s burst (just under the 0.25 s floor) is dropped",
          segs == [], f"{len(segs)} segment(s)")
    segs, _take, _vad = scripted_detector([(16, True), (silence_hops, False)])
    check("a 0.256 s burst (just over it) is kept", len(segs) == 1, f"{len(segs)} segment(s)")

    # A dropped blip in the middle of a pause must not break the pause: the
    # coalescer rebuilds the contiguous span across it from `lead`.
    script = [(32, True), (silence_hops, False), (1, True), (silence_hops, False),
              (32, True), (silence_hops, False)]
    segs, take, _vad = scripted_detector(script)
    check("phrase, blip, phrase -> two segments, the blip is not one of them",
          len(segs) == 2, f"{len(segs)} segment(s)")
    if len(segs) == 2:
        first_end = 32 * HOP + int(0.8 * SR) // HOP * HOP
        second_start = (32 + silence_hops + 1 + silence_hops) * HOP
        lead = segs[1].lead
        check("the second segment's lead is the WHOLE pause, blip included",
              lead is not None and len(lead) == second_start - first_end,
              f"{len(lead) if lead is not None else None} vs {second_start - first_end} samples")
        check("and it is the contiguous audio between the two phrases",
              lead is not None and np.array_equal(lead, take[first_end:second_start]))

    # The end-of-take flush is untouched: whatever is buffered is the words
    # the user was still saying, and only the recognizer's floor may drop it.
    vad = da_audio.TenVadDetector(min_silence_duration=0.8)
    vad._vad = ScriptedTenVad([0.9] * 3)
    vad.accept_waveform(np.full(3 * HOP, 0.1, dtype=np.float32).tolist())
    check("flush() still emits a phrase shorter than the floor",
          vad.flush() is True and not vad.empty() and len(vad.front.samples) == 3 * HOP)


# ---------------------------------------------------------------------------

SECTIONS = {1: review1_vad_speech_floor}


def main(argv):
    wanted = [int(a) for a in argv] if argv else sorted(SECTIONS)
    for n in wanted:
        section = SECTIONS.get(n)
        if section is None:
            check(f"section {n} exists", False)
            continue
        try:
            section()
        except Exception as e:      # a crashed section is a failed section
            check(f"{section.__name__} ran without raising", False,
                  f"{type(e).__name__}: {e}")
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
