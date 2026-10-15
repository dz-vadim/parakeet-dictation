"""Building blocks shared by the settings pages."""

from gi.repository import Gdk, GLib, Gtk

from ... import shortcuts
from ...i18n import _


class Section(Gtk.Box):
    """A bold heading and a two-column grid: right-aligned labels, controls."""

    def __init__(self, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(xalign=0.0)
        heading.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        heading.set_margin_top(14)
        self.pack_start(heading, False, False, 0)
        self._grid = Gtk.Grid(column_spacing=14, row_spacing=8, margin_start=12)
        self.pack_start(self._grid, False, False, 0)
        self._row = 0

    def row(self, label, widget, hint=None):
        if label:
            lb = Gtk.Label(label=label, xalign=1.0)
            lb.set_size_request(230, -1)
            lb.set_mnemonic_widget(widget)
            self._grid.attach(lb, 0, self._row, 1, 1)
        widget.set_halign(Gtk.Align.START)
        self._grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        if hint:
            h = Gtk.Label(label=hint, xalign=0.0)
            h.set_line_wrap(True)
            h.set_max_width_chars(60)
            h.get_style_context().add_class("dim-label")
            self._grid.attach(h, 1, self._row, 1, 1)
            self._row += 1
        return widget

    def add_full(self, widget):
        self._grid.attach(widget, 0, self._row, 2, 1)
        self._row += 1
        return widget


def gdk_mods(state) -> frozenset:
    mods = set()
    if state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
                | Gdk.ModifierType.MOD4_MASK):
        mods.add("Meta")
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods.add("Ctrl")
    if state & Gdk.ModifierType.MOD1_MASK:
        mods.add("Alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods.add("Shift")
    return frozenset(mods)


class ShortcutButton(Gtk.Button):
    """Click, then press a shortcut.  Esc cancels, Backspace disables."""

    def __init__(self, binding: str, on_change):
        super().__init__()
        self.set_size_request(170, -1)
        self._binding = binding
        self._on_change = on_change
        self._listening = False
        self._handler = 0
        self._show()
        self.connect("clicked", self._on_clicked)

    @property
    def binding(self) -> str:
        return self._binding

    def set_binding(self, binding: str):
        self._binding = binding
        self._show()

    def _show(self):
        self.set_label(shortcuts.display(self._binding) or _("None"))

    def _on_clicked(self, _btn):
        if self._listening:
            return
        self._listening = True
        self.set_label(_("Press a shortcut…"))
        self._handler = self.connect("key-press-event", self._on_key)
        self.grab_focus()

    def _on_key(self, _widget, event):
        name = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval)) or ""
        self.feed(name, gdk_mods(event.state))
        return True

    def feed(self, keyname: str, mods: frozenset) -> str:
        if not self._listening:
            return "idle"
        status, binding = shortcuts.capture(keyname, mods)
        if status in ("wait", "reject"):
            return status
        self._listening = False
        if self._handler:
            self.disconnect(self._handler)
            self._handler = 0
        if status == "done":
            self._binding = binding
            self._on_change(binding)
        elif status == "clear":
            self._binding = ""
            self._on_change("")
        self._show()
        return status


def device_choices(saved: str, names: list) -> list:
    out = [("", _("System default"))] + [(n, n) for n in names]
    if saved and not saved.isdigit() and saved not in names:
        out.append((saved, _("{name} (not connected)").format(name=saved)))
    return out
