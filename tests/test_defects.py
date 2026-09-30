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
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
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
# #4  Single instance: the second copy must see the bus name taken and exit.
# ---------------------------------------------------------------------------

def _private_bus():
    """A second connection to the session bus — what another process has."""
    from gi.repository import Gio
    addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    return Gio.DBusConnection.new_for_address_sync(
        addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


def _request_name(bus, name) -> int:
    """RequestName through the bus driver, independent of the package."""
    from gi.repository import Gio, GLib
    return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "RequestName",
                         GLib.Variant("(su)", (name, 4)), GLib.VariantType.new("(u)"),
                         Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]


def defect4_single_instance():
    print("\n[#4] single-instance lock on the session bus")
    REAL_NAME = "org.kde.parakeet.Dictation"

    # --- main() with the name already owned by "another process" ---------
    from parakeet_dictation import app as da_app
    da_app.DIAG = TEST_DIAG
    try:
        from parakeet_dictation import instance as da_instance
        da_instance.DIAG = TEST_DIAG
    except ImportError:
        da_instance = None
    other = _private_bus()
    reply = _request_name(other, REAL_NAME)
    check("the real name can be held for this check (no fixed app running)",
          reply == 1, f"RequestName reply {reply}")
    if reply == 1:
        sentinel = da_app.DictationController

        def must_not_build(*_a, **_k):
            raise AssertionError("main() built the controller with the name taken")

        da_app.DictationController = must_not_build
        out = io.StringIO()
        before = len(diag_lines())
        try:
            with redirect_stdout(out):
                rc = da_app.main()
        except AssertionError as e:
            rc = repr(e)
        finally:
            da_app.DictationController = sentinel
        check("main() returns 0 without building the app", rc == 0, str(rc))
        check("and says so in one line, naming the running pid",
              "already running" in out.getvalue()
              and f"pid {os.getpid()}" in out.getvalue()
              and out.getvalue().count("\n") == 1, out.getvalue().strip())
        check("already_running logged",
              any("event=already_running" in l for l in diag_lines()[before:]))
    other.close_sync(None)

    # --- the helper itself, with a fake owner --------------------------------
    check("the instance module exists", da_instance is not None)
    if da_instance is None:
        return
    name = f"org.kde.parakeet.DictationTest{os.getpid()}"
    first = da_instance.InstanceLock(name)
    check("a free name is acquired", first.acquire() is True)
    other = _private_bus()
    before = len(diag_lines())
    second = da_instance.InstanceLock(name, bus=other)
    check("a second instance is refused", second.acquire() is False)
    check("it learns the owner's pid", second.owner_pid == os.getpid(),
          str(second.owner_pid))
    check("already_running logged with the owner's pid",
          any("event=already_running" in l and f"owner_pid={os.getpid()}" in l
              for l in diag_lines()[before:]))
    first.release()
    check("once the owner lets go, the next instance gets the name",
          second.acquire() is True)
    second.release()
    other.close_sync(None)

    # --- a second PROCESS, the real thing ------------------------------------
    holder = da_instance.InstanceLock(name)
    holder.acquire()
    probe = (
        "import sys, pathlib; sys.path.insert(0, %r)\n"
        "from parakeet_dictation import instance as m\n"
        "from parakeet_dictation.diagnostics import DiagnosticLog\n"
        "m.DIAG = DiagnosticLog(pathlib.Path(%r))\n"
        "lock = m.InstanceLock(%r)\n"
        "sys.exit(0 if lock.acquire() is False and lock.owner_pid == %d else 1)\n"
        % (str(HERE.parent), str(DIAG_PATH), name, os.getpid()))
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, timeout=30)
    check("a second process sees the name as taken and knows who holds it",
          proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-200:])
    holder.release()

# ---------------------------------------------------------------------------
# #5  A missing helper binary is named as itself (wl-copy, ydotool, ...),
#     never as the typer method.
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, rc=0, out=b""):
        self.returncode, self.stdout, self.stderr = rc, out, b""


