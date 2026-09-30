#!/usr/bin/env python3
"""Regression checks for the known defects of the package split.

One section per defect from docs/superpowers/specs/2026-09-30-package-split-design.md
§ "Known defects" (numbering kept), each added in the commit that fixed it and
written to fail on the commit before.  No model is loaded and nothing touches
the real clipboard, config or diagnostics log.

Run:  .venv/bin/python tests/test_defects.py
"""

import io
import json
import os
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
                                diagnostics as da_diagnostics, engine as da_engine)

SR = da_audio.SAMPLE_RATE
FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


DIAG_PATH = HERE / ".test-defects.log"
DIAG_PATH.unlink(missing_ok=True)
TEST_DIAG = da_diagnostics.DiagnosticLog(DIAG_PATH)


def diag_lines():
    return DIAG_PATH.read_text().splitlines() if DIAG_PATH.exists() else []


for _mod in (da_engine,):
    _mod.DIAG = TEST_DIAG


# --- engine harness: no model, no microphone, no main loop ------------------

class GLibInline:
    """idle_add runs the callback at once, on the calling thread."""
    SOURCE_CONTINUE = True
    PRIORITY_DEFAULT = 0

    @staticmethod
    def idle_add(fn, *args):
        fn(*args)
        return 0


da_engine.GLib = GLibInline
da_engine.play_beep_start = lambda *a, **k: None
da_engine.play_beep_stop = lambda *a, **k: None
da_engine.play_beep_pause = lambda *a, **k: None
da_engine.resolve_audio_device = lambda _value: None

STREAMS = []       # every input stream the engine opened, in order


class LiveStream:
    """Silence at real-time pace, until the engine closes it."""

    def __init__(self):
        self.t_open = time.monotonic()
        self.t_close = None
        STREAMS.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.t_close = time.monotonic()
        return False

    def read(self, n):
        time.sleep(n / SR)
        return np.zeros((n, 1), dtype=np.float32), False


class ScriptedVad:
    """Emits the given segments, one every `every` blocks; never speech."""

    def __init__(self, segments, every=3):
        self._pending = list(segments)
        self._ready = []
        self._every = every
        self._calls = 0

    def accept_waveform(self, samples):
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
        return False

    def open_tail(self, _max):
        return np.zeros(0, dtype=np.float32)


DECODE = {"seconds": 0.0, "started": threading.Event(), "count": 0}


def slow_decode(_recognizer, samples, normalize=True, target_dbfs=-18.0):
    """Stands in for ASREngine._decode_segment: takes DECODE['seconds']."""
    DECODE["count"] += 1
    DECODE["started"].set()
    time.sleep(DECODE["seconds"])
    return "decoded text", DECODE["seconds"] * 1000


def make_engine(segments, texts, every=3):
    """An ASREngine whose model, VAD, decoder and microphone are all fakes."""
    config = da_config.AppConfig(coalesce_target_s=0.0, preview=False,
                                 preview_interval_s=0.0)
    engine = da_engine.ASREngine(
        config, {"streaming": False, "files": {}},
        on_text=lambda t: texts.append((time.monotonic(), t)),
        on_partial=lambda t: None,
        on_error=lambda e: texts.append((time.monotonic(), f"ERROR {e}")),
    )
    engine._ensure_models = lambda: None
    engine._acquire_offline_recognizer = lambda: (object(), 0.0)
    engine._build_vad = lambda: ScriptedVad(segments, every=every)
    return engine


da_engine.ASREngine._decode_segment = staticmethod(slow_decode)
da_engine.sd.InputStream = lambda **_kw: LiveStream()


# ---------------------------------------------------------------------------
# #2  AppConfig.load(): a broken file must be reported and backed up, and one
#     bad key must not take the other keys down with it.
# ---------------------------------------------------------------------------

