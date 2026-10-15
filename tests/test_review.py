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


class FakeNameWatch:
    """Gio.bus_watch_name_on_connection / bus_unwatch_name for one name."""

    def __init__(self):
        self.watches = {}
        self._next = 1

    def watch(self, _bus, name, _flags, appeared, vanished):
        wid = self._next
        self._next += 1
        self.watches[wid] = (name, appeared, vanished)
        appeared(None, name, ":1.5")      # like GIO: the name is owned right now
        return wid

    def unwatch(self, wid):
        self.watches.pop(wid, None)

    def vanish(self, name):
        for n, _a, vanished in list(self.watches.values()):
            if n == name:
                vanished(None, name)

    def appear(self, name):
        for n, appeared, _v in list(self.watches.values()):
            if n == name:
                appeared(None, name, ":1.77")


def fake_hotkeys_gio(bus, names=None):
    from types import SimpleNamespace
    from gi.repository import Gio
    names = names or FakeNameWatch()
    return SimpleNamespace(
        bus_get_sync=lambda *_a: bus, BusType=Gio.BusType,
        DBusCallFlags=Gio.DBusCallFlags, DBusSignalFlags=Gio.DBusSignalFlags,
        BusNameWatcherFlags=Gio.BusNameWatcherFlags,
        bus_watch_name_on_connection=names.watch, bus_unwatch_name=names.unwatch)


def review4_hotkey_rebuild_unsubscribes():
    print("\n[#4] rebuild() leaves exactly one press/release subscription pair")
    from types import SimpleNamespace
    from gi.repository import Gio
    from parakeet_dictation import hotkeys as da_hotkeys
    da_hotkeys.DIAG = TEST_DIAG
    bus = FakeGioBus()
    saved_gio = da_hotkeys.Gio
    saved_pynput = sys.modules.get("pynput")
    da_hotkeys.Gio = fake_hotkeys_gio(bus)
    sys.modules["pynput"] = SimpleNamespace(
        keyboard=SimpleNamespace(GlobalHotKeys=FakeGlobalHotKeys,
                                 HotKey=SimpleNamespace(parse=lambda b: [b])))
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
# #6  The streaming path commits what is still in the stream when the
#     session stops — the tail after the last endpoint used to be lost.
# ---------------------------------------------------------------------------

class FakeOnlineStream:
    def __init__(self):
        self.pending = 0
        self.decoded = 0
        self.finished = False

    def accept_waveform(self, _sr, _samples):
        self.pending += 1

    def input_finished(self):
        self.finished = True
        self.pending += 1          # the frames the encoder was holding back


class FakeOnlineRecognizer:
    """sherpa's OnlineRecognizer as _run_streaming drives it: one more word
    per decoded chunk, and the endpoint never fires (the user let go
    mid-sentence)."""

    def __init__(self, words):
        self._words = words
        self.resets = 0

    def create_stream(self):
        return FakeOnlineStream()

    def is_ready(self, s):
        return s.pending > 0

    def decode_stream(self, s):
        s.pending -= 1
        s.decoded += 1

    def get_result(self, s):
        return " ".join(self._words[:min(s.decoded, len(self._words))])

    def is_endpoint(self, _s):
        return False

    def reset(self, s):
        self.resets += 1
        s.decoded = 0


class CountedStream:
    """N reads of silence, then the stop flag."""

    def __init__(self, reads, stop_event):
        self._left = reads
        self._stop = stop_event

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, n):
        self._left -= 1
        if self._left <= 0:
            self._stop.set()
        return np.zeros((n, 1), dtype=np.float32), False


def _streaming_session(partial_overwrite):
    words = ["one", "two", "three", "four", "five", "six"]
    texts, commits, partials = [], [], []
    config = da_config.AppConfig(partial_overwrite=partial_overwrite)
    engine = da_engine.ASREngine(
        config, {"streaming": True, "files": {}},
        on_text=texts.append, on_partial=partials.append, on_error=lambda e: texts.append(f"ERROR {e}"),
        on_partial_type=lambda t: None, on_commit_partial=commits.append)
    rec = FakeOnlineRecognizer(words)
    engine._acquire_online_recognizer = lambda: (rec, 0.0)
    stop = threading.Event()
    da_engine.sd.InputStream = lambda **_kw: CountedStream(5, stop)
    engine._run_streaming(stop)
    return texts, commits, partials, rec


def review6_streaming_flushes_tail_on_stop():
    print("\n[#6] the streaming session commits its tail when it stops")
    texts, commits, partials, rec = _streaming_session(partial_overwrite=False)
    check("five chunks decoded to a five-word partial before the stop",
          "one two three four five" in partials, str(partials[-2:]))
    check("the stop committed the whole tail, including the frames held back",
          texts == ["one two three four five six"], str(texts))
    check("no endpoint fired, so this came from the end-of-session flush",
          rec.resets == 0)

    texts, commits, partials, rec = _streaming_session(partial_overwrite=True)
    check("with partial_overwrite the tail goes through commit_partial",
          commits == ["one two three four five six"] and texts == [],
          f"commits={commits} texts={texts}")


# ---------------------------------------------------------------------------
# #7  A press during the STOPPING window is a queued start, not a dropped
#     key repeat: the next take opens by itself once the previous one ends.
# ---------------------------------------------------------------------------

def review7_press_while_stopping_is_queued():
    print("\n[#7] a press while stopping queues the next take")
    inserted = []

    class RecordingTyper(da_insert.TextTyper):
        def _type_raw(self, text, target=None):
            inserted.append(text)

    ctl, eng, _ov = make_controller()
    ctl._typer = RecordingTyper("clipboard")
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl._on_final_text("first take")
    ctl.hold_release()
    check("release puts the gesture at stopping", ctl.gesture == "stopping", ctl.gesture)
    before = len(diag_lines())
    ctl.hold_press()                       # before the release tail has even fired
    check("the press is logged as queued, not ignored",
          any("event=hold_press_queued" in l for l in diag_lines()[before:])
          and not any("event=hold_press_ignored" in l for l in diag_lines()[before:]),
          str([l.split("event=")[1][:40] for l in diag_lines()[before:]]))
    check("no second session is opened while the first is still stopping",
          eng.starts == 1, f"starts={eng.starts}")
    opened = pump_until(lambda: eng.starts == 2, 4000)
    check("the next take opens by itself once the previous one has ended",
          opened and ctl.gesture == "starting", f"starts={eng.starts} gesture={ctl.gesture}")
    lines = diag_lines()[before:]
    ends = [i for i, l in enumerate(lines) if "event=take_end" in l and "take=1" in l]
    starts = [i for i, l in enumerate(lines) if "event=gesture" in l and "to=starting" in l]
    check("take 1 ended before take 2 started", ends and starts and ends[0] < starts[0],
          f"end at {ends[:1]}, start at {starts[:1]}")
    wait_typer(ctl._typer)
    check("the first take's text was inserted — nothing lost",
          [t.strip() for t in inserted] == ["first take"], str(inserted))
    capture_up(ctl, eng)
    check("capture up moves the queued take to recording", ctl.gesture == "recording",
          ctl.gesture)
    check("_decide_stop on its release is PROCEED",
          ctl._decide_stop() == da_controller.STOP_PROCEED, ctl._decide_stop())
    ctl._on_final_text("second take")
    ctl.hold_release()
    ended = pump_until(lambda: ctl.gesture == "idle", 4000)
    wait_typer(ctl._typer)
    check("the second take ends normally", ended and eng.stops == 2,
          f"gesture={ctl.gesture} stops={eng.stops}")
    check("both takes reached the document, in order",
          [t.strip() for t in inserted] == ["first take", "second take"], str(inserted))

    # A press AND release inside the window: nothing could be captured, so
    # nothing is queued.
    before = len(diag_lines())
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl.hold_release()
    ctl.hold_press()
    ctl.hold_release()
    pump_until(lambda: ctl.gesture == "idle", 4000)
    pump(300)
    check("a tap that ends inside the stop window opens no take",
          eng.starts == 3 and ctl.gesture == "idle",
          f"starts={eng.starts} gesture={ctl.gesture}")
    check("and says so in the log",
          any("event=queued_start_cancelled" in l for l in diag_lines()[before:]))
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #8  The CLI downloader is atomic: an interrupted transfer leaves no final
#     file (and no .part) for the exists()/size checks to be fooled by.
# ---------------------------------------------------------------------------