def _missing(exe, with_filename):
    """What subprocess raises for an absent executable.  The OS normally fills
    in `filename`; the other form is what a wrapped/other OSError looks like,
    and the handler must name the helper either way."""
    if with_filename:
        return FileNotFoundError(2, "No such file or directory", exe)
    return FileNotFoundError(2, "No such file or directory")


def defect5_missing_helper_named():
    print("\n[#5] a missing helper is named, not the typer method")
    from parakeet_dictation import insert as da_insert
    da_insert.DIAG = TEST_DIAG
    saved = (da_insert.subprocess.run, da_insert.shutil.which, da_insert.portal_keyboard)
    da_insert.portal_keyboard = lambda: None
    try:
        for with_filename in (True, False):
            tag = "OS names the file" if with_filename else "errno only"
            # --- wl-copy absent, typer method "clipboard" ----------------------
            def run(args, **_kw):
                if args[0] == "wl-copy":
                    raise _missing("wl-copy", with_filename)
                if args[0] == "wl-paste":
                    return _Completed(0, b"")
                return _Completed()
            da_insert.subprocess.run = run
            da_insert.shutil.which = lambda n: f"/usr/bin/{n}" if n == "ydotool" else None
            failures = []
            typer = da_insert.TextTyper("clipboard", on_failure=failures.append)
            before = len(diag_lines())
            err = io.StringIO()
            with redirect_stderr(err):
                typer.type_text("hello")
            lines = diag_lines()[before:]
            check(f"[{tag}] the log names wl-copy",
                  any("event=helper_missing" in l and "exe=wl-copy" in l for l in lines),
                  str(lines[-1:]))
            check(f"[{tag}] the user-facing error names wl-copy",
                  bool(failures) and "wl-copy" in failures[-1], str(failures))
            check(f"[{tag}] stderr names wl-copy, not 'clipboard'",
                  "wl-copy" in err.getvalue() and "clipboard not found" not in err.getvalue(),
                  err.getvalue().strip()[:80])

            # --- ydotool absent at exec time (which() said it was there) -------
            def run2(args, **_kw):
                if args[0] == "ydotool":
                    raise _missing("ydotool", with_filename)
                if args[0] == "wl-paste":
                    return _Completed(0, b"")
                return _Completed()
            da_insert.subprocess.run = run2
            typer = da_insert.TextTyper("clipboard", on_failure=failures.append)
            before = len(diag_lines())
            with redirect_stderr(io.StringIO()):
                typer.type_text("hello")
            lines = diag_lines()[before:]
            check(f"[{tag}] a vanished ydotool is named in the log",
                  any("event=helper_missing" in l and "exe=ydotool" in l for l in lines),
                  str([l.split("event=")[1][:60] for l in lines]))

            # --- the backspace path blamed the method too ----------------------
            before = len(diag_lines())
            err = io.StringIO()
            with redirect_stderr(err):
                typer._send_backspaces(2)
            lines = diag_lines()[before:]
            check(f"[{tag}] backspace helper: log names ydotool",
                  any("event=helper_missing" in l and "exe=ydotool" in l for l in lines))
            check(f"[{tag}] backspace helper: stderr names ydotool, not 'for clipboard'",
                  "ydotool" in err.getvalue() and "for clipboard" not in err.getvalue(),
                  err.getvalue().strip()[:80])

        # --- wtype method, wtype absent: still named as itself -----------------
        def run3(args, **_kw):
            if args[0] == "wtype":
                raise _missing("wtype", True)
            return _Completed()
        da_insert.subprocess.run = run3
        failures = []
        typer = da_insert.TextTyper("wtype", on_failure=failures.append)
        before = len(diag_lines())
        with redirect_stderr(io.StringIO()):
            typer.type_text("hello")
        check("wtype absent: named in log and error",
              any("exe=wtype" in l for l in diag_lines()[before:])
              and bool(failures) and "wtype" in failures[-1], str(failures))
    finally:
        (da_insert.subprocess.run, da_insert.shutil.which,
         da_insert.portal_keyboard) = saved


# ---------------------------------------------------------------------------
# #6  Clipboard restore is byte-verbatim: trailing newline, non-UTF-8 text
#     and a large clipboard all come back exactly as they were.
# ---------------------------------------------------------------------------