def defect2_config_load():
    print("\n[#2] AppConfig.load() on a malformed file and on one bad key")
    defaults = da_config.AppConfig()
    with tempfile.TemporaryDirectory(prefix="parakeet-cfg-") as tmp:
        cfg_dir = Path(tmp)
        cfg_file = cfg_dir / "config.json"
        saved = (da_config.CONFIG_DIR, da_config.CONFIG_FILE)
        da_config.CONFIG_DIR, da_config.CONFIG_FILE = cfg_dir, cfg_file
        try:
            # --- a JSON typo: a trailing comma -----------------------------
            broken = '{\n  "hotkey_hold": "Meta+Z",\n  "num_threads": 4,\n}\n'
            cfg_file.write_text(broken)
            events = []
            try:
                cfg = da_config.AppConfig.load(log=lambda ev, **f: events.append((ev, f)))
            except TypeError as e:          # no `log=` before the fix
                events.append(("raised", {"err": repr(e)}))
                cfg = da_config.AppConfig.load()
            check("defaults are used when the file cannot be parsed",
                  cfg.hotkey_hold == defaults.hotkey_hold
                  and cfg.num_threads == defaults.num_threads)
            errors = [f for ev, f in events if ev == "config_error"]
            check("a config_error event is reported for the parse failure",
                  bool(errors) and errors[0].get("reason") == "unparseable", str(events))
            backups = sorted(cfg_dir.glob("config.json.broken-*"))
            check("the broken file is backed up as config.json.broken-<timestamp>",
                  len(backups) == 1, str([p.name for p in backups]))
            check("the backup holds the broken text byte for byte",
                  bool(backups) and backups[0].read_text() == broken)
            check("the event names the backup file",
                  bool(errors) and bool(backups)
                  and errors[0].get("backup") == backups[0].name, str(errors))
            check("the unparseable file is moved aside, so the next save cannot "
                  "overwrite it", not cfg_file.exists())
            check("a second load makes no second backup",
                  da_config.AppConfig.load(log=lambda *a, **k: None) is not None
                  and len(list(cfg_dir.glob("config.json.broken-*"))) == 1)

            # --- one key of the wrong type -----------------------------------
            cfg_file.write_text(json.dumps({
                "hotkey_hold": "Meta+Z",
                "num_threads": "eight",        # str where an int belongs
                "vad_threshold": 1,            # int where a float belongs: fine
                "insert_mode": "end_of_take",
                "unknown_future_key": True,    # ignored, as before
            }))
            events.clear()
            err = io.StringIO()
            with redirect_stderr(err):
                cfg = da_config.AppConfig.load(log=lambda ev, **f: events.append((ev, f)))
            check("the good keys survive a bad neighbour",
                  cfg.hotkey_hold == "Meta+Z" and cfg.insert_mode == "end_of_take",
                  f"hotkey_hold={cfg.hotkey_hold!r} insert_mode={cfg.insert_mode!r}")
            check("the bad key falls back to its default",
                  cfg.num_threads == defaults.num_threads, repr(cfg.num_threads))
            check("an int is accepted for a float field",
                  cfg.vad_threshold == 1.0 and isinstance(cfg.vad_threshold, float))
            dropped = [f for ev, f in events if ev == "config_error"]
            check("the dropped key is named in a config_error event",
                  len(dropped) == 1 and dropped[0].get("key") == "num_threads"
                  and dropped[0].get("reason") == "bad_value", str(events))
            check("the stderr line names the key too",
                  "num_threads" in err.getvalue(), err.getvalue().strip()[:80])
            check("no backup is made for a parseable file with one bad key",
                  len(list(cfg_dir.glob("config.json.broken-*"))) == 1
                  and cfg_file.exists())

            # --- a valid file is untouched -----------------------------------
            events.clear()
            cfg_file.write_text(json.dumps({"hotkey_hold": "Meta+Z", "num_threads": 2}))
            cfg = da_config.AppConfig.load(log=lambda ev, **f: events.append((ev, f)))
            check("a valid file loads with no events",
                  cfg.hotkey_hold == "Meta+Z" and cfg.num_threads == 2 and not events)

            # --- not an object at all ----------------------------------------
            cfg_file.write_text("[1, 2, 3]")
            events.clear()
            cfg = da_config.AppConfig.load(log=lambda ev, **f: events.append((ev, f)))
            check("a JSON array is treated as unparseable and backed up",
                  cfg.num_threads == defaults.num_threads
                  and any(f.get("reason") == "unparseable" for ev, f in events)
                  and len(list(cfg_dir.glob("config.json.broken-*"))) == 2)
        finally:
            da_config.CONFIG_DIR, da_config.CONFIG_FILE = saved


# ---------------------------------------------------------------------------
# #3  ASREngine.stop() returns only once capture has stopped AND the decode
#     queue has drained (bounded); a start() in that window is queued.
# ---------------------------------------------------------------------------

def _wait_decoding(timeout=5.0):
    DECODE["started"].clear()
    return DECODE["started"].wait(timeout)


