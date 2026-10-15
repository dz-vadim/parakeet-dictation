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


class FakeBus:
    def __init__(self):
        self.calls, self.subs, self.unsubs = [], {}, []
        self._next = 1

    def signal_subscribe(self, _svc, _iface, signal, _path, _arg0, _flags, handler):
        sid = self._next
        self._next += 1
        self.subs[sid] = (signal, handler)
        return sid

    def signal_unsubscribe(self, sid):
        self.unsubs.append(sid)
        self.subs.pop(sid, None)


def s4_hotkeys_two_actions():
    print("\n[4] hotkeys: hold + toggle registered with kglobalaccel, no pynput")
    from types import SimpleNamespace
    from gi.repository import Gio, GLib
    from parakeet_dictation import hotkeys as hk, config as cfgmod, shortcuts as sc
    bus = FakeBus()
    calls = []

    def fake_call(_bus, _svc, path, iface, method, params, reply_type=None, _t=0):
        calls.append((method, params.unpack() if params is not None else None))
        if method == "globalShortcutAvailable":
            return GLib.Variant("(b)", (True,))
        if method == "getComponent":
            return GLib.Variant("(o)", ("/component/parakeet_dictation",))
        if method == "setShortcutKeys":
            return GLib.Variant("(a(ai))", ([],))
        return None

    saved = (hk.Gio, hk.call_sync)
    hk.Gio = SimpleNamespace(
        bus_get_sync=lambda *_a: bus, BusType=Gio.BusType, DBusSignalFlags=Gio.DBusSignalFlags,
        BusNameWatcherFlags=Gio.BusNameWatcherFlags,
        bus_watch_name_on_connection=lambda *a: 7, bus_unwatch_name=lambda *_a: None)
    hk.call_sync = fake_call
    pressed, released, toggled, changed = [], [], [], []
    try:
        mgr = hk.HotkeyManager(cfgmod.AppConfig(), on_hold_press=lambda: pressed.append(1),
                               on_hold_release=lambda: released.append(1),
                               on_toggle=lambda: toggled.append(1),
                               on_binding_changed=lambda k, b: changed.append((k, b)))
        status = mgr.start()
        check("start() is clean", status == "", status)
        regs = [p[0][1] for m, p in calls if m == "doRegister"]
        check("both actions registered", regs == ["push-to-talk", "toggle"], str(regs))
        keys = [p[1][0][0][0] for m, p in calls if m == "setShortcutKeys"]
        check("Insert and Meta+Insert bound",
              keys == [sc.qt_key_sequence("Insert"), sc.qt_key_sequence("Meta+Insert")], str(keys))
        press = [h for s, h in bus.subs.values() if s == "globalShortcutPressed"]
        for h in press:
            h(None, None, None, None, None, GLib.Variant("(ssx)", ("parakeet-dictation", "toggle", 0)))
            h(None, None, None, None, None, GLib.Variant("(ssx)", ("parakeet-dictation", "push-to-talk", 0)))
        check("toggle press -> on_toggle once", len(toggled) == 1, str(toggled))
        check("hold press -> on_hold_press once", len(pressed) == 1, str(pressed))
        changed_handlers = [h for s, h in bus.subs.values() if s == "yourShortcutsChanged"]
        check("listens for KDE-side changes", len(changed_handlers) == 1)
        changed_handlers[0](None, None, None, None, None, GLib.Variant(
            "(asa(ai))", (["parakeet-dictation", "toggle", "Parakeet Dictation", "x"],
                          [([sc.qt_key_sequence("Meta+Alt+D")],)])))
        check("KDE change reported as hotkey_toggle=Meta+Alt+D",
              changed == [("hotkey_toggle", "Meta+Alt+D")], str(changed))

        def kde_change(action, keys):
            changed.clear()
            changed_handlers[0](None, None, None, None, None, GLib.Variant(
                "(asa(ai))", (["parakeet-dictation", action, "Parakeet Dictation", "x"],
                              keys)))
            return list(changed)

        got = kde_change("toggle", [([0x010000AD],)])         # Qt::Key_MicMute
        check("an unnamable key from KDE is not reported", got == [], str(got))
        got = kde_change("toggle", [([sc.qt_key_sequence("Meta+K"),
                                      sc.qt_key_sequence("Meta+L")],)])
        check("a two-chord sequence from KDE is not reported", got == [], str(got))
        got = kde_change("push-to-talk", [])
        check("a binding cleared in KDE is reported as \"\"",
              got == [("hotkey_hold", "")], str(got))
        mgr.stop()
        calls.clear()
        status = mgr.rebuild(cfgmod.AppConfig(hotkey_toggle=""))
        regs = [p[0][1] for m, p in calls if m == "doRegister"]
        check("empty toggle is not registered", regs == ["push-to-talk"], str(regs))
        check("no pynput import", "pynput" not in sys.modules)
    finally:
        hk.Gio, hk.call_sync = saved