class WlClipboardFake:
    """wl-copy / wl-paste / ydotool as they behave on this desktop.

    Measured against wl-clipboard 2.3.0 on 2026-09-30: wl-paste appends one
    '\\n' to text unless -n/--no-newline is given, and reads verbatim with
    it; wl-copy stores argv text or stdin bytes verbatim; an argv element
    over 128 KiB fails at exec with E2BIG (OSError errno 7,
    "Argument list too long"), which the arg form of a large restore hits.
    """
    ARG_MAX_ELEMENT = 131072

    def __init__(self):
        self.clip = None
        self.primary = None
        self.pastes = []          # clipboard content at each chord press

    def _sel(self, args):
        return "primary" if ("--primary" in args or "-p" in args) else "clip"

    def run(self, args, **kw):
        args = list(args)
        if args[0] == "wl-copy":
            sel = self._sel(args)
            if "--clear" in args:
                setattr(self, sel, None)
                return _Completed()
            if "--" in args:
                text = args[args.index("--") + 1:]
                for a in text:
                    if len(a.encode("utf-8", "surrogateescape")) > self.ARG_MAX_ELEMENT:
                        raise OSError(7, "Argument list too long", "wl-copy")
                data = " ".join(text).encode("utf-8", "surrogateescape")
            else:
                data = bytes(kw.get("input") or b"")
            setattr(self, sel, data)
            return _Completed()
        if args[0] == "wl-paste":
            data = getattr(self, self._sel(args))
            if data is None:
                return _Completed(1, b"")
            if not ("-n" in args or "--no-newline" in args):
                data = data + b"\n"
            return _Completed(0, data)
        if args[0] == "ydotool":
            self.pastes.append(self.clip)
        return _Completed()


def defect6_clipboard_verbatim():
    print("\n[#6] clipboard restore round-trips the user's bytes verbatim")
    from parakeet_dictation import insert as da_insert
    da_insert.DIAG = TEST_DIAG
    saved = (da_insert.subprocess.run, da_insert.shutil.which, da_insert.portal_keyboard)
    fake = WlClipboardFake()
    da_insert.subprocess.run = fake.run
    da_insert.shutil.which = lambda n: f"/usr/bin/{n}" if n == "ydotool" else None
    da_insert.portal_keyboard = lambda: None
    try:
        cases = [
            ("trailing newline", b"line one\nline two\n"),
            ("two trailing newlines", b"abc\n\n"),
            ("just a newline", b"\n"),
            ("non-UTF-8 text (latin-1)", b"caf\xe9 au lait\n"),
            ("300 KB of text", b"x" * 300000 + b"\n"),
        ]
        for label, payload in cases:
            fake.clip, fake.primary = payload, payload + b"P"
            fake.pastes.clear()
            typer = da_insert.TextTyper("clipboard", keep_on_clipboard=False)
            typer.type_text("dictated")
            check(f"{label}: the paste carried the dictated text",
                  fake.pastes == [b"dictated "], str(fake.pastes)[:60])
            time.sleep(da_insert.RESTORE_AFTER_S + 0.4)
            check(f"{label}: clipboard restored verbatim", fake.clip == payload,
                  f"got {fake.clip[:24]!r}..." if fake.clip else repr(fake.clip))
            check(f"{label}: primary restored verbatim", fake.primary == payload + b"P",
                  f"got {fake.primary[:24]!r}..." if fake.primary else repr(fake.primary))

        fake.clip, fake.primary = None, None
        typer = da_insert.TextTyper("clipboard", keep_on_clipboard=False)
        typer.type_text("dictated")
        time.sleep(da_insert.RESTORE_AFTER_S + 0.4)
        check("an empty clipboard is left empty, not holding the dictated text",
              fake.clip is None and fake.primary is None,
              f"{fake.clip!r} / {fake.primary!r}")
    finally:
        (da_insert.subprocess.run, da_insert.shutil.which,
         da_insert.portal_keyboard) = saved


# ---------------------------------------------------------------------------
# #7  The non-layer-shell overlay fallback: undecorated as far as GTK can ask,
#     and honest about the fact that a Wayland toplevel cannot be positioned.
# ---------------------------------------------------------------------------

