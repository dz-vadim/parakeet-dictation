#!/usr/bin/env python3
"""Deterministic cover of the push-to-talk state machine with a stub engine.

The real-app runs prove the D-Bus + audio path; this covers the decisions that
need a keypress you cannot inject (a release with nothing running, a duplicate
release mid-stop) and the fast re-trigger that the reset rule exists for.
"""
import pathlib, sys, threading
sys.path.insert(0, "/home/dz/Projects/parakeet-dictation")
from parakeet_dictation import (config as da_config, controller as da_controller,
                                diagnostics as da_diagnostics, engine as da_engine)
from gi.repository import GLib

# Step 8 exercises apply_config(), which calls AppConfig.save() — point that
# at a scratch file so running the tests cannot rewrite the user's real config.
da_config.CONFIG_DIR = pathlib.Path(__file__).resolve().parent
da_config.CONFIG_FILE = da_config.CONFIG_DIR / ".ptt-state-config.json"

DIAG_PATH = pathlib.Path(__file__).resolve().parent / ".ptt-state-diag.log"
DIAG_PATH.unlink(missing_ok=True)
TEST_DIAG = da_diagnostics.DiagnosticLog(DIAG_PATH)
for _mod in (da_controller, da_engine):
    _mod.DIAG = TEST_DIAG

FAILS = []
def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok: FAILS.append(label)

class StubEngine:
    recognizer_key = ("stub", "", 0, "")   # what apply_config compares across a rebuild
    def __init__(self):
        self.running = False; self.paused = False
        self.starts = 0; self.stops = 0
    @property
    def is_running(self): return self.running
    @property
    def is_paused(self): return self.paused
    def start(self): self.starts += 1
    def stop(self): self.stops += 1; self.running = False
    def wait_drained(self, timeout=60.0): return True   # no queue to drain
    def pause(self): pass

class FakeOverlay:
    def __init__(self): self.states = []; self.previews = []; self.resets = 0
    def set_state(self, s, m=""): self.states.append((s, m))
    def push_level(self, *a): pass
    def capture_started(self): pass
    def set_capture_probe(self, p): pass
    def apply_config(self, c): pass
    def preview_append(self, text): self.previews.append(text)
    def preview_reset(self): self.resets += 1; self.previews.clear()
    def last(self): return self.states[-1] if self.states else None

ctl = da_controller.DictationController(da_config.AppConfig())
eng = StubEngine(); ctl._engine = eng
ov = FakeOverlay(); ctl.set_overlay(ov)

def capture_up():
    """What the engine's idle_add does once the input stream is open."""
    eng.running = True
    ctl._on_capture_start()

steps = []
def later(ms, fn): GLib.timeout_add(ms, lambda: (fn(), False)[1])

def run(seq):
    it = iter(seq)
    def nxt():
        try: delay, fn = next(it)
        except StopIteration:
            loop.quit(); return False
        GLib.timeout_add(delay, lambda: (fn(), nxt(), False)[2])
        return False
    nxt()

# ---------------------------------------------------------------------------
def s1_proceed():
    print("\n[1] press -> capture -> release: proceed")
    ov.states.clear()
    ctl.hold_press()
    check("press moves to starting", ctl.gesture == "starting", ctl.gesture)
    capture_up()
    check("capture_open moves to recording", ctl.gesture == "recording", ctl.gesture)
    check("pill is listening", ov.last()[0] == "listening", str(ov.last()))
    check("decide_stop == proceed while recording",
          ctl._decide_stop() == da_controller.STOP_PROCEED)
    ctl.hold_release()
    check("pill flips to processing before the tail elapses",
          ov.last()[0] == "processing", str(ov.last()))
    check("engine not stopped yet (tail still running)", eng.stops == 0)
    check("gesture is stopping", ctl.gesture == "stopping", ctl.gesture)

def s1_check():
    check("tail fired and engine stopped", eng.stops == 1, f"stops={eng.stops}")
    check("gesture reset to idle at take end", ctl.gesture == "idle", ctl.gesture)
    check("pill hidden (no speech in this take)", ov.last()[0] == "hidden",
          str(ov.last()))

# ---------------------------------------------------------------------------
def s2_defer():
    print("\n[2] release while the start is still in flight: deferred, not dropped")
    ov.states.clear(); eng.stops = 0
    ctl.hold_press()
    check("decide_stop == defer while starting", ctl._decide_stop() == da_controller.STOP_DEFER)
    ctl.hold_release()
    check("stop held, engine untouched", eng.stops == 0 and ctl.gesture == "starting",
          ctl.gesture)
    check("pill already processing", ov.last()[0] == "processing", str(ov.last()))
    capture_up()
    check("deferred stop applied the moment capture came up",
          ctl.gesture == "stopping", ctl.gesture)
    check("pill stayed on processing, no listening flash",
          [s for s, _ in ov.states].count("listening") == 0,
          str([s for s, _ in ov.states]))

def s2_check():
    check("engine stopped once", eng.stops == 1, f"stops={eng.stops}")
    check("gesture idle", ctl.gesture == "idle", ctl.gesture)

