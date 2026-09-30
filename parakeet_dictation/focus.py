"""Focused-window tracking on KDE Plasma Wayland through a resident KWin script.

There is no client-side API for "which window is active" on KDE Wayland: KWin's
getWindowInfo() has no active flag and the runner/effects/virtual-keyboard
objects do not answer it either.  What works is the other direction — a KWin
script (data/focus.js) subscribes to workspace.windowActivated and calls a
method on a D-Bus name this process owns, so the answer is already cached by
the time a take ends.  A resident script costs nothing at insertion time.

Scripts loaded over D-Bus do not survive a KWin restart, so the KWin bus name
is watched and the script reloaded when it reappears; a periodic
isScriptLoaded() check covers a script unloaded by other means.  Where KWin
scripting is unavailable (another compositor) everything degrades to
"unknown" and the chord tables fall back to the configured default.
"""

import threading
import time
from collections import namedtuple
from pathlib import Path

from gi.repository import Gio, GLib

from .diagnostics import DIAG

FocusSnapshot = namedtuple("FocusSnapshot",
                           "resource_class resource_name caption pid timestamp")

_INTROSPECTION = """
<node>
  <interface name="org.kde.parakeet.Focus">
    <method name="Report">
      <arg type="s" name="resource_class" direction="in"/>
      <arg type="s" name="resource_name" direction="in"/>
      <arg type="s" name="caption" direction="in"/>
      <arg type="s" name="pid" direction="in"/>
    </method>
  </interface>
</node>
"""


class FocusTracker:
    BUS_NAME = "org.kde.parakeet.Focus"
    OBJECT_PATH = "/Focus"
    KWIN = "org.kde.KWin"
    SCRIPTING_PATH = "/Scripting"
    SCRIPTING_IFACE = "org.kde.kwin.Scripting"
    SCRIPT_IFACE = "org.kde.kwin.Script"
    PLUGIN = "parakeet-dictation-focus"
    SCRIPT = Path(__file__).resolve().parent / "data" / "focus.js"
    # How often snapshot() re-verifies that KWin still holds the script.
    RECHECK_S = 10.0

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._bus = None
        self._reg_id = 0
        self._own_id = 0
        self._watch_id = 0
        self._loaded = False
        self._last_check = 0.0
        self._lock = threading.Lock()
        self._latest = None
        self._last_logged_cls = None   # so alt-tabbing does not flood the log

    # -- D-Bus plumbing -----------------------------------------------------

    def _call(self, path, iface, method, params, reply_type=None, timeout=2000):
        return self._bus.call_sync(
            self.KWIN, path, iface, method, params,
            GLib.VariantType.new(reply_type) if reply_type else None,
            Gio.DBusCallFlags.NONE, timeout, None)

    def _on_report(self, _conn, _sender, _path, _iface, _method, params, invocation):
        rc, rn, cap, pid = params.unpack()
        try:
            pid = int(pid)
        except ValueError:
            pid = -1
        with self._lock:
            self._latest = FocusSnapshot(rc, rn, cap, pid, time.monotonic())
            changed = rc != self._last_logged_cls
            self._last_logged_cls = rc
        # The class only, and only when it changes: captions carry document
        # titles and chat names, and every activation is a line too many —
        # alt-tabbing between two windows of one app said nothing new.
        if changed:
            DIAG.log("focus", cls=rc or "unknown")
        invocation.return_value(None)

    def _export(self) -> bool:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION).interfaces[0]
        self._reg_id = self._bus.register_object_with_closures2(
            self.OBJECT_PATH, info, self._on_report, None, None)
        self._own_id = Gio.bus_own_name_on_connection(
            self._bus, self.BUS_NAME,
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
            None, None)
        return bool(self._reg_id and self._own_id)

    # -- the script ---------------------------------------------------------

    def _is_loaded(self) -> bool:
        try:
            return self._call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "isScriptLoaded",
                              GLib.Variant("(s)", (self.PLUGIN,)), "(b)",
                              timeout=500).unpack()[0]
        except GLib.Error:
            return False

    def _load(self, reason: str) -> bool:
        """(Re)load and run the script.  Idempotent: a stale copy left behind by
        a previous instance is unloaded first, since KWin refuses a duplicate
        plugin name."""
        t0 = time.monotonic()
        try:
            try:
                self._call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "unloadScript",
                           GLib.Variant("(s)", (self.PLUGIN,)), "(b)")
            except GLib.Error:
                pass
            script_id = self._call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "loadScript",
                                   GLib.Variant("(ss)", (str(self.SCRIPT), self.PLUGIN)),
                                   "(i)").unpack()[0]
            if script_id < 0:
                raise GLib.Error(f"loadScript returned {script_id}")
            self._call(f"{self.SCRIPTING_PATH}/Script{script_id}", self.SCRIPT_IFACE,
                       "run", None)
        except GLib.Error as e:
            self._loaded = False
            DIAG.log("focus_script", loaded=False, reason=reason, err=type(e).__name__)
            return False
        self._loaded = True
        self._last_check = time.monotonic()
        DIAG.log("focus_script", loaded=True, reason=reason,
                 ms=(time.monotonic() - t0) * 1000)
        return True

    def _on_kwin_appeared(self, _conn, _name, _owner):
        if not self._loaded:
            self._load("kwin_appeared")

    def _on_kwin_vanished(self, _conn, _name):
        self._loaded = False
        with self._lock:
            self._latest = None
            self._last_logged_cls = None

    # -- public API ---------------------------------------------------------

    def start(self) -> bool:
        """Export the report object and load the script.  Returns loaded."""
        if not self.enabled:
            DIAG.log("focus_script", loaded=False, reason="disabled")
            return False
        try:
            if not self._export():
                raise GLib.Error("export failed")
        except GLib.Error as e:
            DIAG.log("focus_script", loaded=False, reason="dbus", err=type(e).__name__)
            self._bus = None
            return False
        loaded = self._load("start")
        # Reload after a KWin restart — the script does not survive one.
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._bus, self.KWIN, Gio.BusNameWatcherFlags.NONE,
            self._on_kwin_appeared, self._on_kwin_vanished)
        return loaded

    def reload(self) -> bool:
        if not self.enabled or self._bus is None:
            return False
        return self._load("reload")

    def snapshot(self):
        """The latest report, or None when the focused window is unknown.

        Cheap: one cached tuple, plus at most one isScriptLoaded() round trip
        every RECHECK_S seconds to notice a script that went away.
        """
        if not self.enabled or self._bus is None:
            return None
        now = time.monotonic()
        if not self._loaded or now - self._last_check > self.RECHECK_S:
            self._last_check = now
            if not self._is_loaded():
                self._load("stale")
        with self._lock:
            latest = self._latest
        if latest is None or not latest.resource_class:
            return None
        return latest

    def stop(self):
        if self._bus is None:
            return
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._loaded:
            try:
                self._call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "unloadScript",
                           GLib.Variant("(s)", (self.PLUGIN,)), "(b)")
            except GLib.Error:
                pass
            self._loaded = False
        if self._own_id:
            Gio.bus_unown_name(self._own_id)
            self._own_id = 0
        if self._reg_id:
            self._bus.unregister_object(self._reg_id)
            self._reg_id = 0
        self._bus = None