def defect7_overlay_fallback_honest():
    print("\n[#7] overlay fallback window: undecorated, and honest about placement")
    from parakeet_dictation.ui import overlay as da_overlay   # pins Gtk/Gdk 3.0
    import gi
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GtkLayerShell
    da_overlay.DIAG = TEST_DIAG
    backend = Gdk.Display.get_default().__gtype__.name
    wayland = backend != "GdkX11Display"
    orig = GtkLayerShell.is_supported
    GtkLayerShell.is_supported = lambda: False        # force the fallback
    try:
        before = len(diag_lines())
        ov = da_overlay.PillOverlay(da_config.AppConfig())
        line = next((l for l in diag_lines()[before:] if "event=overlay_backend" in l), "")
        check("without layer-shell the fallback window is used",
              ov.layered is False and "backend=fallback_window" in line, line)
        check("the log says whether the fallback is degraded", "degraded=" in line, line)
        degraded = getattr(ov, "degraded", None)
        check(f"the pill knows its state on this display ({backend})",
              degraded is wayland, f"degraded={degraded!r}")
        check("logged as degraded=1 on Wayland, degraded=0 on X11",
              ("degraded=1" in line) == wayland and ("degraded=0" in line) == (not wayland),
              line)
        check("the window asks for no decorations", ov._win.get_decorated() is False)
        check("the window carries the NOTIFICATION type hint",
              ov._win.get_type_hint() == Gdk.WindowTypeHint.NOTIFICATION)

        moves = []
        ov._win.move = lambda x, y: moves.append((x, y))
        ov.debug_force_state("error", "fallback check")
        if wayland:
            check("no move() is pretended on a Wayland display", moves == [], str(moves))
        else:
            check("move() is used on X11", len(moves) == 1, str(moves))
        # Where the backend does honour move(), the pill is placed.
        ov.degraded = False
        moves.clear()
        ov._show()
        check("move() is used where the backend honours it", len(moves) == 1, str(moves))
        ov.degraded = degraded

        before = len(diag_lines())
        ov.preview.append("the preview panel falls back the same way")
        ov.preview.show()
        pline = next((l for l in diag_lines()[before:] if "event=preview_backend" in l), "")
        check("the preview panel's fallback is logged with the same flag",
              "backend=fallback_window" in pline and "degraded=" in pline
              and ("degraded=1" in pline) == wayland, pline)
        ov.debug_release()
        ov.shutdown()
    finally:
        GtkLayerShell.is_supported = orig


# ---------------------------------------------------------------------------
# #8  The missing-model message points at a downloader that exists.
# ---------------------------------------------------------------------------

def defect8_model_hint():
    print("\n[#8] missing-model message names the real downloader")
    from parakeet_dictation import controller as da_controller
    profile = {"streaming": False, "files": {
        "encoder": {"filename": "no-such-encoder.onnx"},
        "tokens": {"filename": "no-such-tokens.txt"}}}
    engine = da_engine.ASREngine(da_config.AppConfig(model_profile="desktop"), profile,
                                 on_text=None, on_partial=None, on_error=None)
    try:
        engine._ensure_models()
        msg = ""
    except FileNotFoundError as e:
        msg = str(e)
    check("missing files are reported",
          "Missing model files" in msg and "no-such-encoder.onnx" in msg, msg[:80])
    check("the hint names the module that exists",
          "python -m parakeet_dictation.models desktop" in msg, msg.splitlines()[-1:])
    check("the deleted script is not mentioned", "download_models.py" not in msg)
    check("the pill still shows the Settings › Models hint",
          da_controller.overlay_error_message(msg)
          == "Model files missing — open Settings › Models",
          da_controller.overlay_error_message(msg))
    proc = subprocess.run([sys.executable, "-m", "parakeet_dictation.models",
                           "no-such-profile"], cwd=HERE.parent, capture_output=True,
                          text=True, timeout=60)
    check("`python -m parakeet_dictation.models <profile>` is runnable",
          proc.returncode == 1 and "Unknown profile" in proc.stdout,
          (proc.stdout + proc.stderr).strip()[-120:])


