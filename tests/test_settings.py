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


def s2_i18n():
    print("\n[2] i18n: explicit language, system fallback, missing catalogue")
    import os
    from parakeet_dictation import i18n
    got = i18n.setup("uk")
    check("uk loads", got == "uk", got)
    check("a known string is translated", i18n._("Settings") == "Налаштування", i18n._("Settings"))
    got = i18n.setup("en")
    check("en loads", got == "en" and i18n._("Settings") == "Settings")
    saved = {k: os.environ.get(k) for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ["LANG"] = "uk_UA.UTF-8"
        check("system follows LANG=uk_UA", i18n.setup("system") == "uk")
        os.environ["LANG"] = "de_DE.UTF-8"
        check("system with no catalogue falls back to en", i18n.setup("system") == "en"
              and i18n._("Settings") == "Settings")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    i18n.setup("en")
    po = i18n.LOCALE_DIR / "uk" / "LC_MESSAGES" / "parakeet-dictation.po"
    mo = po.with_suffix(".mo")
    check(".mo is not older than .po", mo.stat().st_mtime >= po.stat().st_mtime - 1)
    check("no fuzzy entries", "#, fuzzy" not in po.read_text().split("\n\n", 1)[1])


def s3_config_migration():
    print("\n[3] config: defaults, migration of the pynput era, enforce")
    import json, os, tempfile
    from parakeet_dictation import config as cfgmod
    d = cfgmod.AppConfig()
    check("defaults Insert / Meta+Insert / system",
          (d.hotkey_hold, d.hotkey_toggle, d.ui_language) == ("Insert", "Meta+Insert", "system"))
    for gone in ("hotkey_mode", "hotkey_start", "hotkey_stop", "hotkey_pause"):
        check(f"{gone} is gone", not hasattr(d, gone))
    events = []
    log = lambda ev, **kw: events.append((ev, kw))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"hotkey_mode": "toggle", "hotkey_toggle": "<ctrl>+0",
                                    "hotkey_start": "<ctrl>+9", "hotkey_pause": "<ctrl>+<alt>+0",
                                    "hotkey_hold": "Meta+Z", "insert_mode": "per_segment"}))
        saved = (cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR)
        cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR = path, Path(tmp)
        try:
            import io
            from contextlib import redirect_stderr
            with redirect_stderr(io.StringIO()):
                c = cfgmod.AppConfig.load(log)
                c.save(log)
        finally:
            cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR = saved
        check("pynput toggle -> Meta+Insert", c.hotkey_toggle == "Meta+Insert", c.hotkey_toggle)
        check("old default Meta+Z -> Insert", c.hotkey_hold == "Insert", c.hotkey_hold)
        check("per_segment survives (start/stop takes)", c.insert_mode == "per_segment")
        check("config_migrated logged", any(e == "config_migrated" for e, _ in events), str(events))
        on_disk = json.loads(path.read_text())
        check("dead keys dropped on save",
              not {"hotkey_mode", "hotkey_start", "hotkey_pause"} & set(on_disk), str(on_disk))
    with redirect_stderr(io.StringIO()):        # enforce() also warns on stderr
        c = cfgmod.AppConfig(hotkey_hold="Meta+Q", hotkey_toggle="Meta+Q")
        forced = c.enforce(lambda *a, **k: None)
        check("duplicate binding: toggle reset", c.hotkey_toggle == "Meta+Insert"
              and "hotkey_toggle" in forced, c.hotkey_toggle)
        c = cfgmod.AppConfig(hotkey_hold="Meta+Insert", hotkey_toggle="Meta+Insert")
        c.enforce(lambda *a, **k: None)
        check("duplicate of the default: toggle disabled", c.hotkey_toggle == "")
        c = cfgmod.AppConfig(hotkey_hold="Meta+", hotkey_toggle="")
        c.enforce(lambda *a, **k: None)
        check("unparseable hold reset, empty toggle kept", (c.hotkey_hold, c.hotkey_toggle) == ("Insert", ""))
        c = cfgmod.AppConfig(ui_language="de")
        c.enforce(lambda *a, **k: None)
        check("unknown ui_language reset", c.ui_language == "system")
        canon = []
        c = cfgmod.AppConfig(hotkey_hold="ctrl+d", hotkey_toggle="super+insert")
        forced = c.enforce(lambda ev, **kw: canon.append((ev, kw)))
        check("valid non-canonical bindings rewritten (R3)",
              (c.hotkey_hold, c.hotkey_toggle) == ("Ctrl+D", "Meta+Insert")
              and {"hotkey_hold", "hotkey_toggle"} <= set(forced)
              and sum(1 for ev, kw in canon
                      if ev == "config_forced" and kw.get("reason") == "canonical") == 2,
              f"{c.hotkey_hold!r} {c.hotkey_toggle!r} {canon}")


SECTIONS = {1: s1_shortcuts, 2: s2_i18n, 3: s3_config_migration}


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