def review8_cli_download_is_atomic():
    print("\n[#8] the CLI download leaves nothing behind when interrupted")
    from types import SimpleNamespace
    from parakeet_dictation import models as da_models
    updates = []

    class FakeResponse:
        def __init__(self, chunks, fail_after):
            self.headers = {"content-length": str(sum(len(c) for c in chunks))}
            self._chunks, self._fail = chunks, fail_after

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=None):
            for i, c in enumerate(self._chunks):
                if i == self._fail:
                    raise ConnectionError("connection reset by peer")
                yield c

    class FakeBar:
        def __init__(self, **kw):
            self.n, self.total = 0, kw.get("total")

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def update(self, n):
            self.n += n
            updates.append(n)

        def refresh(self):
            pass

    responses = {}
    fake_requests = SimpleNamespace(get=lambda url, stream=True, timeout=None: responses[url])
    fake_tqdm = SimpleNamespace(tqdm=FakeBar)
    saved = (sys.modules.get("requests"), sys.modules.get("tqdm"))
    sys.modules["requests"], sys.modules["tqdm"] = fake_requests, fake_tqdm
    try:
        with tempfile.TemporaryDirectory(prefix="parakeet-dl-") as tmp:
            dest = Path(tmp) / "encoder.onnx"
            responses["u1"] = FakeResponse([b"x" * 1000, b"y" * 1000, b"z" * 1000], fail_after=2)
            raised = None
            try:
                da_models.download_file("u1", dest)
            except Exception as e:
                raised = e
            check("the interrupted download raises", raised is not None, repr(raised))
            check("no final file is left behind", not dest.exists())
            check("and no .part either", not list(Path(tmp).iterdir()),
                  str([p.name for p in Path(tmp).iterdir()]))
            check("progress was reported through the bar for the bytes that arrived",
                  sum(updates) == 2000, str(updates))

            updates.clear()
            responses["u2"] = FakeResponse([b"x" * 1000, b"y" * 1000], fail_after=None)
            da_models.download_file("u2", dest)
            check("a completed download lands under its final name",
                  dest.exists() and dest.stat().st_size == 2000
                  and [p.name for p in Path(tmp).iterdir()] == ["encoder.onnx"])
            check("with the whole size reported", sum(updates) == 2000, str(updates))
    finally:
        for name, mod in zip(("requests", "tqdm"), saved):
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# #9  The preview pass must keep decoding while audio keeps arriving, even
#     once the open phrase is longer than the window (the windowed size then
#     never changes, which used to read as "no new audio").
# ---------------------------------------------------------------------------

class FullWindowVad:
    """A phrase that has been open for longer than the preview window."""

    def open_tail(self, max_samples):
        return np.full(max_samples, 0.01, dtype=np.float32)


class EmptyCoalescer:
    closed = 0

    def tail(self, _max):
        return np.zeros(0, dtype=np.float32)


def review9_preview_keeps_going_beyond_the_window():
    print("\n[#9] the preview pass keeps producing hypotheses beyond the window")
    previews = []
    config = da_config.AppConfig(preview_interval_s=0.2, preview_window_s=1.0, normalize=False)
    engine = da_engine.ASREngine(config, {"streaming": False, "files": {}},
                                 on_text=None, on_partial=None, on_error=None,
                                 on_preview=previews.append)
    engine._acquire_offline_recognizer = lambda: (object(), 0.0)
    saved = da_engine.ASREngine._decode_prepared
    da_engine.ASREngine._decode_prepared = staticmethod(
        lambda _rec, audio: f"guess after {len(previews) + 1} passes")
    stats = {"preview_passes": 0, "preview_skipped": 0, "preview_ms": 0.0, "captured": 0}
    stop, lock = threading.Event(), threading.Lock()
    try:
        worker = threading.Thread(target=engine._preview_worker,
                                  args=(FullWindowVad(), EmptyCoalescer(), lock, stats, stop),
                                  daemon=True)
        worker.start()
        for _ in range(12):                 # the capture thread: 100 ms blocks
            time.sleep(0.1)
            with lock:
                stats["captured"] += 1600
        stop.set()
        worker.join(3)
    finally:
        da_engine.ASREngine._decode_prepared = saved
    check("the pass keeps producing hypotheses while audio keeps coming",
          stats["preview_passes"] >= 4, f"{stats['preview_passes']} passes in 1.2 s")
    check("each of them reached the preview callback",
          len(previews) == stats["preview_passes"] and len(set(previews)) == len(previews),
          str(previews[-2:]))
    check("the worker stopped with the session", not worker.is_alive())


# ---------------------------------------------------------------------------
# #10 _BlockCoalescer counts a block handed straight out (coalescing off) as
#     closed, so the preview's staleness sequence still moves.
# ---------------------------------------------------------------------------

def review10_coalescer_disabled_counts_closed_blocks():
    print("\n[#10] the disabled coalescer still bumps `closed`")
    seg = da_audio._SpeechSegment(list(np.zeros(SR, dtype=np.float32)))
    off = da_audio._BlockCoalescer(0.0)
    check("coalescing off, nothing closed yet", not off.enabled and off.closed == 0)
    blocks = off.add(seg)
    check("add() hands the segment straight out as one block",
          len(blocks) == 1 and blocks[0][1] == 1 and len(blocks[0][0]) == SR)
    check("and counts it as closed", off.closed == 1, str(off.closed))
    off.add(seg)
    check("twice for two segments", off.closed == 2, str(off.closed))
    check("flush() with nothing pending closes nothing", off.flush() == [] and off.closed == 2)
    on = da_audio._BlockCoalescer(4.0)
    on.add(seg)
    check("(coalescing on: a pending block is not closed yet)", on.closed == 0)
    on.flush()
    check("(and flush closes it)", on.closed == 1)


