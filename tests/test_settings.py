#!/usr/bin/env python3
"""Checks for the tray/settings redesign (2026-10-15).

Display-free sections test pure modules; sections marked GTK build widgets
offscreen.  Nothing touches the real config, autostart dir, D-Bus names or
the diagnostics log.

Run:  .venv/bin/python tests/test_settings.py            # every section
      .venv/bin/python tests/test_settings.py 1 3        # sections 1 and 3
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def s1_shortcuts():
    print("\n[1] shortcuts: parse, reverse, validate, capture")
    from parakeet_dictation import shortcuts as sc
    check("Meta+Alt+D is 0x18000044", sc.qt_key_sequence("Meta+Alt+D") == 0x18000044)
    check("Insert parses", sc.qt_key_sequence("Insert") == 0x01000006)
    for b in ("Insert", "Meta+Insert", "Meta+Alt+D", "Ctrl+Shift+F5", "Pause", "Meta+Space"):
        rt = sc.binding_from_qt(sc.qt_key_sequence(b))
        check(f"round trip {b}", rt == b, rt)
    check("binding_from_qt(0) is empty", sc.binding_from_qt(0) == "")
    check("empty is valid (disabled)", sc.is_valid(""))
    check("<ctrl>+0 is invalid", not sc.is_valid("<ctrl>+0"))
    check("Insert has no modifier", not sc.has_modifier("Insert"))
    check("Meta+Insert has a modifier", sc.has_modifier("Meta+Insert"))
    F = frozenset
    cases = [
        (("Super_L", F({"Meta"})), ("wait", "")),
        (("Control_R", F({"Ctrl"})), ("wait", "")),
        (("Escape", F()), ("cancel", "")),
        (("BackSpace", F()), ("clear", "")),
        (("Insert", F()), ("done", "Insert")),
        (("Insert", F({"Meta"})), ("done", "Meta+Insert")),
        (("d", F({"Alt", "Meta"})), ("done", "Meta+Alt+D")),
        (("F5", F({"Shift", "Ctrl"})), ("done", "Ctrl+Shift+F5")),
        (("F13", F()), ("done", "F13")),
        (("a", F()), ("reject", "")),
        (("space", F()), ("reject", "")),
        (("space", F({"Meta"})), ("done", "Meta+Space")),
        (("Scroll_Lock", F()), ("done", "ScrollLock")),
        (("Prior", F({"Ctrl"})), ("done", "Ctrl+PageUp")),
        (("dead_acute", F({"Ctrl"})), ("reject", "")),
    ]
    for (name, mods), want in cases:
        got = sc.capture(name, mods)
        check(f"capture {name} {sorted(mods)}", got == want, str(got))


SECTIONS = {1: s1_shortcuts}


def main(argv):
    wanted = [int(a) for a in argv] or sorted(SECTIONS)
    for n in wanted:
        section = SECTIONS.get(n)
        if section is None:
            check(f"section {n} exists", False)
            continue
        try:
            section()
        except Exception as e:
            check(f"{section.__name__} ran without raising", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
