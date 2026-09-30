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
    # How often the timer re-verifies that KWin still holds the script.
    RECHECK_S = 10.0

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._bus = None
        self._reg_id = 0
        self._own_id = 0
        self._watch_id = 0
        self._timer_id = 0
        self._loaded = False
        self._loading = False          # a _load chain is in flight
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

    def _recheck(self):
        """Timer tick: ask KWin whether the script is still loaded.

        Asynchronously — this runs on the GTK loop, and the reply may take
        as long as KWin takes.  The bus-name watcher covers a KWin restart;
        this covers a script unloaded by other means (a Scripting KCM
        change, say).  Nothing on the key-release path ever waits on it.
        """
        if self._bus is None:
            self._timer_id = 0
            return GLib.SOURCE_REMOVE
        self._bus.call(self.KWIN, self.SCRIPTING_PATH, self.SCRIPTING_IFACE,
                       "isScriptLoaded", GLib.Variant("(s)", (self.PLUGIN,)),
                       GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 500, None,
                       self._on_recheck_reply)
        return GLib.SOURCE_CONTINUE

    def _on_recheck_reply(self, bus, result, *_user):
        try:
            loaded = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return          # KWin not answering: the name watcher's case, not ours
        if not loaded and self._bus is not None:
            self._load("stale")

    def _load(self, reason: str):
        """(Re)load and run the script.  Idempotent: a stale copy left behind by
        a previous instance is unloaded first, since KWin refuses a duplicate
        plugin name.

        Asynchronous, like _recheck: three round trips to KWin on the GTK
        loop would otherwise stall startup for as long as KWin takes.  One
        chain at a time — the name watcher fires as soon as it is armed, and
        a second chain would unload the first one's script under it.
        """
        if self._loading:
            return
        self._loading = True
        t0 = time.monotonic()

        def call(path, iface, method, params, reply_type, callback):
            self._bus.call(self.KWIN, path, iface, method, params,
                           GLib.VariantType.new(reply_type) if reply_type else None,
                           Gio.DBusCallFlags.NONE, 2000, None, callback)

        def fail(e):
            self._loading = False
            self._loaded = False
            DIAG.log("focus_script", loaded=False, reason=reason, err=type(e).__name__)

        def on_unloaded(bus, result, *_user):
            try:
                bus.call_finish(result)
            except GLib.Error:
                pass                    # nothing stale to unload
            if self._bus is None:
                self._loading = False
                return
            call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "loadScript",
                 GLib.Variant("(ss)", (str(self.SCRIPT), self.PLUGIN)), "(i)", on_loaded)

        def on_loaded(bus, result, *_user):
            try:
                script_id = bus.call_finish(result).unpack()[0]
                if script_id < 0:
                    raise GLib.Error(f"loadScript returned {script_id}")
            except GLib.Error as e:
                fail(e)
                return
            if self._bus is None:
                self._loading = False
                return
            call(f"{self.SCRIPTING_PATH}/Script{script_id}", self.SCRIPT_IFACE,
                 "run", None, None, on_run)

        def on_run(bus, result, *_user):
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                fail(e)
                return
            self._loading = False
            self._loaded = True
            DIAG.log("focus_script", loaded=True, reason=reason,
                     ms=(time.monotonic() - t0) * 1000)

        call(self.SCRIPTING_PATH, self.SCRIPTING_IFACE, "unloadScript",
             GLib.Variant("(s)", (self.PLUGIN,)), "(b)", on_unloaded)

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
        """Export the report object and start loading the script.  Returns
        whether the report object is up; the load reports through the log."""
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
        self._load("start")
        # Reload after a KWin restart — the script does not survive one.
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._bus, self.KWIN, Gio.BusNameWatcherFlags.NONE,
            self._on_kwin_appeared, self._on_kwin_vanished)
        # And notice a script that went away without KWin going with it.
        self._timer_id = GLib.timeout_add_seconds(int(self.RECHECK_S), self._recheck)
        return True

    def reload(self) -> bool:
        if not self.enabled or self._bus is None:
            return False
        self._load("reload")
        return True

    def snapshot(self):
        """The latest report, or None when the focused window is unknown.

        Cache only.  This runs on the key-release path, on the GTK main
        thread, and a D-Bus round trip there (0.5 s to notice KWin is not
        answering, several seconds if a reload follows) is exactly what must
        never happen: the pill froze and the release tail waited on it.  The
        script's health is the timer's and the name watcher's business.
        """
        if not self.enabled or self._bus is None:
            return None
        with self._lock:
            latest = self._latest
        if latest is None or not latest.resource_class:
            return None
        return latest

    def stop(self):
        if self._bus is None:
            return
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
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
