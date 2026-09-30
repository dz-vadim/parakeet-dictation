"""Global shortcuts.

Push-to-talk (hold mode) is registered with kglobalaccel, the only path that
grabs a key on KDE Wayland.  The toggle / start-stop / pause bindings still go
through a pynput listener, which only sees keys inside this app's own windows
there; the tray and the SIGUSR1/SIGUSR2 signals are what drive those modes.
"""

import re

from gi.repository import Gio, GLib

from .config import APP_ID, APP_NAME, AppConfig
from .dbus import call_sync
from .diagnostics import DIAG

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

QT_MODIFIER_BITS = {
    "shift": 0x02000000,
    "ctrl": 0x04000000, "control": 0x04000000,
    "alt": 0x08000000,
    "meta": 0x10000000, "super": 0x10000000, "win": 0x10000000,
}

QT_NAMED_KEYS = {
    "escape": 0x01000000, "tab": 0x01000001, "backtab": 0x01000002,
    "backspace": 0x01000003, "return": 0x01000004, "enter": 0x01000005,
    "insert": 0x01000006, "delete": 0x01000007, "pause": 0x01000008,
    "print": 0x01000009, "sysreq": 0x0100000A, "clear": 0x0100000B,
    "home": 0x01000010, "end": 0x01000011, "left": 0x01000012,
    "up": 0x01000013, "right": 0x01000014, "down": 0x01000015,
    "pageup": 0x01000016, "pagedown": 0x01000017, "capslock": 0x01000024,
    "numlock": 0x01000025, "scrolllock": 0x01000026, "menu": 0x01000055,
    "space": 0x20,
}


def qt_key_sequence(binding: str) -> int:
    """Translate a KDE shortcut string ("Meta+Alt+D") into a Qt key int.

    kglobalaccel speaks QKeySequence, not X11 keysyms: modifier bits sit in the
    high bits (Meta 0x10000000, Alt 0x08000000, Ctrl 0x04000000, Shift
    0x02000000) and the key code in the low ones.  Cross-check against a
    shortcut KDE ships: Close Window reads back as 150994995 = 0x09000033 =
    Alt+F4, and Meta+Alt+D is 0x18000044.
    """
    parts = [p.strip() for p in binding.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    *mods, key = parts
    value = 0
    for mod in mods:
        bit = QT_MODIFIER_BITS.get(mod.lower())
        if bit is None:
            raise ValueError(f"unknown modifier {mod!r}")
        value |= bit
    low = key.lower()
    if low in QT_NAMED_KEYS:
        return value | QT_NAMED_KEYS[low]
    m = re.fullmatch(r"f(\d{1,2})", low)
    if m and 1 <= int(m.group(1)) <= 35:
        return value | (0x01000030 + int(m.group(1)) - 1)
    if len(key) == 1:
        return value | ord(key.upper())
    raise ValueError(f"unknown key {key!r}")


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
        return owner

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
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs = []
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