# ---------------------------------------------------------------------------
def s3_reject():
    print("\n[3] release with nothing running: visible error, not a silent no-op")
    ov.states.clear(); eng.stops = 0
    check("decide_stop == reject when idle", ctl._decide_stop() == da_controller.STOP_REJECT)
    ctl.hold_release()
    check("error state shown", ov.last()[0] == "error", str(ov.last()))
    check("message is actionable", "hold" in ov.last()[1].lower(), ov.last()[1])
    check("engine untouched", eng.stops == 0)
    check("gesture still idle", ctl.gesture == "idle", ctl.gesture)

# ---------------------------------------------------------------------------
def s4_duplicate():
    print("\n[4] duplicate release during an in-flight stop: ignored")
    ov.states.clear(); eng.stops = 0
    ctl.hold_press(); capture_up(); ctl.hold_release()
    n_before = len(ov.states)
    ctl.hold_release(); ctl.hold_release()
    check("no extra state changes from the duplicates",
          len(ov.states) == n_before, f"{len(ov.states)} vs {n_before}")
    check("still exactly one stop pending", ctl.gesture == "stopping", ctl.gesture)

def s4_check():
    check("engine stopped exactly once", eng.stops == 1, f"stops={eng.stops}")

# ---------------------------------------------------------------------------
def s5_retrigger():
    print("\n[5] fast re-trigger: the take that just ended must not eat the new hold")
    ov.states.clear(); eng.stops = 0; eng.starts = 0
    ctl.hold_press()
    check("new take accepted right after the previous one ended",
          ctl.gesture == "starting" and eng.starts == 1,
          f"{ctl.gesture} starts={eng.starts}")
    capture_up()
    ctl.hold_release()
    check("its release is honoured (mic does not stay on)",
          ctl.gesture == "stopping", ctl.gesture)

def s5_check():
    check("re-triggered take stopped the engine", eng.stops == 1, f"stops={eng.stops}")
    check("gesture idle again", ctl.gesture == "idle", ctl.gesture)

# ---------------------------------------------------------------------------
def s6_start_failure():
    print("\n[6] start failure: terminal path resets state")
    ov.states.clear(); eng.stops = 0
    ctl.hold_press()
    check("starting", ctl.gesture == "starting", ctl.gesture)
    ctl._on_error("Missing model files: encoder.onnx")
    check("gesture reset to idle on start failure", ctl.gesture == "idle", ctl.gesture)
    check("error pill with a model hint", ov.last()[0] == "error"
          and "Model" in ov.last()[1], str(ov.last()))
    log = DIAG_PATH.read_text()
    check("start_failed logged as a terminal outcome",
          "outcome=start_failed" in log)

# ---------------------------------------------------------------------------
def s7_external():
    print("\n[7] started from the tray: same state machine, next press is a stop")
    ov.states.clear(); eng.stops = 0; eng.starts = 0
    ctl.start()                       # what the tray / window button calls
    capture_up()
    check("external start lands in recording", ctl.gesture == "recording", ctl.gesture)
    ctl.hold_press()
    check("a press is ignored, not read as a fresh start",
          eng.starts == 1, f"starts={eng.starts}")
    ctl.hold_release()
    check("the release stops it", ctl.gesture == "stopping", ctl.gesture)

def s7_check():
    check("engine stopped", eng.stops == 1, f"stops={eng.stops}")
    check("idle", ctl.gesture == "idle", ctl.gesture)

def s8_cancel():
    print("\n[8] settings saved mid-take: cancel is a terminal path too")
    ov.states.clear(); eng.stops = 0
    ctl.hold_press(); capture_up()
    check("recording", ctl.gesture == "recording", ctl.gesture)
    ctl.apply_config(ctl.config)
    check("gesture reset to idle on config change", ctl.gesture == "idle", ctl.gesture)
    check("pill hidden", ov.last()[0] == "hidden", str(ov.last()))
    log = DIAG_PATH.read_text()
    check("cancel logged as the outcome", "outcome=cancel reason=config_change" in log)
    ctl._engine = eng                 # apply_config rebuilt a real engine
    eng.running = False

def s8_check():
    log = DIAG_PATH.read_text()
    check("no second take_end for the cancelled take",
          log.count("reason=config_change") == 1,
          str(log.count("reason=config_change")))

loop = GLib.MainLoop()
run([(50, s1_proceed), (600, s1_check),
     (50, s2_defer),   (600, s2_check),
     (50, s3_reject),
     (50, s4_duplicate), (600, s4_check),
     (50, s5_retrigger), (600, s5_check),
     (50, s6_start_failure),
     (50, s7_external),  (600, s7_check),
     (50, s8_cancel),    (900, s8_check)])
GLib.timeout_add(30000, lambda: (print("TIMEOUT"), loop.quit(), False)[2])
loop.run()

print("\n" + "=" * 60)
print("ALL CHECKS PASSED" if not FAILS else f"FAILED ({len(FAILS)}): {FAILS}")
sys.exit(1 if FAILS else 0)
