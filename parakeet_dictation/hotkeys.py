"""Global shortcuts.

Both of the app's shortcuts — hold to talk, and start / stop — are actions
of our own kglobalaccel component, the only path that grabs a key on KDE
Wayland.  They show up (and can be rebound) in System Settings > Shortcuts;
a rebinding made there is reported back through `on_binding_changed`.
"""

from gi.repository import Gio, GLib

from .config import APP_ID, APP_NAME, AppConfig
from .dbus import call_sync
from .diagnostics import DIAG
from .i18n import _
from .shortcuts import binding_from_qt, qt_key_sequence  # noqa: F401  (re-export)

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
    """One action registered directly with kglobalaccel.

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

    # SetPresent (2) | NoAutoloading (4): the keys we pass win over whatever
    # kglobalshortcutsrc last cached for this action.
    SET_FLAGS = 6

    def __init__(self, binding: str, action: str, description: str, on_press,
                 on_release=None):
        self._binding = binding
        self.action = action
        self._on_press = on_press
        self._on_release = on_release
        self._bus = None
        self._action_id = [self.COMPONENT, action, APP_NAME, description]
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

        signals = [("globalShortcutPressed", self._handle_press)]
        if self._on_release is not None:
            signals.append(("globalShortcutReleased", self._handle_release))
        for signal, handler in signals:
            self._subs.append(self._bus.signal_subscribe(
                self.SERVICE, self.COMPONENT_IFACE, signal, path, None,
                Gio.DBusSignalFlags.NONE, handler))
        self.registered = True
        DIAG.log("hold_hotkey_registered", action=self.action, key=hex(key), path=path,
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
            DIAG.log("hold_hotkey_lost", action=self.action, reason="kglobalaccel_vanished")

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
            DIAG.log("hold_hotkey_failed", action=self.action, err=type(e).__name__,
                     stage="reregister")
            return
        DIAG.log("hold_hotkey_reregistered", action=self.action, conflict=owner or "none")

    def _drop_subscriptions(self):
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs = []

    def _handle_press(self, _conn, _sender, _path, _iface, _signal, params):
        if params.unpack()[1] == self.action:
            self._on_press()

    def _handle_release(self, _conn, _sender, _path, _iface, _signal, params):
        if self._on_release is None:
            return
        if params.unpack()[1] == self.action:
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
    """The app's two global shortcuts, both registered with kglobalaccel.

    pynput is gone: on KDE Wayland it could not grab keys outside the app's
    own windows, so its Toggle/Start/Stop/Pause bindings never fired.
    """

    ACTIONS = (
        # (config key, action id, description shown in System Settings)
        ("hotkey_hold", "push-to-talk", "Hold to talk"),
        ("hotkey_toggle", "toggle", "Start / stop dictation"),
    )

    def __init__(self, config: AppConfig, on_hold_press, on_hold_release, on_toggle,
                 on_binding_changed=None):
        self._config = config
        self._callbacks = {
            "push-to-talk": (on_hold_press, on_hold_release),
            "toggle": (on_toggle, None),
        }
        self._on_binding_changed = on_binding_changed
        self._hotkeys = []
        self._changed_sub = 0

    def start(self) -> str:
        problems = []
        for key, action, description in self.ACTIONS:
            binding = getattr(self._config, key)
            if not binding:
                continue
            press, release = self._callbacks[action]
            hotkey = KGlobalAccelHotkey(binding, action, description, press, release)
            try:
                owner = hotkey.register()
            except (GLib.Error, ValueError) as e:
                DIAG.log("hotkey_failed", action=action, err=type(e).__name__)
                problems.append(_("{binding} could not be registered: {error}")
                                .format(binding=binding, error=e))
                continue
            self._hotkeys.append(hotkey)
            if owner:
                DIAG.log("hotkey_conflict", action=action, owner=owner)
                problems.append(_("{binding} is already used by {owner} — change it in "
                                  "Settings or in System Settings > Shortcuts")
                                .format(binding=binding, owner=owner))
        self._watch_changes()
        return "; ".join(problems)

    def _watch_changes(self):
        if self._changed_sub or not self._hotkeys:
            return
        bus = self._hotkeys[0]._bus
        self._changed_bus = bus
        self._changed_sub = bus.signal_subscribe(
            KGlobalAccelHotkey.SERVICE, KGlobalAccelHotkey.ROOT_IFACE,
            "yourShortcutsChanged", KGlobalAccelHotkey.ROOT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_shortcuts_changed)

    def _on_shortcuts_changed(self, _conn, _sender, _path, _iface, _signal, params):
        action_id, keys = params.unpack()
        if len(action_id) < 2 or action_id[0] != KGlobalAccelHotkey.COMPONENT:
            return
        config_key = {a: k for k, a, _d in self.ACTIONS}.get(action_id[1])
        if config_key is None:
            return
        # a(ai): a list of 1-tuples, each holding the key ints of one
        # sequence (up to four chords, unused ones 0).  No keys, or only
        # zeros, is a binding cleared in KDE.
        chords = [k for k in (keys[0][0] if keys and keys[0] else []) if k]
        binding = binding_from_qt(chords[0]) if chords else ""
        if len(chords) > 1 or (chords and not binding):
            # A multi-chord sequence or a key we cannot name (media keys,
            # non-ASCII): saving it would truncate or wipe the user's KDE
            # binding, so the config keeps what it has.
            DIAG.log("hotkey_changed_in_kde", action=action_id[1], unsupported=1)
            return
        if binding == getattr(self._config, config_key):
            return
        DIAG.log("hotkey_changed_in_kde", action=action_id[1])
        if self._on_binding_changed:
            self._on_binding_changed(config_key, binding)

    def stop(self):
        if self._changed_sub:
            self._changed_bus.signal_unsubscribe(self._changed_sub)
            self._changed_sub = 0
        for hotkey in self._hotkeys:
            hotkey.unregister()
        self._hotkeys = []

    def rebuild(self, config: AppConfig) -> str:
        self.stop()
        self._config = config
        return self.start()
