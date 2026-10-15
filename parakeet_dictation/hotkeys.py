"""Global shortcuts.

Push-to-talk (hold mode) is registered with kglobalaccel, the only path that
grabs a key on KDE Wayland.  The toggle / start-stop / pause bindings still go
through a pynput listener, which only sees keys inside this app's own windows
there; the tray and the SIGUSR1/SIGUSR2 signals are what drive those modes.
"""

from gi.repository import Gio, GLib

from .config import APP_ID, APP_NAME, AppConfig
from .dbus import call_sync
from .diagnostics import DIAG
from .shortcuts import qt_key_sequence  # noqa: F401  (re-export)

# ---------------------------------------------------------------------------
# Global shortcuts via kglobalaccel
#
# A `[services]` entry in kglobalshortcutsrc can only ever fire once per press:
# it *launches* a .desktop file, and a launch has no counterpart when the key
# comes back up.  Registering a component of our own with kglobalaccel gives
# both globalShortcutPressed and globalShortcutReleased on the same binding,
# which is what push-to-talk needs.  No relogin is involved — that constraint
# belongs to hand-edited kglobalshortcutsrc entries, not to this path.
# ---------------------------------------------------------------------------


class KGlobalAccelHotkey:
    """One push-to-talk action registered directly with kglobalaccel.

    Gio.DBusConnection rather than dbus-python so the QKeySequence argument —
    `a(ai)`, a set of sequences each holding up to four key ints — is typed
    exactly; a loose binding marshals it as a plain array and the call fails.
    Signals arrive on whatever main context is thread-default at subscribe
    time, so this must be built on the GTK main loop.
    """

    SERVICE = "org.kde.kglobalaccel"
    ROOT_PATH = "/kglobalaccel"
    ROOT_IFACE = "org.kde.KGlobalAccel"
    COMPONENT_IFACE = "org.kde.kglobalaccel.Component"
    COMPONENT = APP_ID
    ACTION = "push-to-talk"

    # SetPresent (2) | NoAutoloading (4): the keys we pass win over whatever
    # kglobalshortcutsrc last cached for this action.
    SET_FLAGS = 6

    def __init__(self, binding: str, on_press, on_release):
        self._binding = binding
        self._on_press = on_press
        self._on_release = on_release
        self._bus = None
        self._action_id = [self.COMPONENT, self.ACTION, APP_NAME,
                           "Push-to-talk dictation"]
        self._subs = []
        self._watch_id = 0
        self._vanished = False
        self.registered = False

    def _call(self, path, iface, method, params, reply_type=None):
        return call_sync(self._bus, self.SERVICE, path, iface, method, params,
                         reply_type, 5000)

    def _owner_of(self, key: int) -> str:
        """Unique name of the component already holding `key`, if any."""
        try:
            infos = self._call(self.ROOT_PATH, self.ROOT_IFACE,
                               "getGlobalShortcutsByKey",
                               GLib.Variant("(i)", (key,)),
                               "(a(ssssssaiai))").unpack()[0]
        except GLib.Error:
            return "unknown"
        for info in infos:
            if info[2] != self.COMPONENT:
                return info[2]
        return ""

    def register(self) -> str:
        """Register, bind the key and subscribe.  Returns "" when clean.

        A non-empty return names the component that already owns the key.
        kglobalaccel accepts the registration either way and the action shows
        up in System Settings, but only the first claimant is actually handed
        the key — verified here with injected key events — so a conflict has
        to be reported rather than assumed harmless.
        """
        key = qt_key_sequence(self._binding)
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        available = self._call(
            self.ROOT_PATH, self.ROOT_IFACE, "globalShortcutAvailable",
            GLib.Variant("((ai)s)", (([key],), self.COMPONENT)), "(b)").unpack()[0]
        owner = "" if available else self._owner_of(key)

        self._call(self.ROOT_PATH, self.ROOT_IFACE, "doRegister",
                   GLib.Variant("(as)", (self._action_id,)))
        self._call(self.ROOT_PATH, self.ROOT_IFACE, "setShortcutKeys",
                   GLib.Variant("(asa(ai)u)",
                                (self._action_id, [([key],)], self.SET_FLAGS)),
                   "(a(ai))")
        path = self._call(self.ROOT_PATH, self.ROOT_IFACE, "getComponent",
                          GLib.Variant("(s)", (self.COMPONENT,)), "(o)").unpack()[0]

        for signal, handler in (("globalShortcutPressed", self._handle_press),
                                ("globalShortcutReleased", self._handle_release)):
            self._subs.append(self._bus.signal_subscribe(
                self.SERVICE, self.COMPONENT_IFACE, signal, path, None,
                Gio.DBusSignalFlags.NONE, handler))
        self.registered = True
        DIAG.log("hold_hotkey_registered", key=hex(key), path=path,
                 conflict=owner or "none")
        if not self._watch_id:
            # kglobalaccel forgets every component when it restarts (a
            # Plasma crash, `kquitapp6 kglobalaccel`); without this the key
            # silently stopped working until the app was restarted.
            self._watch_id = Gio.bus_watch_name_on_connection(
                self._bus, self.SERVICE, Gio.BusNameWatcherFlags.NONE,
                self._on_service_appeared, self._on_service_vanished)
        return owner

    def _on_service_vanished(self, _conn, _name):
        if self.registered:
            self._vanished = True
            DIAG.log("hold_hotkey_lost", reason="kglobalaccel_vanished")

    def _on_service_appeared(self, _conn, _name, _owner):
        # The watch reports the name as soon as it is armed; only a
        # re-appearance after a loss needs a new registration.
        if not self._vanished:
            return
        self._vanished = False
        self._drop_subscriptions()
        try:
            owner = self.register()
        except (GLib.Error, ValueError) as e:
            self.registered = False
            DIAG.log("hold_hotkey_failed", err=type(e).__name__, stage="reregister")
            return
        DIAG.log("hold_hotkey_reregistered", conflict=owner or "none")

    def _drop_subscriptions(self):
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs = []

    def _handle_press(self, _conn, _sender, _path, _iface, _signal, params):
        if params.unpack()[1] == self.ACTION:
            self._on_press()

    def _handle_release(self, _conn, _sender, _path, _iface, _signal, params):
        if params.unpack()[1] == self.ACTION:
            self._on_release()

    def unregister(self):
        # signal_unsubscribe, and not inside a blanket except: the previous
        # spelling named a method Gio.DBusConnection does not have, the
        # AttributeError was swallowed, and every rebuild() leaked its two
        # handlers — after N settings saves each key press fired N+1 times.
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        self._vanished = False
        self._drop_subscriptions()
        if self.registered:
            try:
                self._call(self.ROOT_PATH, self.ROOT_IFACE, "unRegister",
                           GLib.Variant("(as)", (self._action_id,)))
            except GLib.Error:
                pass
            self.registered = False