# ---------------------------------------------------------------------------
# #9  num_threads defaults to 4 (docs/measurements/2026-09-30-preview-cost.md:
#     8 threads made a 1 s preview cadence cost ~11 cores and the final
#     decode 7x slower; 4 costs ~3 cores with the final decode unaffected).
# ---------------------------------------------------------------------------

def defect9_thread_default():
    print("\n[#9] num_threads defaults to 4")
    cpus = os.cpu_count() or 4
    expected = min(cpus, 4)
    check(f"default is {expected} on this {cpus}-thread machine",
          da_config.AppConfig().num_threads == expected,
          str(da_config.AppConfig().num_threads))
    check("the default never exceeds 4",
          da_config.AppConfig.__dataclass_fields__["num_threads"].default <= 4)
    with tempfile.TemporaryDirectory(prefix="parakeet-cfg-") as tmp:
        saved = (da_config.CONFIG_DIR, da_config.CONFIG_FILE)
        da_config.CONFIG_DIR = Path(tmp)
        da_config.CONFIG_FILE = Path(tmp) / "config.json"
        try:
            da_config.CONFIG_FILE.write_text(json.dumps({"num_threads": 8}))
            check("an explicit num_threads in the file keeps its value",
                  da_config.AppConfig.load(log=lambda *a, **k: None).num_threads == 8)
        finally:
            da_config.CONFIG_DIR, da_config.CONFIG_FILE = saved


# ---------------------------------------------------------------------------
# #10 focus.py logs the window class when it changes, not on every activation.
# ---------------------------------------------------------------------------

def defect10_focus_log_on_change():
    print("\n[#10] focus logged on class change only")
    from gi.repository import GLib
    from parakeet_dictation import focus as da_focus
    da_focus.DIAG = TEST_DIAG
    tracker = da_focus.FocusTracker(enabled=True)

    class Invocation:
        def return_value(self, _v):
            pass

    def activate(cls, caption="doc"):
        tracker._on_report(None, None, None, None, None,
                           GLib.Variant("(ssss)", (cls, cls, caption, "4242")),
                           Invocation())

    before = len(diag_lines())
    activate("kitty", "a")
    activate("kitty", "b")            # alt-tab between two kitty windows
    activate("kitty", "c")
    activate("org.kde.kwrite")
    activate("org.kde.kwrite")
    activate("kitty")
    activate("")                      # desktop / no window
    activate("")
    logged = [l.split("cls=")[1].split()[0]
              for l in diag_lines()[before:] if "event=focus " in l]
    check("one focus line per class change",
          logged == ["kitty", "org.kde.kwrite", "kitty", "unknown"], str(logged))
    check("the cached snapshot still follows every activation",
          tracker._latest is not None and tracker._latest.resource_class == "")
    activate("kitty", "z")
    check("a caption change on the same class is tracked but not logged",
          tracker._latest.caption == "z"
          and sum(1 for l in diag_lines()[before:] if "event=focus " in l) == 5)


# ---------------------------------------------------------------------------
# #11 Defaults match the configuration the app is built and tested for, and
#     hold + per_segment — a broken state on KWin — cannot run.
# ---------------------------------------------------------------------------

