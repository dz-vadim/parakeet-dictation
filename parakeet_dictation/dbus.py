"""Session-bus helpers shared by the D-Bus clients (instance lock, KWin focus
script, kglobalaccel, RemoteDesktop portal).

A leaf module: it imports nothing from the package, so any module may use it.
"""

from gi.repository import Gio, GLib


def session_bus():
    """The process's shared session-bus connection."""
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def call_sync(bus, service, path, iface, method, params, reply_type=None,
              timeout_ms=5000):
    """One blocking method call.  `reply_type` is a GVariant type string
    such as "(b)", or None to accept whatever comes back."""
    return bus.call_sync(
        service, path, iface, method, params,
        GLib.VariantType.new(reply_type) if reply_type else None,
        Gio.DBusCallFlags.NONE, timeout_ms, None)
