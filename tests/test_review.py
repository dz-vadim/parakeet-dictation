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


# --- controller harness: stub engine, fake overlay, a pumped GLib main loop ---
# (goes right after the #1 section in tests/test_review.py)

from parakeet_dictation import (controller as da_controller, engine as da_engine,  # noqa: E402
                                insert as da_insert)
from gi.repository import GLib  # noqa: E402

for _mod in (da_controller, da_engine, da_insert):
    _mod.DIAG = TEST_DIAG

# apply_config() calls AppConfig.save(): point that at scratch, never at ~/.config.
_CFG_TMP = Path(tempfile.mkdtemp(prefix="parakeet-review-"))
da_config.CONFIG_DIR = _CFG_TMP
da_config.CONFIG_FILE = _CFG_TMP / "config.json"


class GLibInline:
    """idle_add runs the callback at once, on the calling thread (engine tests)."""
    SOURCE_CONTINUE = True
    SOURCE_REMOVE = False
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


class _Completed:
    def __init__(self, rc=0, out=b""):
        self.returncode, self.stdout, self.stderr = rc, out, b""


class StubEngine:
    """The controller's view of an engine, with no thread and no microphone."""

    recognizer_key = ("stub", "", 0, "")

    def __init__(self):
        self.running = False
        self.paused = False
        self.starts = 0
        self.stops = 0

    @property
    def is_running(self):
        return self.running

    @property
    def is_paused(self):
        return self.paused

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1
        self.running = False

    def wait_drained(self, timeout=60.0):
        return True

    def pause(self):
        pass


class FakeOverlay:
    def __init__(self):
        self.states = []

    def set_state(self, state, message=""):
        self.states.append((state, message))

    def push_level(self, *_a):
        pass

    def capture_started(self):
        pass

    def set_capture_probe(self, _p):
        pass

    def apply_config(self, _c):
        pass

    def preview_append(self, _t):
        pass

    def preview_hypothesis(self, _t):
        pass

    def preview_reset(self):
        pass


def make_controller(stub=True, **cfg):
    config = da_config.AppConfig(**cfg)
    with redirect_stderr(io.StringIO()):
        ctl = da_controller.DictationController(config)
    eng = None
    if stub:
        eng = StubEngine()
        ctl._engine = eng
    ov = FakeOverlay()
    ctl.set_overlay(ov)
    return ctl, eng, ov


def capture_up(ctl, eng):
    """What the engine's idle_add does once the input stream is open."""
    eng.running = True
    ctl._on_capture_start()


def pump(ms):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, lambda: (loop.quit(), False)[1])
    loop.run()


def pump_until(cond, timeout_ms=3000, step_ms=20):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not cond() and time.monotonic() < deadline:
        pump(step_ms)
    return cond()


def wait_typer(typer, timeout=5.0):
    """Block until the typer has nothing queued (a no-op for a synchronous typer)."""
    wait = getattr(typer, "wait_idle", None)
    if callable(wait):
        wait(timeout)


def events(lines, name):
    return [l for l in lines if f"event={name} " in l or l.endswith(f"event={name}")]


# ---------------------------------------------------------------------------
# #2  An exception in the typer must not leave the gesture at STOPPING: the
#     take ends whatever the insertion did, and an OSError from a helper
#     (E2BIG, EPERM) is reported through on_failure instead of escaping.
# ---------------------------------------------------------------------------

def review2_take_ends_even_if_the_typer_raises():
    print("\n[#2] the take ends even when the typer raises")

    # --- the typer itself: an OSError from wl-copy is reported, not raised --
    saved = (da_insert.subprocess.run, da_insert.shutil.which, da_insert.portal_keyboard)
    da_insert.portal_keyboard = lambda: None
    da_insert.shutil.which = lambda n: f"/usr/bin/{n}" if n == "ydotool" else None

    def run(args, **_kw):
        if args[0] == "wl-copy":
            raise OSError(7, "Argument list too long", "wl-copy")
        return _Completed(0, b"")

    da_insert.subprocess.run = run
    failures = []
    typer = da_insert.TextTyper("clipboard", on_failure=failures.append)
    before = len(diag_lines())
    raised = None
    try:
        with redirect_stderr(io.StringIO()):
            typer.type_text("hello")
            wait_typer(typer)
    except Exception as e:
        raised = e
    finally:
        (da_insert.subprocess.run, da_insert.shutil.which,
         da_insert.portal_keyboard) = saved
    check("an OSError from the staging helper does not escape type_text",
          raised is None, repr(raised))
    check("it is reported through on_failure, naming the helper",
          bool(failures) and "wl-copy" in failures[-1], str(failures))
    check("and logged as insert_failed with the error type",
          any("event=insert_failed" in l and "err=OSError" in l
              for l in diag_lines()[before:]),
          str([l.split("event=")[1][:50] for l in diag_lines()[before:]]))

    # --- the controller: whatever escapes the typer, the take still ends ----
    class RaisingTyper(da_insert.TextTyper):
        def _type_raw(self, text, target=None):
            raise PermissionError(13, "Permission denied", "/dev/uinput")

    ctl, eng, _ov = make_controller()
    ctl._typer = RaisingTyper("clipboard")
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl._on_final_text("some words")
    ctl.hold_release()
    check("release puts the gesture at stopping", ctl.gesture == "stopping", ctl.gesture)
    before = len(diag_lines())
    with redirect_stderr(io.StringIO()):
        ended = pump_until(lambda: ctl.gesture == "idle", 4000)
    check("the gesture is back at idle after the typer raised", ended, ctl.gesture)
    check("take_end was logged for that take",
          any("event=take_end" in l and "take=1" in l for l in diag_lines()[before:]))
    ctl.hold_press()
    check("the next press opens a new take (dictation is not dead)",
          ctl.gesture == "starting" and eng.starts == 2,
          f"gesture={ctl.gesture} starts={eng.starts}")
    capture_up(ctl, eng)
    ctl.hold_release()
    with redirect_stderr(io.StringIO()):
        pump_until(lambda: ctl.gesture == "idle", 4000)
    check("and that take ends too", ctl.gesture == "idle" and eng.stops == 2,
          f"gesture={ctl.gesture} stops={eng.stops}")
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #3  apply_config() must preload when the recognizer key changes, even though
#     the Settings dialog hands back the very same (mutated) config object.
# ---------------------------------------------------------------------------