# ---------------------------------------------------------------------------
# Hotkey manager
# ---------------------------------------------------------------------------

class HotkeyManager:
    def __init__(self, config: AppConfig, on_toggle, on_start, on_stop, on_pause,
                 on_hold_press=None, on_hold_release=None):
        self._config = config
        self._on_toggle = on_toggle
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_pause = on_pause
        self._on_hold_press = on_hold_press or (lambda: None)
        self._on_hold_release = on_hold_release or (lambda: None)
        self._listener = None
        self._hold = None

    def start(self) -> str:
        """Bind the configured hotkeys.  Returns "" or a problem to show.

        Only hold mode goes through kglobalaccel; the other modes keep the
        pynput listener plus the SIGUSR1 path they have always used.
        """
        status = ""
        if self._config.hotkey_mode == "hold":
            status = self._start_hold()

        from pynput import keyboard
        bindings = {}
        if self._config.hotkey_mode == "toggle":
            bindings[self._config.hotkey_toggle] = lambda: GLib.idle_add(self._on_toggle)
        elif self._config.hotkey_mode == "start_stop":
            bindings[self._config.hotkey_start] = lambda: GLib.idle_add(self._on_start)
            bindings[self._config.hotkey_stop] = lambda: GLib.idle_add(self._on_stop)

        if self._config.hotkey_pause:
            bindings[self._config.hotkey_pause] = lambda: GLib.idle_add(self._on_pause)

        # GlobalHotKeys raises ValueError on the first binding it cannot
        # parse ("Meta+Z" typed into a pynput field), which used to take the
        # whole app down at every launch.  A bad one is dropped and named.
        bad = []
        for binding in list(bindings):
            try:
                keyboard.HotKey.parse(binding)
            except ValueError:
                bad.append(binding)
                del bindings[binding]
        if bad:
            DIAG.log("hotkey_invalid", count=len(bad))
            problem = (f"{', '.join(bad)} is not a valid shortcut — "
                       f"set it again in Settings")
            status = f"{status}; {problem}" if status else problem

        self._listener = keyboard.GlobalHotKeys(bindings)
        self._listener.daemon = True
        self._listener.start()
        return status

    def _start_hold(self) -> str:
        binding = self._config.hotkey_hold
        self._hold = KGlobalAccelHotkey(binding, self._on_hold_press,
                                        self._on_hold_release)
        try:
            owner = self._hold.register()
        except (GLib.Error, ValueError) as e:
            DIAG.log("hold_hotkey_failed", err=type(e).__name__)
            self._hold = None
            return f"Push-to-talk unavailable: {e}"
        if owner:
            DIAG.log("hold_hotkey_conflict", owner=owner)
            return (f"{binding} is already held by {owner} — "
                    f"free it in System Settings > Shortcuts")
        return ""

    def stop(self):
        if self._hold:
            self._hold.unregister()
            self._hold = None
        if self._listener:
            self._listener.stop()
            self._listener = None

    def rebuild(self, config: AppConfig) -> str:
        self._config = config
        self.stop()
        return self.start()