def defect11_hold_mode_defaults_and_guard():
    print("\n[#11] defaults are the tested configuration; hold + per_segment cannot run")
    from parakeet_dictation import controller as da_controller
    da_controller.DIAG = TEST_DIAG
    cfg = da_config.AppConfig()
    check("default hotkey_mode is hold", cfg.hotkey_mode == "hold", cfg.hotkey_mode)
    check("default hotkey_hold is Meta+Z", cfg.hotkey_hold == "Meta+Z", cfg.hotkey_hold)
    check("default insert_mode is end_of_take", cfg.insert_mode == "end_of_take",
          cfg.insert_mode)

    with tempfile.TemporaryDirectory(prefix="parakeet-cfg-") as tmp:
        saved = (da_config.CONFIG_DIR, da_config.CONFIG_FILE)
        da_config.CONFIG_DIR = Path(tmp)
        da_config.CONFIG_FILE = Path(tmp) / "config.json"
        try:
            events = []

            def log(ev, **f):
                events.append((ev, f))

            da_config.CONFIG_FILE.write_text(json.dumps(
                {"hotkey_mode": "hold", "insert_mode": "per_segment"}))
            with redirect_stderr(io.StringIO()):
                cfg = da_config.AppConfig.load(log=log)
            check("hold + per_segment is forced to end_of_take on load",
                  cfg.insert_mode == "end_of_take", cfg.insert_mode)
            check("config_forced logged with key and reason",
                  any(ev == "config_forced" and f.get("key") == "insert_mode"
                      and f.get("reason") == "hold_mode" for ev, f in events), str(events))

            events.clear()
            da_config.CONFIG_FILE.write_text(json.dumps(
                {"hotkey_mode": "toggle", "insert_mode": "per_segment",
                 "hotkey_hold": "Meta+Alt+D"}))
            cfg = da_config.AppConfig.load(log=log)
            check("explicit values outside the forbidden combination are kept",
                  cfg.hotkey_mode == "toggle" and cfg.insert_mode == "per_segment"
                  and cfg.hotkey_hold == "Meta+Alt+D" and not events,
                  f"{cfg.hotkey_mode} {cfg.insert_mode} {cfg.hotkey_hold} {events}")

            # However the config object was made, the controller will not run it.
            before = len(diag_lines())
            with redirect_stderr(io.StringIO()):
                ctl = da_controller.DictationController(
                    da_config.AppConfig(hotkey_mode="hold", insert_mode="per_segment"))
            check("the controller forces end_of_take at construction",
                  ctl.config.insert_mode == "end_of_take" and ctl._insert_at_end)
            check("and logs config_forced",
                  any("event=config_forced" in l and "key=insert_mode" in l
                      and "reason=hold_mode" in l for l in diag_lines()[before:]))
            before = len(diag_lines())
            with redirect_stderr(io.StringIO()):
                ctl.apply_config(
                    da_config.AppConfig(hotkey_mode="hold", insert_mode="per_segment"))
            on_disk = json.loads(da_config.CONFIG_FILE.read_text())
            check("apply_config forces it too and saves the corrected file",
                  ctl.config.insert_mode == "end_of_take"
                  and on_disk["insert_mode"] == "end_of_take", str(on_disk.get("insert_mode")))
            check("and logs it again",
                  any("event=config_forced" in l for l in diag_lines()[before:]))
            ctl.shutdown()
        finally:
            da_config.CONFIG_DIR, da_config.CONFIG_FILE = saved

    # The Settings dialog shows the forced value and will not offer the other.
    from parakeet_dictation import models as da_models
    from parakeet_dictation.ui import windows as da_windows
    dlg = da_windows.SettingsDialog(
        da_config.AppConfig(hotkey_mode="hold", insert_mode="per_segment"),
        da_models.load_model_profiles(), lambda _c: None)
    try:
        combo = getattr(dlg, "_insert_mode_combo", None)
        check("the Settings dialog shows end_of_take for hold mode",
              combo is not None and combo.get_active_id() == "end_of_take",
              combo.get_active_id() if combo is not None else "no control")
        check("and the control is disabled while hold mode is selected",
              combo is not None and not combo.get_sensitive())
        dlg._mode_toggle.set_active(True)
        check("choosing toggle mode re-enables the control",
              combo is not None and combo.get_sensitive())
        dlg._mode_hold.set_active(True)
        check("choosing hold mode disables it again and selects end_of_take",
              combo is not None and not combo.get_sensitive()
              and combo.get_active_id() == "end_of_take")
    finally:
        dlg.destroy()


# ---------------------------------------------------------------------------

SECTIONS = [defect2_config_load, defect3_engine_stop,
            defect4_single_instance, defect5_missing_helper_named,
            defect6_clipboard_verbatim, defect7_overlay_fallback_honest,
            defect8_model_hint, defect9_thread_default,
            defect10_focus_log_on_change,
            defect11_hold_mode_defaults_and_guard]


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