# ---------------------------------------------------------------------------
# #11 test_defects.py defect 4 runs main() against a scratch config, and puts
#     the suite's diagnostics flag back afterwards.  Checked from outside, in
#     a subprocess, with a sentinel standing in for the user's config file.
# ---------------------------------------------------------------------------

def review11_test_defects_runs_main_on_a_scratch_config():
    print("\n[#11] test_defects defect 4 never reads the user's config")
    import subprocess
    with tempfile.TemporaryDirectory(prefix="parakeet-review-") as tmp:
        sentinel = Path(tmp) / "user-config.json"
        sentinel.write_text('{"diagnostics": false, "num_threads": 1}')
        probe = (
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            "import test_defects as td\n"
            "from parakeet_dictation import config as da_config\n"
            f"sentinel = pathlib.Path({str(sentinel)!r})\n"
            "da_config.CONFIG_DIR, da_config.CONFIG_FILE = sentinel.parent, sentinel\n"
            "mtime = sentinel.stat().st_mtime_ns\n"
            "td.defect4_single_instance()\n"
            "print('ENABLED', td.TEST_DIAG._enabled)\n"
            "print('SENTINEL_UNTOUCHED', sentinel.exists() and sentinel.stat().st_mtime_ns == mtime)\n"
            "print('FAILURES', len(td.FAILURES))\n")
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                              timeout=120, cwd=HERE.parent)
        out = proc.stdout
        check("the defect-4 section itself passes", "FAILURES 0" in out,
              (proc.stderr or out).strip()[-300:])
        check("main() did not read the user's config (diagnostics=false would have "
              "switched the suite's log off)", "ENABLED True" in out,
              [l for l in out.splitlines() if l.startswith("ENABLED")])
        check("the user's config file was never touched", "SENTINEL_UNTOUCHED True" in out)


# ---------------------------------------------------------------------------
# #12 ptt_state.py and overlay_safety.py locate the checkout from __file__.
# ---------------------------------------------------------------------------

def review12_tests_locate_the_checkout_from_file():
    print("\n[#12] the GLib test scripts carry no hard-coded checkout path")
    for name in ("ptt_state.py", "overlay_safety.py"):
        src = (HERE / name).read_text()
        check(f"{name} has no absolute /home/... path", "/home/" not in src)
        after = src.split("sys.path.insert", 1)[1][:160] if "sys.path.insert" in src else ""
        check(f"{name} derives sys.path from __file__", "__file__" in after, after[:80])


# ---------------------------------------------------------------------------
# #13 FocusTracker.snapshot() is cache-only; isScriptLoaded lives on a timer.
# ---------------------------------------------------------------------------

class CountingBus:
    def __init__(self):
        self.sync, self.async_, self.callback = [], [], None

    def call_sync(self, _service, _path, _iface, method, *_a):
        self.sync.append(method)
        return GLib.Variant("(b)", (True,))

    def call(self, _service, _path, _iface, method, _params, _reply_type, _flags,
             _timeout, _cancellable, callback, *_user):
        self.async_.append(method)
        self.callback = callback

    def call_finish(self, _result):
        return GLib.Variant("(b)", (False,))       # "not loaded"


class _Invocation:
    def return_value(self, _v):
        pass


def review13_focus_snapshot_is_cache_only():
    print("\n[#13] snapshot() makes no D-Bus call")
    from parakeet_dictation import focus as da_focus
    da_focus.DIAG = TEST_DIAG
    tracker = da_focus.FocusTracker(enabled=True)
    bus = CountingBus()
    tracker._bus = bus
    tracker._loaded = False                 # the state that used to trigger a recheck
    tracker._last_check = 0.0
    tracker._on_report(None, None, None, None, None,
                       GLib.Variant("(ssss)", ("kitty", "kitty", "doc", "4242")), _Invocation())
    snap = tracker.snapshot()
    check("snapshot() returns the cached report",
          snap is not None and snap.resource_class == "kitty", str(snap))
    check("and makes no D-Bus call, synchronous or otherwise",
          bus.sync == [] and bus.async_ == [], f"sync={bus.sync} async={bus.async_}")
    for _ in range(5):
        tracker.snapshot()
    check("no matter how often it is asked", bus.sync == [] and bus.async_ == [],
          f"sync={bus.sync} async={bus.async_}")

    loads = []
    tracker._load = lambda reason: loads.append(reason) or True
    recheck = getattr(tracker, "_recheck", None)
    check("the periodic recheck exists", callable(recheck))
    if callable(recheck):
        cont = recheck()
        check("the timer tick asks KWin asynchronously whether the script is loaded",
              bus.async_ == ["isScriptLoaded"] and bus.sync == [] and cont is True,
              f"sync={bus.sync} async={bus.async_} cont={cont!r}")
        bus.callback(bus, None)
        check("a script that went away is reloaded from the timer, not from a key release",
              loads == ["stale"], str(loads))
    tracker._timer_id = GLib.timeout_add_seconds(60, lambda: True)
    tracker.stop()
    check("stop() disarms the timer", getattr(tracker, "_timer_id", 1) == 0)


# ---------------------------------------------------------------------------
# #14 Insertion runs off the calling (GTK main) thread, in order, and a run
#     of backspaces is one helper process.
# ---------------------------------------------------------------------------