def s5_controller_take_kind():
    print("\n[5] controller: hold takes insert at the end; hold-off only after a modifier")
    import io
    from contextlib import redirect_stderr
    from parakeet_dictation import config as cfgmod, controller as ctlmod

    class Eng:
        running = paused = False
        recognizer_key = ("desktop", 4, "en")
        def start(self): pass
        def stop(self): self.running = False
        def stop_async(self, *a, **k): self.running = False
        def wait_drained(self, *a, **k): return True
        @property
        def is_running(self): return self.running
        @property
        def is_paused(self): return self.paused

    def make(**kw):
        with redirect_stderr(io.StringIO()):
            ctl = ctlmod.DictationController(cfgmod.AppConfig(**kw))
        ctl._engine = Eng()
        return ctl

    ctl = make(insert_mode="per_segment")
    ctl.hold_press()
    check("hold take inserts at end even with per_segment", ctl._insert_at_end)
    ctl = make(insert_mode="per_segment")
    ctl.start()
    check("toggle take honours per_segment", not ctl._insert_at_end)
    ctl = make(hotkey_hold="Insert")
    ctl.hold_press(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.hold_release()
    check("bare-key release: no hold-off", ctl._release_t == 0.0, str(ctl._release_t))
    ctl = make(hotkey_hold="Meta+Z")
    ctl.hold_press(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.hold_release()
    check("modifier hold release: hold-off armed", ctl._release_t > 0.0)
    ctl = make()
    ctl.start(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.stop(hold_off=False)
    check("tray stop: no hold-off", ctl._release_t == 0.0)
    ctl = make()
    ctl.start(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.stop()
    check("signal/shortcut stop: hold-off armed", ctl._release_t > 0.0)


def s6_menu_model():
    print("\n[6] tray menu model")
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.ui import menu_model as mm
    i18n.setup("en")
    cfg = cfgmod.AppConfig(audio_device="WM105 Keyboard", model_profile="desktop")
    profiles = {"desktop": {"name": "Parakeet TDT 0.6B v3 (int8)"},
                "laptop": {"name": "Canary 180M Flash (int8)"},
                "streaming": {"name": "Nemotron Streaming 0.6B (int8)"}}
    items = mm.build_menu("idle", cfg, profiles, {"desktop", "laptop"}, ["WM105 Keyboard", "Built-in"])
    labels = [i.label for i in items if i.kind != "separator"]
    check("idle top-level order", labels == ["Start Dictation", "Microphone", "Model",
                                             "Settings…", "About", "Quit"], str(labels))
    check("start shows Meta+Insert", items[0].accel == "Meta+Insert")
    check("no disabled/status rows", all(i.kind in ("action", "submenu", "separator") for i in items))
    mic = next(i for i in items if i.label == "Microphone")
    check("mic: default + devices, saved one active",
          [(c.label, c.active) for c in mic.children] ==
          [("System default", False), ("WM105 Keyboard", True), ("Built-in", False)])
    model = next(i for i in items if i.label == "Model")
    check("model: only downloaded + Download More…",
          [c.label for c in model.children if c.kind != "separator"] ==
          ["Parakeet TDT 0.6B v3 (int8)", "Canary 180M Flash (int8)", "Download More…"])
    rec = mm.build_menu("recording", cfg, profiles, {"desktop"}, [])
    check("recording: Stop + Pause",
          [i.label for i in rec[:2]] == ["Stop Dictation", "Pause"]
          and [i.action for i in rec[:2]] == ["toggle", "pause"])
    pau = mm.build_menu("paused", cfg, profiles, {"desktop"}, [])
    check("paused: Stop + Resume", [i.label for i in pau[:2]] == ["Stop Dictation", "Resume"])
    cfg2 = cfgmod.AppConfig(hotkey_toggle="")
    check("no toggle binding -> no accel", mm.build_menu("idle", cfg2, profiles, {"desktop"}, [])[0].accel == "")
    gone = cfgmod.AppConfig(audio_device="USB Mic")
    mic = next(i for i in mm.build_menu("idle", gone, profiles, {"desktop"}, ["Built-in"]) if i.label == "Microphone")
    check("disconnected saved mic still listed and active",
          ("USB Mic (not connected)", True) in [(c.label, c.active) for c in mic.children])
    check("tooltip idle names the hold key",
          mm.tooltip("idle", cfg) == "Parakeet Dictation — ready · hold Insert to talk", mm.tooltip("idle", cfg))
    i18n.setup("uk")
    check("labels are translated", mm.build_menu("idle", cfg, profiles, {"desktop"}, [])[0].label
          == "Почати диктування")
    i18n.setup("en")


def s7_settings_draft():
    print("\n[7] settings draft: dirty, page defaults, diff-merge commit")
    from parakeet_dictation import config as cfgmod
    from parakeet_dictation.ui.settings.model import SettingsDraft, PAGE_KEYS
    applied = cfgmod.AppConfig(num_threads=2, overlay_position="top")
    d = SettingsDraft(applied)
    check("fresh draft is clean", not d.dirty)
    d.set("overlay_position", "bottom")
    check("edit makes it dirty", d.dirty)
    d.set("overlay_position", "top")
    check("edit back is clean again", not d.dirty)
    d.set("num_threads", 6)
    d.set("overlay_position", "bottom")
    d.reset_page("advanced")
    check("Defaults resets only the page", d.get("num_threads") == cfgmod.AppConfig().num_threads
          and d.get("overlay_position") == "bottom")
    current = cfgmod.AppConfig(num_threads=2, overlay_position="top", model_profile="laptop")
    out = d.commit(current)
    check("commit keeps a change made elsewhere (tray model switch)",
          out.model_profile == "laptop", out.model_profile)
    check("commit applies the window's edits", out.overlay_position == "bottom"
          and out.num_threads == cfgmod.AppConfig().num_threads)
    check("commit returns a copy", out is not current)
    check("clean after commit", not d.dirty)
    every = {k for keys in PAGE_KEYS.values() for k in keys}
    check("every page key is a config field", every <= set(cfgmod.AppConfig.__dataclass_fields__), str(every - set(cfgmod.AppConfig.__dataclass_fields__)))


def s8_autostart():
    print("\n[8] autostart file")
    import os, tempfile
    from parakeet_dictation import autostart
    saved = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            check("off by default", not autostart.is_enabled())
            autostart.set_enabled(True)
            text = autostart.desktop_path().read_text()
            check("written under XDG_CONFIG_HOME", str(autostart.desktop_path()).startswith(tmp))
            check("Exec line present", f"Exec={autostart.exec_line()}" in text, text)
            check("now enabled", autostart.is_enabled())
            autostart.desktop_path().write_text(text + "Hidden=true\n")
            check("Hidden=true counts as off", not autostart.is_enabled())
            autostart.set_enabled(False)
            check("removed", not autostart.desktop_path().exists())
        finally:
            if saved is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = saved


def s9_widgets():
    print("\n[9] widgets: shortcut button state machine, device choices (GTK)")
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk
    from parakeet_dictation import i18n
    from parakeet_dictation.ui.settings import widgets as w
    i18n.setup("en")
    got = []
    b = w.ShortcutButton("Insert", got.append)
    check("shows its binding", b.get_label() == "Insert")
    b.clicked()
    check("listening prompt", b.get_label() == "Press a shortcut…", b.get_label())
    check("lone Meta keeps listening", b.feed("Super_L", frozenset({"Meta"})) == "wait")
    check("Meta+Insert accepted", b.feed("Insert", frozenset({"Meta"})) == "done"
          and got == ["Meta+Insert"] and b.get_label() == "Meta+Insert", str(got))
    b.clicked()
    check("bare letter rejected, still listening", b.feed("a", frozenset()) == "reject"
          and b.get_label() == "Press a shortcut…")
    check("Escape restores", b.feed("Escape", frozenset()) == "cancel" and b.get_label() == "Meta+Insert")
    b.clicked()
    b.feed("BackSpace", frozenset())
    check("Backspace disables", got[-1] == "" and b.get_label() == "None", b.get_label())
    b.clicked()
    n = len(got)
    b.emit("focus-out-event", Gdk.Event.new(Gdk.EventType.FOCUS_CHANGE))
    check("focus loss cancels the capture", b.get_label() == "None" and len(got) == n
          and b.feed("Insert", frozenset()) == "idle", b.get_label())
    ch = w.device_choices("USB Mic", ["Built-in"])
    check("device choices keep a missing saved mic",
          ch == [("", "System default"), ("Built-in", "Built-in"), ("USB Mic", "USB Mic (not connected)")], str(ch))
    check("numeric legacy value is not shown as a name", w.device_choices("7", ["A"])[-1] == ("A", "A"))


def s10_settings_window():
    print("\n[10] settings window: pages, Apply/Cancel/Defaults, duplicate shortcut (GTK)")
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.models import load_model_profiles
    from parakeet_dictation.ui.settings import SettingsWindow
    i18n.setup("en")
    state = {"cfg": cfgmod.AppConfig(), "applied": []}

    def on_apply(cfg):
        state["applied"].append(cfg)
        state["cfg"] = cfg
        return ""

    win = SettingsWindow(lambda: state["cfg"], load_model_profiles(), on_apply, lambda: None)
    for page in ("general", "recognition", "output", "advanced", "about"):
        win.show_page(page)
        check(f"page {page} shown", win.stack.get_visible_child_name() == page)
    check("Apply starts disabled", not win.apply_button.get_sensitive())
    win.draft.set("overlay_position", "top"); win._refresh_buttons()
    check("an edit enables Apply", win.apply_button.get_sensitive())
    check("apply() succeeds", win.apply())
    check("on_apply got the edit", state["applied"][-1].overlay_position == "top")
    check("Apply disabled again", not win.apply_button.get_sensitive())
    win.draft.set("hotkey_toggle", "Insert"); win._refresh_buttons()
    check("same shortcut twice is refused", not win.apply()
          and "already used" in win.info_label.get_text(), win.info_label.get_text())
    win.cancel()
    check("cancel discards the draft", win.draft.get("hotkey_toggle") == state["cfg"].hotkey_toggle)
    win.show_page("advanced")
    win.draft.set("num_threads", 9); win.draft.set("overlay_position", "bottom")
    win.defaults()
    check("Defaults resets the current page only",
          win.draft.get("num_threads") == cfgmod.AppConfig().num_threads
          and win.draft.get("overlay_position") == "bottom")
    win.destroy()
    odd = cfgmod.AppConfig(insert_mode="per_segment", tray_icon_style="parrot", beep_volume=0.3,
                           overlay_position="top", ui_language="uk")
    win = SettingsWindow(lambda: odd, load_model_profiles(), on_apply, lambda: None)
    check("building from a non-default config leaves the draft clean",
          not win.draft.dirty and not win.apply_button.get_sensitive(), str(win.draft.changed_keys()))
    win.destroy()
    # R9: spin bounds come from config._RANGES; beeps-off slider; Start-at-login failure.
    win = SettingsWindow(lambda: cfgmod.AppConfig(beep_volume=0.0), load_model_profiles(),
                         on_apply, lambda: None)

    def walk(w):
        yield w
        if isinstance(w, Gtk.Container):
            for c in w.get_children():
                yield from walk(c)
    ranges = {tuple(s.get_range()) for s in walk(win) if isinstance(s, Gtk.SpinButton)}
    for key in ("num_threads", "vad_min_silence", "paste_min_delay_ms"):
        lo, hi = cfgmod._RANGES[key]
        check(f"{key} spin bounds match config._RANGES", (float(lo), float(hi)) in ranges, str(ranges))
    scale = next(s for s in walk(win) if isinstance(s, Gtk.Scale))
    check("beeps off: slider shows the default volume",
          abs(scale.get_value() - cfgmod.AppConfig().beep_volume) < 1e-9, str(scale.get_value()))
    from parakeet_dictation import autostart
    saved_set = autostart.set_enabled

    def failing(_on):
        raise PermissionError(13, "Permission denied")
    autostart.set_enabled = failing
    try:
        win.autostart_wanted = not win._autostart_initial
        check("apply() is False when Start at login cannot change", win.apply() is False)
        check("its message is shown", "Start at login" in win.info_label.get_text(),
              win.info_label.get_text())
    finally:
        autostart.set_enabled = saved_set
    win.destroy()


def s11_tray():
    print("\n[11] tray renders the menu model; actions reach the controller (GTK)")
    from types import SimpleNamespace
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.ui import tray as traymod
    i18n.setup("en")

    class FakeIndicator:
        def __init__(self): self.menu = None; self.title = ""; self.icon = None
        def set_icon_theme_path(self, *_a): pass
        def set_icon_full(self, name, _desc): self.icon = name
        def set_status(self, *_a): pass
        def set_title(self, t): self.title = t
        def set_menu(self, m): self.menu = m

    ind = FakeIndicator()
    saved = traymod.AyatanaAppIndicator3
    traymod.AyatanaAppIndicator3 = SimpleNamespace(
        Indicator=SimpleNamespace(new=lambda *a: ind),
        IndicatorCategory=SimpleNamespace(APPLICATION_STATUS=0),
        IndicatorStatus=SimpleNamespace(ACTIVE=1))
    calls = []
    ctl = SimpleNamespace(config=cfgmod.AppConfig(), profiles={"desktop": {"name": "P3"}},
                          profiles_data={"profiles": {"desktop": {"name": "P3"}}},
                          is_running=False, is_paused=False,
                          toggle=lambda hold_off=True: calls.append(("toggle", hold_off)),
                          pause=lambda: calls.append(("pause",)),
                          set_status_callback=lambda cb: None, notify=lambda m: None,
                          apply_config=lambda c: calls.append(("apply", c)))
    mgr = SimpleNamespace(rebuild=lambda c: "")
    try:
        t = traymod.TrayIcon(ctl, mgr)
        labels = [i.get_label() for i in ind.menu.get_children() if not isinstance(i, Gtk.SeparatorMenuItem)]
        check("menu starts with Start Dictation", labels[0].startswith("Start Dictation"), str(labels))
        check("no Show Window", not any("Show Window" in l for l in labels))
        check("tooltip set", ind.title.endswith("hold Insert to talk"), ind.title)
        ind.menu.get_children()[0].activate()
        check("tray toggle stops without hold-off", calls[-1] == ("toggle", False), str(calls))
        t.on_binding_changed("hotkey_toggle", "Meta+Alt+D")
        check("KDE-side change applied to config", calls[-1][0] == "apply"
              and calls[-1][1].hotkey_toggle == "Meta+Alt+D")
    finally:
        traymod.AyatanaAppIndicator3 = saved

    # R8: a hidden long-lived settings window is refreshed when reopened.
    import os, tempfile
    from parakeet_dictation.ui.settings import SettingsWindow
    saved_present = SettingsWindow.present
    SettingsWindow.present = lambda self: None          # never map a window during tests
    saved_xdg = os.environ.get("XDG_CONFIG_HOME")
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    traymod.AyatanaAppIndicator3 = SimpleNamespace(
        Indicator=SimpleNamespace(new=lambda *a: ind),
        IndicatorCategory=SimpleNamespace(APPLICATION_STATUS=0),
        IndicatorStatus=SimpleNamespace(ACTIVE=1))
    try:
        from parakeet_dictation import autostart
        t = traymod.TrayIcon(ctl, mgr)
        t.open_settings("advanced")
        win = t._settings
        check("settings opens on the requested page", win.current_page == "advanced")
        win.hide()
        ctl.config = cfgmod.AppConfig(num_threads=7, audio_device="Gone Mic")
        autostart.set_enabled(True)
        t.open_settings("general")
        check("same window instance reused", t._settings is win)
        check("reopened window shows the new config", win.draft.get("num_threads") == 7)
        spins = []

        def walk(w):
            yield w
            if isinstance(w, Gtk.Container):
                for c in w.get_children():
                    yield from walk(c)
        values = [s.get_value() for s in walk(win) if isinstance(s, Gtk.SpinButton)]
        check("the widget shows it too", 7.0 in values, str(values))
        combos = [c for c in walk(win) if isinstance(c, Gtk.ComboBoxText)]
        check("microphone combo keeps the saved-but-missing device",
              any(c.get_active_id() == "Gone Mic" for c in combos))
        check("autostart state re-read", win.autostart_wanted and win._autostart_initial)
        check("Apply is disabled after the refresh", not win.apply_button.get_sensitive())
        win.destroy()
    finally:
        SettingsWindow.present = saved_present
        traymod.AyatanaAppIndicator3 = saved
        if saved_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved_xdg
        tmp.cleanup()


SECTIONS = {1: s1_shortcuts, 2: s2_i18n, 3: s3_config_migration, 4: s4_hotkeys_two_actions,
            5: s5_controller_take_kind, 6: s6_menu_model,
            7: s7_settings_draft, 8: s8_autostart, 9: s9_widgets,
            10: s10_settings_window, 11: s11_tray}


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