def review3_apply_config_preloads_on_model_switch():
    print("\n[#3] apply_config preloads after a model switch on the shared config object")
    ctl, _eng, _ov = make_controller(stub=False)
    preloads = []
    ctl.preload = lambda: preloads.append(ctl._engine)
    cfg = ctl.config                       # what SettingsDialog does: same object
    cfg.model_profile = "laptop"
    with redirect_stderr(io.StringIO()):
        ctl.apply_config(cfg)
    check("switching the profile on the shared config object triggers a preload",
          len(preloads) == 1, f"{len(preloads)} preload(s)")
    check("the preload targets the engine built for the NEW profile",
          bool(preloads) and preloads[0] is ctl._engine
          and ctl._engine._profile is ctl.profiles["laptop"])
    with redirect_stderr(io.StringIO()):
        ctl.apply_config(cfg)
    check("saving unchanged settings does not preload again", len(preloads) == 1,
          f"{len(preloads)} preload(s)")
    cfg.num_threads = cfg.num_threads + 1
    with redirect_stderr(io.StringIO()):
        ctl.apply_config(cfg)
    check("a thread-count change preloads", len(preloads) == 2, f"{len(preloads)}")
    cfg.language = "uk"
    with redirect_stderr(io.StringIO()):
        ctl.apply_config(cfg)
    check("a language change preloads", len(preloads) == 3, f"{len(preloads)}")
    cfg.beep_volume = 0.1
    with redirect_stderr(io.StringIO()):
        ctl.apply_config(cfg)
    check("a change that does not touch the recognizer does not",
          len(preloads) == 3, f"{len(preloads)}")
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #4  HotkeyManager.rebuild() must drop the previous kglobalaccel signal
#     subscriptions, or every settings save adds one more handler per key.
# ---------------------------------------------------------------------------

class FakeGioBus:
    """Gio.DBusConnection as KGlobalAccelHotkey uses it.  Like the real one it
    has signal_unsubscribe() and no unsubscribe()."""

    def __init__(self):
        self.subs = {}
        self._next = 1
        self.calls = []

    def call_sync(self, _service, _path, _iface, method, _params, _reply_type,
                  _flags, _timeout, _cancellable):
        self.calls.append(method)
        if method == "globalShortcutAvailable":
            return GLib.Variant("(b)", (True,))
        if method == "getComponent":
            return GLib.Variant("(o)", ("/component/parakeet_dictation",))
        if method == "setShortcutKeys":
            return GLib.Variant("(a(ai))", ([([0x1000000],)],))
        return None

    def signal_subscribe(self, _sender, _iface, signal, _path, _arg0, _flags, handler):
        sid = self._next
        self._next += 1
        self.subs[sid] = (signal, handler)
        return sid

    def signal_unsubscribe(self, sid):
        del self.subs[sid]

    def emit(self, signal, component, action):
        for _sid, (sig, handler) in list(self.subs.items()):
            if sig == signal:
                handler(self, ":1.9", "/component/parakeet_dictation",
                        "org.kde.kglobalaccel.Component", signal,
                        GLib.Variant("(ssx)", (component, action, 0)))


class FakeGlobalHotKeys:
    def __init__(self, _bindings):
        self.daemon = False

    def start(self):
        pass

    def stop(self):
        pass