def review14_insertion_off_thread_and_batched_backspaces():
    print("\n[#14] insertion is off the main thread; backspaces are batched")
    saved = (da_insert.subprocess.run, da_insert.shutil.which, da_insert.portal_keyboard)
    da_insert.portal_keyboard = lambda: None
    da_insert.shutil.which = lambda n: f"/usr/bin/{n}" if n == "ydotool" else None
    calls, threads = [], set()

    def run(args, **_kw):
        args = list(args)
        if args[0] == "wl-copy" and _kw.get("input") is not None:
            # Staged text arrives on stdin (C8); keep it as the last element
            # so the checks below read it like the argv it used to be.
            args = args + ["--", _kw["input"].decode()]
        calls.append(args)
        if args[0] == "ydotool" or (args[0] == "wl-copy" and "--" in args):
            threads.add(threading.get_ident())
        if args[0] == "ydotool":
            time.sleep(0.05)                # a slow chord
        if args[0] == "wl-paste":
            return _Completed(0, b"")
        return _Completed()

    da_insert.subprocess.run = run
    try:
        typer = da_insert.TextTyper("clipboard")
        me = threading.get_ident()
        t0 = time.monotonic()
        typer.type_text("one")
        typer.type_text("two")
        typer.type_text("three")
        dt = time.monotonic() - t0
        check("type_text returns without waiting for wl-copy, the settle or the chord",
              dt < 0.03, f"{dt * 1000:.0f} ms for three insertions")
        wait = getattr(typer, "wait_idle", None)
        check("the typer can be waited on (wait_idle)", callable(wait))
        if callable(wait):
            check("and wait_idle returns once the queue has drained", wait(5.0) is True)
        staged = [a[-1] for a in calls
                  if a[0] == "wl-copy" and "--" in a and "--primary" not in a]
        chords = [a for a in calls if a[0] == "ydotool"]
        check("the three insertions were staged and pasted in call order",
              staged == ["one ", "two ", "three "] and len(chords) == 3, str(staged))
        check("every helper ran off the calling thread", threads and me not in threads,
              f"{len(threads)} thread(s), caller among them: {me in threads}")
        check("and on one worker (the clipboard is a shared resource)", len(threads) == 1,
              f"{len(threads)} worker threads")
        time.sleep(da_insert.RESTORE_AFTER_S + 0.4)      # outlive the restores

        calls.clear()
        typer._send_backspaces(7)
        bs = [a for a in calls if a[0] == "ydotool"]
        check("seven backspaces are ONE ydotool invocation",
              len(bs) == 1 and bs[0][1] == "key" and bs[0][2:] == ["14:1", "14:0"] * 7,
              f"{len(bs)} invocation(s)")
        calls.clear()
        typer._send_backspaces(0)
        check("zero backspaces spawn nothing", calls == [])

        # Streaming partials: erase, then type, in order — through the same queue.
        calls.clear()
        typer.type_partial("hello")
        typer.type_partial("hello world")
        typer.commit_partial("hello world!")
        if callable(wait):
            wait(5.0)
        seq = []
        for a in calls:
            if a[0] == "wl-copy" and "--" in a and "--primary" not in a:
                seq.append(("stage", a[-1]))
            elif a[0] == "ydotool":
                seq.append(("backspace", len(a[2:]) // 2) if a[2] == "14:1" else ("chord",))
        check("partials: type, erase 5, type, erase 11, commit — each erase one process",
              seq == [("stage", "hello"), ("chord",), ("backspace", 5),
                      ("stage", "hello world"), ("chord",), ("backspace", 11),
                      ("stage", "hello world! "), ("chord",)], str(seq))
        time.sleep(da_insert.RESTORE_AFTER_S + 0.4)
    finally:
        (da_insert.subprocess.run, da_insert.shutil.which,
         da_insert.portal_keyboard) = saved


# ---------------------------------------------------------------------------
# #15 Cleanups (no behaviour change): one source for the class tables, and
#     the staging newline rule keyed on the class, not the chord.
# ---------------------------------------------------------------------------

def review15_cleanups_single_source_tables():
    print("\n[#15] cleanups: one class table, terminal flag from the class")
    check("config.py owns the terminal table and insert.py reads that very list",
          da_insert.DEFAULT_TERMINAL_CLASSES is da_config.DEFAULT_TERMINAL_CLASSES)
    check("same for the no-Ctrl+V table",
          da_insert.DEFAULT_NO_CTRL_V_CLASSES is da_config.DEFAULT_NO_CTRL_V_CLASSES)
    cfg = da_config.AppConfig()
    check("AppConfig's defaults are copies of them, not shared lists",
          cfg.terminal_window_classes == da_config.DEFAULT_TERMINAL_CLASSES
          and cfg.terminal_window_classes is not da_config.DEFAULT_TERMINAL_CLASSES
          and cfg.no_ctrl_v_classes == da_config.DEFAULT_NO_CTRL_V_CLASSES)
    check("a typer built without a config falls back to the same table",
          da_insert.TextTyper("clipboard").chord_config.terminal_window_classes
          == da_config.DEFAULT_TERMINAL_CLASSES)
    is_term = da_insert.is_terminal_class
    check("is_terminal_class follows the config table, case-insensitively",
          is_term("kitty", cfg) and is_term("KITTY", cfg) and not is_term("chromium", cfg)
          and not is_term("", cfg) and not is_term(None, cfg))
    over = da_config.AppConfig(paste_overrides={"kitty": "shift+insert"})
    check("a chord override does not un-terminal a terminal",
          da_insert.choose_chord("kitty", over)[0] == "shift+insert" and is_term("kitty", over))



# ===========================================================================
# 2026-10-15 full review (docs/reviews/2026-10-15-full-review.md).  Sections
# from #16 on carry the finding id of that review in their title.
# ===========================================================================

# ---------------------------------------------------------------------------
# #16 (C1)  Tap, then press-and-hold while capture is still opening: the
#     second press cancels the deferred stop the tap left behind, so the
#     take records for as long as the key is held.
# ---------------------------------------------------------------------------

def review16_press_cancels_deferred_stop():
    print("\n[#16 C1] a press while STARTING cancels the deferred stop")
    ctl, eng, ov = make_controller()
    ctl.hold_press()
    ctl.hold_release()                    # quick tap: capture not open yet
    check("the tap's release is deferred", ctl._stop_pending, ctl.gesture)
    before = len(diag_lines())
    ctl.hold_press()                      # pressed again and HELD
    check("the second press clears the deferred stop", not ctl._stop_pending)
    check("and says so in the log",
          bool(events(diag_lines()[before:], "deferred_stop_cancelled")))
    capture_up(ctl, eng)
    pump(400)
    check("capture open with the key held: the take is recording",
          ctl.gesture == "recording" and eng.stops == 0,
          f"gesture={ctl.gesture} stops={eng.stops}")
    check("and the pill says listening", ov.states[-1][0] == "listening", str(ov.states[-1]))
    ctl.hold_release()
    ended = pump_until(lambda: ctl.gesture == "idle", 4000)
    check("the real release still ends the take", ended and eng.stops == 1,
          f"gesture={ctl.gesture} stops={eng.stops}")
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #17 (C2)  A wl-copy that hangs (TimeoutExpired, not an OSError) is reported
#     and leaves no stale save/restore state behind for the next take.
# ---------------------------------------------------------------------------

class ClipboardWorld:
    """wl-copy / wl-paste / ydotool over two in-memory selections.  Text
    staged through argv (`-- text`) or stdin (`input=`) is accepted alike.
    `hang = (selection, n)` makes the next n wl-copy calls on that selection
    time out (subprocess.run raises TimeoutExpired once it has killed it)."""

    def __init__(self, clip=b"USER-OLD", primary=b"USER-P"):
        self.sel = {"clip": clip, "primary": primary}
        self.hang = (None, 0)
        self.pastes = []
        self.argv = []
        self.inputs = []

    def run(self, args, **kw):
        args = list(args)
        self.argv.append(args)
        key = "primary" if "--primary" in args else "clip"
        if args[0] == "wl-copy":
            if self.hang[0] == key and self.hang[1] > 0:
                self.hang = (key, self.hang[1] - 1)
                raise da_insert.subprocess.TimeoutExpired(args, kw.get("timeout", 5))
            if "--clear" in args:
                self.sel[key] = None
                return _Completed()
            if "--" in args:
                self.sel[key] = " ".join(args[args.index("--") + 1:]).encode()
            else:
                self.inputs.append(kw.get("input"))
                self.sel[key] = bytes(kw.get("input") or b"")
            return _Completed()
        if args[0] == "wl-paste":
            data = self.sel[key]
            return _Completed(1, b"") if data is None else _Completed(0, data)
        if args[0] in ("ydotool", "wtype"):
            self.pastes.append(self.sel["clip"])
        return _Completed()

    def install(self):
        saved = (da_insert.subprocess.run, da_insert.shutil.which,
                 da_insert.portal_keyboard)
        da_insert.subprocess.run = self.run
        da_insert.shutil.which = lambda n: f"/usr/bin/{n}" if n == "ydotool" else None
        da_insert.portal_keyboard = lambda: None

        def undo():
            (da_insert.subprocess.run, da_insert.shutil.which,
             da_insert.portal_keyboard) = saved
        return undo


def review17_wl_copy_timeout_is_reported():
    print("\n[#17 C2] a hung wl-copy is reported and resets the clipboard state")
    for hang in ("clip", "primary"):
        world = ClipboardWorld()
        world.hang = (hang, 1)
        undo = world.install()
        try:
            fails = []
            typer = da_insert.TextTyper("clipboard", on_failure=fails.append)
            typer.type_text("first take")
            typer.wait_idle()
            check(f"hung {hang} staging: the failure is reported",
                  any("wl-copy timed out" in f for f in fails), str(fails))
            check(f"hung {hang} staging: no chord was pressed", world.pastes == [],
                  str(world.pastes))
            time.sleep(da_insert.RESTORE_AFTER_S + 0.4)
            check(f"hung {hang} staging: no restore is left pending",
                  not typer._clip_restore_pending and typer._clip_saved is None,
                  f"pending={typer._clip_restore_pending}")
            check(f"hung {hang} staging: the user's selections are theirs again",
                  world.sel["clip"] == b"USER-OLD" and world.sel["primary"] == b"USER-P",
                  str(world.sel))
            world.sel["clip"] = b"USER-NEW-COPY"        # the user copies something
            typer.type_text("second take")
            typer.wait_idle()
            time.sleep(da_insert.RESTORE_AFTER_S + 0.4)
            check(f"hung {hang} staging: the next take pastes its own text",
                  world.pastes == [b"second take "], str(world.pastes))
            check(f"hung {hang} staging: and restores the NEW copy, not a stale one",
                  world.sel["clip"] == b"USER-NEW-COPY", repr(world.sel["clip"]))
        finally:
            undo()


# ---------------------------------------------------------------------------
# #18 (C3)  A portal error in the middle of a chord: whatever went down is
#     released and the session closed; ydotool replays the chord only when
#     no paste can have happened, never after the V-down went out.
# ---------------------------------------------------------------------------

def _portal_world(fail_at, release_ok=False):
    """A ready PortalKeyboard whose NotifyKeyboardKeysym fails from call
    number `fail_at` on (0-based; None = never), and the ydotool calls the
    typer makes behind it."""
    portal = da_insert.PortalKeyboard()
    portal._state, portal._session, portal._bus = "ready", "/s", object()
    log = {"keys": [], "ydotool": [], "closed": 0, "calls": 0}

    def call(method, params, *_a, **_k):
        if method == "Close":
            log["closed"] += 1
            return None
        n = log["calls"]
        log["calls"] += 1
        keysym, state = params.unpack()[2:]
        if fail_at is not None and n >= fail_at and not (release_ok and n > fail_at
                                                         and state == 0):
            raise GLib.Error("Timeout was reached")
        log["keys"].append((keysym, state))
        return None

    portal._call = call

    def run(args, **_kw):
        log["ydotool"].append(list(args))
        return _Completed()

    return portal, log, run


def review18_portal_error_mid_chord():
    print("\n[#18 C3] a portal error mid-chord neither double-pastes nor sticks Ctrl")
    KS_CTRL, KS_V = 0xffe3, 0x76
    saved = (da_insert.subprocess.run, da_insert.shutil.which, da_insert.portal_keyboard)
    da_insert.shutil.which = lambda n: "/usr/bin/ydotool" if n == "ydotool" else None
    try:
        # Ctrl down, V down, V up went out; Ctrl up failed.
        portal, log, run = _portal_world(fail_at=3)
        da_insert.portal_keyboard = lambda: portal
        da_insert.subprocess.run = run
        ok, transport = da_insert.TextTyper("clipboard")._send_chord("ctrl+v")
        check("V-down went out: the paste counts as done, through the portal",
              ok and transport == "portal", f"ok={ok} transport={transport}")
        check("and ydotool does NOT replay the chord (no second paste)",
              log["ydotool"] == [], str(log["ydotool"]))
        check("the session is closed and marked dead",
              log["closed"] == 1 and not portal.ready, f"closed={log['closed']}")

        # Ctrl down went out; V down failed: no paste happened, so ydotool
        # may press the chord — but Ctrl is released on the portal first.
        portal, log, run = _portal_world(fail_at=1, release_ok=True)
        da_insert.portal_keyboard = lambda: portal
        da_insert.subprocess.run = run
        ok, transport = da_insert.TextTyper("clipboard")._send_chord("ctrl+v")
        attempts = log["keys"][1:]
        check("only Ctrl went down: the portal releases it",
              (KS_CTRL, 0) in attempts, str(attempts))
        check("and V is never released without having been pressed",
              (KS_V, 0) not in attempts, str(attempts))
        check("then ydotool presses the chord once",
              ok and transport == "ydotool" and len(log["ydotool"]) == 1,
              f"ok={ok} transport={transport} ydotool={len(log['ydotool'])}")

        # Nothing went out at all: plain fallback, as before.
        portal, log, run = _portal_world(fail_at=0)
        da_insert.portal_keyboard = lambda: portal
        da_insert.subprocess.run = run
        ok, transport = da_insert.TextTyper("clipboard")._send_chord("ctrl+v")
        check("a failure on the first key falls back to ydotool",
              ok and transport == "ydotool" and len(log["ydotool"]) == 1,
              f"ok={ok} transport={transport}")

        # And the clean path is untouched.
        portal, log, run = _portal_world(fail_at=None)
        da_insert.portal_keyboard = lambda: portal
        da_insert.subprocess.run = run
        ok, transport = da_insert.TextTyper("clipboard")._send_chord("ctrl+v")
        check("a clean portal chord: four keysyms, no ydotool",
              ok and transport == "portal" and len(log["keys"]) == 4
              and log["ydotool"] == [] and portal.ready, str(log))
    finally:
        (da_insert.subprocess.run, da_insert.shutil.which,
         da_insert.portal_keyboard) = saved


# ---------------------------------------------------------------------------
# #19 (C4)  A binding pynput cannot parse ("Meta+Z" in hotkey_toggle) used to
#     raise ValueError out of keyboard.GlobalHotKeys and kill the app at every
#     launch.  The manager drops it and says so; enforce() resets it.
# ---------------------------------------------------------------------------

def review19_bad_pynput_binding():
    print("\n[#19 C4] a binding pynput rejects neither crashes nor survives a load")
    from pynput import keyboard
    from parakeet_dictation import hotkeys as da_hotkeys
    da_hotkeys.DIAG = TEST_DIAG
    built = []

    class StrictGlobalHotKeys(FakeGlobalHotKeys):
        def __init__(self, bindings):
            super().__init__(bindings)
            for b in bindings:
                keyboard.HotKey.parse(b)          # raises like the real one
            built.append(dict(bindings))

    saved = keyboard.GlobalHotKeys
    keyboard.GlobalHotKeys = StrictGlobalHotKeys
    try:
        cfg = da_config.AppConfig(hotkey_mode="toggle")
        cfg.hotkey_toggle = "Meta+Z"               # what enforce() would refuse
        mgr = da_hotkeys.HotkeyManager(cfg, on_toggle=lambda: None, on_start=lambda: None,
                                       on_stop=lambda: None, on_pause=lambda: None)
        try:
            status, err = mgr.start(), None
        except Exception as e:          # noqa: BLE001 — that IS the bug
            status, err = None, e
        check("start() does not raise on an unparseable binding", err is None, repr(err))
        check("it returns a status naming the binding",
              bool(status) and "Meta+Z" in status, repr(status))
        check("the valid bindings are still registered",
              bool(built) and list(built[-1]) == [cfg.hotkey_pause], str(built))
        mgr.stop()
    finally:
        keyboard.GlobalHotKeys = saved

    events_seen = []
    cfg = da_config.AppConfig(hotkey_toggle="Meta+Z", hotkey_start="<ctrl>+",
                              hotkey_stop="<ctrl>+8", hotkey_pause="")
    with redirect_stderr(io.StringIO()):
        forced = cfg.enforce(lambda ev, **kw: events_seen.append((ev, kw)))
    defaults = da_config.AppConfig()
    check("enforce() resets an unparseable hotkey_toggle to its default",
          cfg.hotkey_toggle == defaults.hotkey_toggle and "hotkey_toggle" in forced,
          f"{cfg.hotkey_toggle!r} {forced}")
    check("and hotkey_start", cfg.hotkey_start == defaults.hotkey_start, cfg.hotkey_start)
    check("a valid binding and an empty (disabled) pause are kept",
          cfg.hotkey_stop == "<ctrl>+8" and cfg.hotkey_pause == "",
          f"{cfg.hotkey_stop!r} {cfg.hotkey_pause!r}")
    check("each reset is logged with the key it changed",
          {kw.get("key") for ev, kw in events_seen if ev == "config_forced"}
          >= {"hotkey_toggle", "hotkey_start"}, str(events_seen))


# ---------------------------------------------------------------------------
# #20 (C5)  paste_min_delay_ms applies to every stop, not only to a hotkey
#     release: a toggle / SIGUSR1 / tray stop is a key chord too.
# ---------------------------------------------------------------------------

class NotBeforeTyper(da_insert.TextTyper):
    """Records what type_text was asked to do instead of doing it."""

    def __init__(self):
        super().__init__("clipboard")
        self.calls = []

    def type_text(self, text, target=None, on_done=None, not_before=None):
        self.calls.append((text, not_before, time.monotonic()))
        if on_done is not None:
            on_done()


def review20_stop_honours_paste_min_delay():
    print("\n[#20 C5] an explicit stop holds the chord back like a release does")
    ctl, eng, _ov = make_controller(hotkey_mode="toggle", paste_min_delay_ms=350)
    typer = NotBeforeTyper()
    ctl._typer = typer
    ctl.toggle()
    capture_up(ctl, eng)
    ctl._on_final_text("toggled take")
    t_stop = time.monotonic()
    ctl.toggle()                                   # the stop
    pump_until(lambda: ctl.gesture == "idle", 4000)
    check("the take was inserted once", len(typer.calls) == 1, str(typer.calls))
    if typer.calls:
        _text, not_before, _t = typer.calls[0]
        check("with a not_before ~350 ms after the stop",
              not_before is not None and 0.3 <= not_before - t_stop <= 0.45,
              f"not_before={'None' if not_before is None else f'{(not_before - t_stop) * 1000:.0f} ms'}")
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #21 (C6)  kglobalaccel restarting (a Plasma crash, kquitapp) drops every
#     registration: push-to-talk is re-registered when the name comes back.
# ---------------------------------------------------------------------------

def review21_kglobalaccel_restart_reregisters():
    print("\n[#21 C6] push-to-talk survives a kglobalaccel restart")
    from types import SimpleNamespace
    from parakeet_dictation import hotkeys as da_hotkeys
    da_hotkeys.DIAG = TEST_DIAG
    bus, names = FakeGioBus(), FakeNameWatch()
    saved_gio, saved_pynput = da_hotkeys.Gio, sys.modules.get("pynput")
    da_hotkeys.Gio = fake_hotkeys_gio(bus, names)
    sys.modules["pynput"] = SimpleNamespace(keyboard=SimpleNamespace(
        GlobalHotKeys=FakeGlobalHotKeys, HotKey=SimpleNamespace(parse=lambda b: [b])))
    presses = []
    try:
        mgr = da_hotkeys.HotkeyManager(
            da_config.AppConfig(hotkey_mode="hold"), on_toggle=lambda: None,
            on_start=lambda: None, on_stop=lambda: None, on_pause=lambda: None,
            on_hold_press=lambda: presses.append(1))
        mgr.start()
        svc = da_hotkeys.KGlobalAccelHotkey.SERVICE
        check("the manager watches the kglobalaccel bus name",
              any(n == svc for n, _a, _v in names.watches.values()), str(names.watches))
        check("the initial appearance does not register a second time",
              bus.calls.count("doRegister") == 1, f"doRegister x{bus.calls.count('doRegister')}")
        before = len(diag_lines())
        names.vanish(svc)
        names.appear(svc)
        check("kglobalaccel coming back registers the action again",
              bus.calls.count("doRegister") == 2, f"doRegister x{bus.calls.count('doRegister')}")
        check("with exactly one press/release subscription pair", len(bus.subs) == 2,
              f"{len(bus.subs)} subscriptions")
        check("and says so in the log",
              bool(events(diag_lines()[before:], "hold_hotkey_reregistered")))
        bus.emit("globalShortcutPressed", da_hotkeys.KGlobalAccelHotkey.COMPONENT,
                 "push-to-talk")
        check("a press after the restart fires once", presses == [1], str(presses))
        mgr.stop()
        check("stop() drops the watch and every subscription",
              not names.watches and not bus.subs, f"{names.watches} {bus.subs}")
    finally:
        da_hotkeys.Gio = saved_gio
        if saved_pynput is None:
            sys.modules.pop("pynput", None)
        else:
            sys.modules["pynput"] = saved_pynput


# ---------------------------------------------------------------------------
# #22 (C7)  Quitting (tray, SIGINT, SIGTERM from systemd or logout) within
#     half a second of a paste used to take the pending clipboard restore
#     down with the process: the dictated text stayed on the clipboard.
# ---------------------------------------------------------------------------

def review22_shutdown_flushes_clipboard_restore():
    print("\n[#22 C7] shutdown runs a pending clipboard restore; SIGTERM shuts down")
    world = ClipboardWorld()
    undo = world.install()
    try:
        typer = da_insert.TextTyper("clipboard")
        flush = getattr(typer, "flush_restore", None)
        check("TextTyper has flush_restore()", callable(flush))
        typer.type_text("dictated")
        typer.wait_idle()
        check("right after the paste the clipboard holds the dictated text",
              world.sel["clip"] == b"dictated ", repr(world.sel["clip"]))
        if callable(flush):
            t0 = time.monotonic()
            flush()
            dt = time.monotonic() - t0
            check("flush_restore() puts the user's selections back before returning",
                  world.sel["clip"] == b"USER-OLD" and world.sel["primary"] == b"USER-P",
                  str(world.sel))
            check("waiting at most out the paste's restore delay",
                  dt <= da_insert.RESTORE_AFTER_S + 0.2, f"{dt * 1000:.0f} ms")
            check("and nothing is left pending", not typer._clip_restore_pending)
            n = len(world.argv)
            time.sleep(da_insert.RESTORE_AFTER_S + 0.3)
            check("the timer it replaced does not restore a second time",
                  len(world.argv) == n, str(world.argv[n:]))
            flush()
            check("a flush with nothing pending touches nothing", len(world.argv) == n)
    finally:
        undo()

    flushed = []

    class FlushTyper(NotBeforeTyper):
        def flush_restore(self):
            flushed.append(1)

    ctl, eng, _ov = make_controller()
    ctl._typer = FlushTyper()
    ctl.shutdown()
    check("controller.shutdown() flushes the typer's restore", flushed == [1], str(flushed))

    src = (HERE.parent / "parakeet_dictation" / "app.py").read_text()
    check("app.py handles SIGTERM on the main loop",
          "signal.SIGTERM" in src and "unix_signal_add" in src)


# ---------------------------------------------------------------------------
# #23 (C8)  Privacy: dictated text never sits in a process argv (any user can
#     read /proc/*/cmdline), the restore token is never world-readable, the
#     log does not record every window switch, and the log is 0600 in 0700.
# ---------------------------------------------------------------------------

def review23_privacy():
    print("\n[#23 C8] no text in argv, private token and log, no focus trail")
    import os
    import stat
    secret = "my secret dictation"
    world = ClipboardWorld()
    undo = world.install()
    try:
        typer = da_insert.TextTyper("clipboard")
        typer.type_text(secret)
        typer.wait_idle()
        typer.flush_restore()
        in_argv = [a for a in world.argv if any(secret in str(x) for x in a)]
        check("clipboard: the text is in no helper's argv", in_argv == [], str(in_argv))
        check("it reaches wl-copy on stdin, on both selections",
              world.inputs.count((secret + " ").encode()) == 2, str(world.inputs))
        check("and the paste carried it", world.pastes == [(secret + " ").encode()],
              str(world.pastes))

        world.argv.clear()
        seen = []
        real_run = world.run

        def run(args, **kw):
            if args[0] == "wtype":
                seen.append((list(args), kw.get("input")))
            return real_run(args, **kw)

        da_insert.subprocess.run = run
        typer = da_insert.TextTyper("wtype")
        typer.type_text(secret)
        typer.wait_idle()
        check("wtype: the text is in no argv",
              seen and all(secret not in " ".join(a) for a, _i in seen), str(seen))
        check("wtype reads it from stdin (`wtype -`)",
              seen and seen[0][0] == ["wtype", "-"]
              and seen[0][1] == (secret + " ").encode(), str(seen))
    finally:
        undo()

    token = da_insert.PortalKeyboard._token_path()
    token.unlink(missing_ok=True)
    opened = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *a, **k):
        if str(path).startswith(str(token)):
            opened.append(mode)
        return real_open(path, flags, mode, *a, **k)

    os.open = spy_open
    try:
        da_insert.PortalKeyboard.save_token("tok-1")
    finally:
        os.open = real_open
    check("the restore token is created 0600 from the start (never 0644)",
          opened and all(m == 0o600 for m in opened), str([oct(m) for m in opened]))
    check("and is 0600 on disk", stat.S_IMODE(token.stat().st_mode) == 0o600,
          oct(stat.S_IMODE(token.stat().st_mode)))
    token.chmod(0o644)
    da_insert.PortalKeyboard.save_token("tok-2")
    check("rewriting a pre-existing 0644 token leaves it 0600",
          stat.S_IMODE(token.stat().st_mode) == 0o600
          and da_insert.PortalKeyboard.load_token() == "tok-2")
    token.unlink(missing_ok=True)

    from parakeet_dictation import focus as da_focus
    da_focus.DIAG = TEST_DIAG
    tracker = da_focus.FocusTracker(enabled=False)

    class Invocation:
        def return_value(self, _v):
            pass

    before = len(diag_lines())
    for cls in ("kitty", "org.kde.kwrite", "org.telegram.desktop"):
        tracker._on_report(None, None, None, None, None,
                           GLib.Variant("(ssss)", (cls, cls, "caption", "1")), Invocation())
    check("window switches are not logged",
          not events(diag_lines()[before:], "focus"), str(diag_lines()[before:]))
    check("the snapshot still follows them",
          tracker.snapshot() is None or tracker._latest.resource_class == "org.telegram.desktop")

    d = Path(tempfile.mkdtemp(prefix="parakeet-diag-")) / "sub"
    log = da_diagnostics.DiagnosticLog(d / "diagnostics.log")
    old_umask = os.umask(0o022)
    try:
        log.log("probe", n=1)
    finally:
        os.umask(old_umask)
    check("the diagnostics dir is created 0700",
          d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700,
          oct(stat.S_IMODE(d.stat().st_mode)) if d.exists() else "missing")
    f = d / "diagnostics.log"
    check("the diagnostics log is created 0600",
          f.exists() and stat.S_IMODE(f.stat().st_mode) == 0o600,
          oct(stat.S_IMODE(f.stat().st_mode)) if f.exists() else "missing")
    small = da_diagnostics.DiagnosticLog(d / "small.log", max_bytes=200)
    for i in range(20):
        small.log("probe", n=i)
    f2 = d / "small.log"
    check("and stays 0600 after a trim rewrites it",
          stat.S_IMODE(f2.stat().st_mode) == 0o600, oct(stat.S_IMODE(f2.stat().st_mode)))


# ---------------------------------------------------------------------------
# #24 (C9)  Text that arrives after its take is over (an abandoned drain's
#     late decode) must not ride along with the next take's paste.
# ---------------------------------------------------------------------------

def review24_late_text_is_not_pasted_into_the_next_take():
    print("\n[#24 C9] a late text from a finished take is dropped")
    ctl, eng, _ov = make_controller()
    typer = NotBeforeTyper()
    ctl._typer = typer
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl._on_final_text("first take")
    ctl.hold_release()
    pump_until(lambda: ctl.gesture == "idle", 4000)
    before = len(diag_lines())
    ctl._on_final_text("late from take one")          # after the take ended
    ctl._on_commit_partial("late partial")
    check("the late text is logged as dropped",
          bool(events(diag_lines()[before:], "late_text_dropped")))
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl._on_final_text("second take")
    ctl.hold_release()
    pump_until(lambda: ctl.gesture == "idle", 4000)
    texts = [t for t, _nb, _t in typer.calls]
    check("each take pasted only its own text",
          texts == ["first take", "second take"], str(texts))
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #25 (E1)  A microphone error mid-take (USB mic unplugged, PipeWire
#     restarted) still decodes what was captured before it, in both paths.
# ---------------------------------------------------------------------------

class FailingStream(CountedStream):
    """N good reads, then the host error an unplugged device produces."""

    def read(self, n):
        self._left -= 1
        if self._left < 0:
            raise da_engine.sd.PortAudioError("Unanticipated host error")
        return np.zeros((n, 1), dtype=np.float32), False


class ScriptedVad:
    """Closes one 1 s phrase on the third block and holds a second one open
    until the end-of-take flush."""

    def __init__(self):
        self.ready, self.n, self.open = [], 0, True

    def accept_waveform(self, _x):
        self.n += 1
        if self.n == 3:
            self.ready.append(da_audio._SpeechSegment(np.ones(SR, np.float32), None))

    def is_speech_detected(self):
        return True

    def empty(self):
        return not self.ready

    @property
    def front(self):
        return self.ready[0]

    def pop(self):
        self.ready.pop(0)

    def flush(self):
        if self.open:
            self.open = False
            self.ready.append(da_audio._SpeechSegment(np.ones(SR, np.float32), None))
            return True
        return False

    def open_tail(self, _n):
        return np.zeros(0, np.float32)


def review25_mic_error_keeps_captured_speech():
    print("\n[#25 E1] a mic error mid-take still decodes what was captured")
    texts, errors = [], []
    config = da_config.AppConfig(preview=False)
    engine = da_engine.ASREngine(config, {"streaming": False, "files": {}},
                                 on_text=texts.append, on_partial=lambda _t: None,
                                 on_error=errors.append)
    engine._acquire_offline_recognizer = lambda: (None, 0.0)
    engine._decode_segment = lambda _r, smp, **_k: (f"words{len(smp)}", 1.0)
    engine._build_vad = ScriptedVad
    da_engine.sd.InputStream = lambda **_kw: FailingStream(5, threading.Event())
    engine.start()
    engine._thread.join(10)
    check("the error is still reported", errors == ["Unanticipated host error"], str(errors))
    check("and both phrases captured before it were decoded and delivered",
          sum(int(t[5:]) for t in texts) == 2 * SR, str(texts))

    words = ["one", "two", "three", "four", "five", "six"]
    texts, commits = [], []
    engine = da_engine.ASREngine(
        da_config.AppConfig(partial_overwrite=False), {"streaming": True, "files": {}},
        on_text=texts.append, on_partial=lambda _t: None, on_error=lambda _e: None,
        on_partial_type=lambda _t: None, on_commit_partial=commits.append)
    rec = FakeOnlineRecognizer(words)
    engine._acquire_online_recognizer = lambda: (rec, 0.0)
    da_engine.sd.InputStream = lambda **_kw: FailingStream(5, threading.Event())
    raised = None
    try:
        engine._run_streaming(threading.Event())
    except da_engine.sd.PortAudioError as e:
        raised = e
    check("streaming: the error still propagates to _run", raised is not None)
    check("streaming: the tail before the error is committed",
          texts == ["one two three four five six"], str(texts))

    # The controller end: in end-of-take mode that text is held back, and
    # the error used to discard it.  The key may still be down, so no chord
    # can go out — the text is put on the clipboard and the pill says so.
    copied = []

    class CopyTyper(NotBeforeTyper):
        def copy_only(self, text):
            copied.append(text)

    ctl, eng, ov = make_controller()
    ctl._typer = CopyTyper()
    ctl.hold_press()
    capture_up(ctl, eng)
    ctl._on_final_text("words before")
    ctl._on_final_text("the unplug")
    with redirect_stderr(io.StringIO()):
        ctl._on_error("Unanticipated host error")
    check("controller: the held-back text goes to the clipboard, nothing is pasted",
          copied == ["words before the unplug"] and ctl._typer.calls == [],
          f"copied={copied} pasted={ctl._typer.calls}")
    check("controller: the pill tells the user where the text is",
          ov.states and ov.states[-1][0] == "error" and "clipboard" in ov.states[-1][1].lower(),
          str(ov.states[-1:]))
    check("controller: the take still ends", ctl.gesture == "idle", ctl.gesture)
    ctl.shutdown()


# ---------------------------------------------------------------------------
# #26 (E2)  audio_device is matched by NAME first: PortAudio indexes shift
#     whenever a device comes or goes, so a stored index picks a different
#     microphone after the next replug.  A stale value falls back to the
#     default and says so (without the device name — the log never has one).
# ---------------------------------------------------------------------------

def review26_audio_device_by_name():
    print("\n[#26 E2] the microphone is resolved by name, index only as a fallback")
    devices = [{"index": 0, "name": "HDA Intel PCH: ALC257 Analog (hw:0,0)", "channels": 2},
               {"index": 3, "name": "Blue Yeti: USB Audio (hw:2,0)", "channels": 2},
               {"index": 4, "name": "pipewire", "channels": 64},
               {"index": 5, "name": "3", "channels": 1}]
    saved = (da_audio.list_input_devices, getattr(da_audio, "DIAG", None))
    da_audio.list_input_devices = lambda: devices
    da_audio.DIAG = TEST_DIAG
    try:
        r = da_audio.resolve_audio_device
        check("empty -> system default", r("") is None)
        check("an exact name wins", r("Blue Yeti: USB Audio (hw:2,0)") == 3)
        check("a substring of a name matches (hw suffix changed after a replug)",
              r("Blue Yeti") == 3)
        check("a name that is all digits is matched as a name before an index",
              r("3") == 5, str(r("3")))
        check("a legacy index still works when no name matches", r("4") == 4)
        before = len(diag_lines())
        check("a name that matches nothing falls back to the default",
              r("Rode NT-USB") is None)
        lines = events(diag_lines()[before:], "device_missing")
        check("and logs device_missing", bool(lines), str(diag_lines()[before:]))
        check("without the device name", lines and "Rode" not in lines[0], str(lines))
        check("a stale index (no such input device) falls back to the default",
              r("17") is None)
    finally:
        da_audio.list_input_devices = saved[0]
        if saved[1] is not None:
            da_audio.DIAG = saved[1]

# ---------------------------------------------------------------------------

SECTIONS = {1: review1_vad_speech_floor,
            2: review2_take_ends_even_if_the_typer_raises,
            3: review3_apply_config_preloads_on_model_switch,
            4: review4_hotkey_rebuild_unsubscribes,
            5: review5_recognizer_cache_keyed_on_language,
            6: review6_streaming_flushes_tail_on_stop,
            7: review7_press_while_stopping_is_queued,
            8: review8_cli_download_is_atomic,
            9: review9_preview_keeps_going_beyond_the_window,
            10: review10_coalescer_disabled_counts_closed_blocks,
            11: review11_test_defects_runs_main_on_a_scratch_config,
            12: review12_tests_locate_the_checkout_from_file,
            13: review13_focus_snapshot_is_cache_only,
            14: review14_insertion_off_thread_and_batched_backspaces,
            15: review15_cleanups_single_source_tables,
            16: review16_press_cancels_deferred_stop,
            17: review17_wl_copy_timeout_is_reported,
            18: review18_portal_error_mid_chord,
            19: review19_bad_pynput_binding,
            20: review20_stop_honours_paste_min_delay,
            21: review21_kglobalaccel_restart_reregisters,
            22: review22_shutdown_flushes_clipboard_restore,
            23: review23_privacy,
            24: review24_late_text_is_not_pasted_into_the_next_take,
            25: review25_mic_error_keeps_captured_speech,
            26: review26_audio_device_by_name}


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
