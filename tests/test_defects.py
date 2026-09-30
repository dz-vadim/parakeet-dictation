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
import time
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from parakeet_dictation import (config as da_config,  # noqa: E402
                                diagnostics as da_diagnostics)

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

SECTIONS = [defect2_config_load]


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