def defect3_engine_stop():
    print("\n[#3] ASREngine.stop(): capture stopped, decoder drained, starts queued")
    one_second = np.zeros(SR, dtype=np.float32)
    default_bound = getattr(da_engine.ASREngine, "DRAIN_TIMEOUT_S", None)

    # --- A. a decode longer than the bound: stop() gives up on time and the
    #        late result is dropped, never delivered into the next take ------
    print("  A. decode outlives the drain bound")
    da_engine.ASREngine.DRAIN_TIMEOUT_S = 1.0
    DECODE["seconds"] = 3.0
    texts = []
    engine = make_engine([one_second], texts)
    before = len(diag_lines())
    DECODE["started"].clear()
    engine.start()
    check("the fake decoder was reached", DECODE["started"].wait(5))
    t0 = time.monotonic()
    engine.stop()
    dt = time.monotonic() - t0
    lines = diag_lines()[before:]
    check("stop() returned once the bound elapsed, not once the decode did",
          0.9 <= dt < 2.5, f"{dt:.2f} s")
    check("drain_timeout logged", any("event=drain_timeout" in l for l in lines))
    check("no text had been delivered when stop() returned", texts == [], str(texts))
    check("engine reports not running after stop()", not engine.is_running)
    time.sleep(3.0)                       # let the abandoned decode finish
    lines = diag_lines()[before:]
    check("the late result was dropped, not delivered", texts == [], str(texts))
    check("the drop is logged", any("event=late_text_dropped" in l for l in lines))
    check("the run thread has ended", not engine._thread.is_alive())

    # --- B. a long decode inside the bound: stop() waits for it -------------
    print("  B. decode inside the bound")
    da_engine.ASREngine.DRAIN_TIMEOUT_S = 60.0
    DECODE["seconds"] = 6.0
    texts = []
    engine = make_engine([one_second], texts)
    before = len(diag_lines())
    DECODE["started"].clear()
    engine.start()
    check("decoder reached", DECODE["started"].wait(5))
    t0 = time.monotonic()
    engine.stop()
    t_ret = time.monotonic()
    lines = diag_lines()[before:]
    check("stop() blocked until the decode finished",
          5.5 <= t_ret - t0 < 9.0, f"{t_ret - t0:.2f} s")
    check("text delivered exactly once, BEFORE stop() returned",
          len(texts) == 1 and texts[0][0] <= t_ret,
          f"{len(texts)} texts" + (f", {(texts[0][0] - t_ret) * 1000:+.0f} ms vs return"
                                   if texts else ""))
    check("no drain_timeout for a decode inside the bound",
          not any("event=drain_timeout" in l for l in lines))
    check("run thread ended, engine not running",
          not engine._thread.is_alive() and not engine.is_running)

    # --- C. start() while the previous session is still draining ----------
    print("  C. start() during the drain is queued behind it")
    DECODE["seconds"] = 4.0
    texts = []
    engine = make_engine([one_second], texts)
    before = len(diag_lines())
    n_streams = len(STREAMS)
    DECODE["started"].clear()
    engine.start()
    check("decoder reached", DECODE["started"].wait(5))
    stopper = threading.Thread(target=engine.stop)
    stopper.start()
    time.sleep(0.3)                       # stop() is now inside the drain
    engine._build_vad = lambda: ScriptedVad([])   # the queued take says nothing
    t0 = time.monotonic()
    engine.start()
    dt = time.monotonic() - t0
    check("start() during the drain returns at once (queued, not blocking)",
          dt < 0.2, f"{dt * 1000:.0f} ms")
    time.sleep(0.5)
    check("no second stream is opened while the first session is draining",
          len(STREAMS) == n_streams + 1, f"{len(STREAMS) - n_streams} streams")
    stopper.join(20)
    time.sleep(0.5)
    lines = diag_lines()[before:]
    first, second = (STREAMS[n_streams], STREAMS[n_streams + 1]
                     if len(STREAMS) > n_streams + 1 else None)
    check("the second session opened only after the first had ended",
          second is not None and first.t_close is not None
          and second.t_open >= first.t_close,
          "no second session" if second is None else
          f"{(second.t_open - first.t_close) * 1000:+.0f} ms after the first closed")
    check("the first session's text landed before the second opened",
          len(texts) == 1 and second is not None and texts[0][0] <= second.t_open,
          str(len(texts)))
    order = [l.split("event=")[1].split()[0] for l in lines
             if "event=session_start" in l or "event=session_stop" in l]
    check("log order: session_start, session_stop, session_start",
          order == ["session_start", "session_stop", "session_start"], str(order))
    check("the queued start is logged", any("event=session_queued" in l for l in lines))
    check("the second session is running", engine.is_running)
    t0 = time.monotonic()
    engine.stop()
    check("the second session stops cleanly",
          not engine.is_running and not engine._thread.is_alive()
          and time.monotonic() - t0 < 3.0)
    check("still exactly one text for the two sessions", len(texts) == 1)

    # --- D. press and release during the drain: the queued session never
    #        opens the microphone at all -------------------------------------
    print("  D. start() then stop() during the drain")
    DECODE["seconds"] = 2.0
    texts = []
    engine = make_engine([one_second], texts)
    before = len(diag_lines())
    n_streams = len(STREAMS)
    DECODE["started"].clear()
    engine.start()
    check("decoder reached", DECODE["started"].wait(5))
    stopper = threading.Thread(target=engine.stop)
    stopper.start()
    time.sleep(0.2)
    engine.start()                        # queued
    time.sleep(0.1)
    t0 = time.monotonic()
    engine.stop()                         # cancels the queued one
    dt = time.monotonic() - t0
    stopper.join(20)
    time.sleep(0.3)
    lines = diag_lines()[before:]
    check("only the first session ever opened a stream",
          len(STREAMS) == n_streams + 1, f"{len(STREAMS) - n_streams} streams")
    check("the cancelled session is logged as skipped",
          any("event=session_skipped" in l for l in lines))
    check("engine idle afterwards",
          not engine.is_running and not engine._thread.is_alive())
    check("the first session's text still landed once", len(texts) == 1)

    if default_bound is not None:
        da_engine.ASREngine.DRAIN_TIMEOUT_S = default_bound


# ---------------------------------------------------------------------------

SECTIONS = [defect2_config_load, defect3_engine_stop]


def main():
    for section in SECTIONS:
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
    sys.exit(main())
