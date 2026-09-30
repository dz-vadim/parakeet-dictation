"""Single-instance guard: one well-known session-bus name per desktop session.

Two copies of the app both register the push-to-talk component with
kglobalaccel and both act on every press — two takes per hold, two pastes (it
happened during this project).  So the second copy must refuse to start.
Owning a D-Bus name is the lock: the bus releases it the instant the owning
process dies, so there is no stale lock file to clean up after a crash, and
the owner's pid can be asked for so the refusal can say who is running.

The name is requested with DO_NOT_QUEUE through the bus driver's RequestName,
synchronously, before anything else is set up: Gio.bus_own_name() answers
through main-loop callbacks, which would mean building the whole app first.
"""

from gi.repository import GLib

from .dbus import call_sync, session_bus
from .diagnostics import DIAG

BUS_NAME = "org.kde.parakeet.Dictation"

_DRIVER = "org.freedesktop.DBus"
_DRIVER_PATH = "/org/freedesktop/DBus"
_FLAG_DO_NOT_QUEUE = 4
_REPLY_PRIMARY_OWNER = 1
_REPLY_ALREADY_OWNER = 4


class InstanceLock:
    """Own `name` on the session bus for the life of this process."""

    def __init__(self, name: str = BUS_NAME, bus=None):
        self.name = name
        self._bus = bus
        self.owner_pid = 0
        self.held = False

    def _driver(self, method, params, reply_type, timeout_ms=3000):
        return call_sync(self._bus, _DRIVER, _DRIVER_PATH, _DRIVER, method, params,
                         reply_type, timeout_ms).unpack()

    def acquire(self) -> bool:
        """True when this process now owns the name.

        False when another process does; `owner_pid` then names it (0 if the
        bus would not say).  With no session bus at all there is nothing to
        own and nothing to collide with either (no bus, no kglobalaccel), so
        that case logs and returns True rather than refusing to start.
        """
        try:
            if self._bus is None:
                self._bus = session_bus()
            reply = self._driver("RequestName", GLib.Variant("(su)", (self.name, _FLAG_DO_NOT_QUEUE)),
                                 "(u)")[0]
        except GLib.Error as e:
            DIAG.log("instance_lock", name=self.name, acquired=False,
                     reason="no_session_bus", err=type(e).__name__)
            return True
        if reply in (_REPLY_PRIMARY_OWNER, _REPLY_ALREADY_OWNER):
            self.held = True
            DIAG.log("instance_lock", name=self.name, acquired=True)
            return True
        self.owner_pid = self._owner_pid()
        DIAG.log("already_running", name=self.name, owner_pid=self.owner_pid)
        return False

    def _owner_pid(self) -> int:
        try:
            owner = self._driver("GetNameOwner", GLib.Variant("(s)", (self.name,)), "(s)")[0]
            return int(self._driver("GetConnectionUnixProcessID",
                                    GLib.Variant("(s)", (owner,)), "(u)")[0])
        except (GLib.Error, ValueError):
            return 0

    def release(self):
        """Give the name back (tests; the bus does this itself at exit)."""
        if not self.held or self._bus is None:
            return
        try:
            self._driver("ReleaseName", GLib.Variant("(s)", (self.name,)), "(u)")
        except GLib.Error:
            pass
        self.held = False
