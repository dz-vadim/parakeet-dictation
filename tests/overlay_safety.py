#!/usr/bin/env python3
"""Overlay honesty checks: refusal gate, watchdog, stale meter, preview panel."""
import sys, time
sys.path.insert(0, "/home/dz/Projects/parakeet-dictation")
from parakeet_dictation import config as da_config, diagnostics as da_diagnostics
from parakeet_dictation.ui import overlay as da_overlay
from gi.repository import GLib

import pathlib as _pl; DIAG_PATH = str(_pl.Path(__file__).resolve().parent / ".overlay-safety-diag.log")
import pathlib; pathlib.Path(DIAG_PATH).unlink(missing_ok=True)
da_overlay.DIAG = da_diagnostics.DiagnosticLog(pathlib.Path(DIAG_PATH))

capturing = {"on": False}
ov = da_overlay.PillOverlay(da_config.AppConfig())
ov.set_capture_probe(lambda: capturing["on"])
fails = []
def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok: fails.append(label)

def phase1():
    ov.set_state("listening")
    check("listening refused while engine not capturing", ov._state == "hidden",
          f"state={ov._state}")
    ov.set_state("paused")
    check("paused refused while engine not capturing", ov._state == "hidden",
          f"state={ov._state}")
    capturing["on"] = True
    ov.capture_started()
    ov.set_state("listening")
    check("listening shown once the engine reports capture", ov._state == "listening")
    check("watchdog armed", ov._watchdog_source != 0)
    for v in (0.05, 0.1, 0.15): ov.push_level(v, True)
    check("meter fresh right after a buffer", not ov._levels_stale())
    GLib.timeout_add(2600, phase2)
    return False

def phase2():
    check("meter goes stale after 2 s with no buffers", ov._levels_stale(),
          f"{time.monotonic() - ov._last_level:.1f} s since last buffer")
    check("still listening while the engine says it is capturing",
          ov._state == "listening")
    capturing["on"] = False          # engine dies / session ends silently
    GLib.timeout_add(2600, phase3)
    return False

def phase3():
    check("watchdog hid the pill once capture stopped", ov._state == "hidden",
          f"state={ov._state}")
    log = pathlib.Path(DIAG_PATH).read_text()
    check("refusal logged", "event=overlay_state_refused" in log)
    check("watchdog logged", "event=overlay_watchdog" in log)
    GLib.timeout_add(200, phase4_preview)
    return False


def phase4_preview():
    """The transcript preview is scenery that follows the pill, and only shows
    text while the pill is claiming a take."""
    check("no preview surface exists before anything is previewed",
          not ov.preview.visible())

    # Pill hidden = no take = nothing for a panel to be showing.
    ov.preview_append("this should not appear on screen")
    check("preview refused while the pill is hidden", not ov.preview.visible())
    check("refusal logged", "event=preview_refused"
          in pathlib.Path(DIAG_PATH).read_text())
    ov.preview_reset()

    capturing["on"] = True
    ov.capture_started()
    ov.set_state("listening")
    ov.preview_append("First segment of the take.")
    check("panel appears when the first segment decodes", ov.preview.visible())
    one_row = len(ov.preview.rows)
    h1 = ov.preview.panel_height()
    ov.preview_append("Second segment, appended after the first one and long "
                      "enough to need another line of its own on screen.")
    check("the panel grew with the second segment",
          ov.preview.segments == 2 and len(ov.preview.rows) >= one_row
          and ov.preview.panel_height() >= h1,
          f"{ov.preview.segments} segments, {len(ov.preview.rows)} rows, "
          f"{ov.preview.panel_height()} px")
    check("panel is wider than the pill and still on screen",
          da_overlay.PillOverlay.WIDTH < ov.preview.panel_width()
          <= da_overlay.TranscriptPreview.MAX_WIDTH, f"{ov.preview.panel_width()} px")
    check("the pill itself was neither moved nor resized by the panel",
          ov._win.get_size() == (da_overlay.PillOverlay.WIDTH, da_overlay.PillOverlay.HEIGHT),
          str(ov._win.get_size()))
    check("preview_update logged with chars and segments",
          any("event=preview_update" in l and "chars=" in l and "segments=" in l
              for l in pathlib.Path(DIAG_PATH).read_text().splitlines()))

    ov.set_state("success")
    check("the panel survives into the pill's success state", ov.preview.visible())
    ov.set_state("hidden")
    check("and disappears with the pill", not ov.preview.visible())
    check("its text is dropped with the take", ov.preview.chars == 0
          and ov.preview.segments == 0)

    # The setting is a real off switch, not just a hidden window.
    off = da_config.AppConfig(preview=False)
    ov.apply_config(off)
    capturing["on"] = True
    ov.capture_started()
    ov.set_state("listening")
    ov.preview_append("preview is switched off")
    check("preview=False shows nothing and stores nothing",
          not ov.preview.visible() and ov.preview.chars == 0)
    ov.apply_config(da_config.AppConfig())

    da_overlay.Gtk.main_quit()
    return False

GLib.timeout_add(20000, lambda: (print("TIMEOUT"), da_overlay.Gtk.main_quit(), False)[2])
GLib.timeout_add(200, phase1)
try:
    da_overlay.Gtk.main()
finally:
    ov.debug_release()
print("RESULT:", "ALL PASS" if not fails else f"FAILED: {fails}")