def review4_hotkey_rebuild_unsubscribes():
    print("\n[#4] rebuild() leaves exactly one press/release subscription pair")
    from types import SimpleNamespace
    from gi.repository import Gio
    from parakeet_dictation import hotkeys as da_hotkeys
    da_hotkeys.DIAG = TEST_DIAG
    bus = FakeGioBus()
    saved_gio = da_hotkeys.Gio
    saved_pynput = sys.modules.get("pynput")
    da_hotkeys.Gio = SimpleNamespace(
        bus_get_sync=lambda *_a: bus, BusType=Gio.BusType,
        DBusCallFlags=Gio.DBusCallFlags, DBusSignalFlags=Gio.DBusSignalFlags)
    sys.modules["pynput"] = SimpleNamespace(
        keyboard=SimpleNamespace(GlobalHotKeys=FakeGlobalHotKeys))
    presses, releases = [], []
    try:
        cfg = da_config.AppConfig(hotkey_mode="hold")
        mgr = da_hotkeys.HotkeyManager(
            cfg, on_toggle=lambda: None, on_start=lambda: None, on_stop=lambda: None,
            on_pause=lambda: None, on_hold_press=lambda: presses.append(1),
            on_hold_release=lambda: releases.append(1))
        status = mgr.start()
        check("hold mode registers cleanly and subscribes to press and release",
              status == "" and len(bus.subs) == 2, f"status={status!r} subs={len(bus.subs)}")
        for _ in range(3):
            mgr.rebuild(cfg)
        check("after three rebuilds exactly two subscriptions remain",
              len(bus.subs) == 2, f"{len(bus.subs)} subscriptions")
        bus.emit("globalShortcutPressed", da_hotkeys.KGlobalAccelHotkey.COMPONENT, "push-to-talk")
        bus.emit("globalShortcutReleased", da_hotkeys.KGlobalAccelHotkey.COMPONENT, "push-to-talk")
        check("one key press fires the handler once, not four times",
              presses == [1] and releases == [1], f"presses={len(presses)} releases={len(releases)}")
        check("each rebuild unregistered the previous action with kglobalaccel",
              bus.calls.count("unRegister") == 3 and bus.calls.count("doRegister") == 4,
              f"unRegister x{bus.calls.count('unRegister')}, doRegister x{bus.calls.count('doRegister')}")
        mgr.stop()
        check("stop() leaves no subscription behind", len(bus.subs) == 0, str(bus.subs))
    finally:
        da_hotkeys.Gio = saved_gio
        if saved_pynput is None:
            sys.modules.pop("pynput", None)
        else:
            sys.modules["pynput"] = saved_pynput


# ---------------------------------------------------------------------------
# #5  The recognizer cache is keyed on the language too: a Canary profile
#     built for "en" must be rebuilt for "uk".
# ---------------------------------------------------------------------------

class FakeRecognizer:
    def create_stream(self):
        from types import SimpleNamespace
        return SimpleNamespace(accept_waveform=lambda *_a: None)

    def decode_stream(self, _s):
        pass

    def is_ready(self, _s):
        return False


def review5_recognizer_cache_keyed_on_language():
    print("\n[#5] the recognizer cache key includes the language")
    saved = (da_engine._cached_key, da_engine._cached_recognizer)
    builds = []

    def build(tag):
        def _b():
            builds.append(tag)
            return FakeRecognizer()
        return _b

    try:
        before = len(diag_lines())
        r1, _ = da_engine.get_recognizer("offline", "desktop", 4, "en", build("en"))
        r2, ms = da_engine.get_recognizer("offline", "desktop", 4, "en", build("en-again"))
        check("same profile, threads and language: a cache hit",
              r2 is r1 and ms == 0.0 and builds == ["en"], str(builds))
        r3, _ = da_engine.get_recognizer("offline", "desktop", 4, "uk", build("uk"))
        check("a language change rebuilds the recognizer",
              r3 is not r1 and builds == ["en", "uk"], str(builds))
        r4, _ = da_engine.get_recognizer("offline", "desktop", 4, "en", build("en-back"))
        check("(one slot: switching back rebuilds again)",
              r4 is not r3 and builds == ["en", "uk", "en-back"], str(builds))
        check("recognizer_loaded logs the language",
              any("event=recognizer_loaded" in l and "language=uk" in l
                  for l in diag_lines()[before:]))
        e_en = da_engine.ASREngine(da_config.AppConfig(language="en"),
                                   {"streaming": False, "files": {}},
                                   on_text=None, on_partial=None, on_error=None)
        e_uk = da_engine.ASREngine(da_config.AppConfig(language="uk"),
                                   {"streaming": False, "files": {}},
                                   on_text=None, on_partial=None, on_error=None)
        check("ASREngine.recognizer_key carries the language",
              e_en.recognizer_key != e_uk.recognizer_key
              and e_en.recognizer_key[:3] == e_uk.recognizer_key[:3],
              f"{e_en.recognizer_key} vs {e_uk.recognizer_key}")
    finally:
        da_engine._cached_key, da_engine._cached_recognizer = saved


# ---------------------------------------------------------------------------

SECTIONS = {1: review1_vad_speech_floor,
            2: review2_take_ends_even_if_the_typer_raises,
            3: review3_apply_config_preloads_on_model_switch,
            4: review4_hotkey_rebuild_unsubscribes,
            5: review5_recognizer_cache_keyed_on_language}


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
