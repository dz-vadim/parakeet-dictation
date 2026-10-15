"""The settings window: sidebar of pages, one draft, OK / Apply / Cancel."""

from gi.repository import Gio, GLib, Gtk

from ... import autostart
from ...config import APP_NAME
from ...i18n import _
from .model import PAGE_KEYS, SettingsDraft
from .widgets import ShortcutButton


class SettingsWindow(Gtk.Window):
    def __init__(self, get_config, profiles_data, on_apply, on_models_changed):
        super().__init__(title=_("{app} — Settings").format(app=APP_NAME))
        self.set_default_size(880, 600)
        self.set_icon_name("parakeet-dictation")
        self._get_config = get_config
        self.profiles_data = profiles_data
        self._on_apply = on_apply
        self.on_models_changed = on_models_changed
        self.draft = SettingsDraft(get_config())
        self._refreshers = []
        self._autostart_initial = autostart.is_enabled()
        self.autostart_wanted = self._autostart_initial
        self.refresh_devices = lambda: None      # the General page installs the real one
        self.connect("delete-event", self._on_delete)
        # Built first: page builders may fire change handlers, which refresh it.
        self.apply_button = Gtk.Button.new_with_mnemonic(_("_Apply"))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        self.info = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, show_close_button=True)
        self.info.set_no_show_all(True)
        self.info_label = Gtk.Label(xalign=0.0)
        self.info_label.set_line_wrap(True)
        self.info.get_content_area().add(self.info_label)
        self.info_label.show()
        self.info.connect("response", lambda bar, _r: bar.hide())
        outer.pack_start(self.info, False, False, 0)

        body = Gtk.Box()
        outer.pack_start(body, True, True, 0)
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_size_request(200, -1)
        self.stack = Gtk.Stack()
        from .pages import page_list
        self._page_ids = []
        for page_id, title, icon, build in page_list():
            row = Gtk.Box(spacing=10, margin=8)
            # Breeze names first; fall back for themes that lack them (Adwaita has no "configure").
            gicon = Gio.ThemedIcon.new_from_names([icon, "preferences-other"])
            row.pack_start(Gtk.Image.new_from_gicon(gicon, Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
            row.pack_start(Gtk.Label(label=title, xalign=0.0), False, False, 0)
            self.sidebar.add(row)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=22)
            heading = Gtk.Label(xalign=0.0)
            heading.set_markup(f"<big><b>{GLib.markup_escape_text(title)}</b></big>")
            page.pack_start(heading, False, False, 0)
            page.pack_start(build(self), False, False, 0)
            scroller.add(page)
            scroller.show_all()
            self.stack.add_named(scroller, page_id)
            self._page_ids.append(page_id)
        self.sidebar.connect("row-selected", self._on_row_selected)
        body.pack_start(self.sidebar, False, False, 0)
        body.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        body.pack_start(self.stack, True, True, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)
        buttons = Gtk.Box(spacing=8, margin=10)
        defaults = Gtk.Button.new_with_mnemonic(_("_Defaults"))
        defaults.connect("clicked", lambda _b: self.defaults())
        buttons.pack_start(defaults, False, False, 0)
        ok = Gtk.Button.new_with_mnemonic(_("_OK"))
        ok.get_style_context().add_class("suggested-action")
        ok.connect("clicked", lambda _b: self.apply() and self.hide())
        self.apply_button.connect("clicked", lambda _b: self.apply())
        cancel = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        cancel.connect("clicked", lambda _b: self.cancel())
        for b in (ok, self.apply_button, cancel):
            buttons.pack_end(b, False, False, 0)
        outer.pack_start(buttons, False, False, 0)
        # present() maps the toplevel only; the content must be visible
        # already.  The info bar and the download progress are no_show_all,
        # so they stay hidden until they have something to say.
        outer.show_all()
        self.show_page("general")
        self._refresh_buttons()

    # -- pages ---------------------------------------------------------------
    def show_page(self, page_id: str):
        self.sidebar.select_row(self.sidebar.get_row_at_index(self._page_ids.index(page_id)))
        self.stack.set_visible_child_name(page_id)

    def _on_row_selected(self, _box, row):
        if row is not None:
            self.stack.set_visible_child_name(self._page_ids[row.get_index()])

    @property
    def current_page(self) -> str:
        return self.stack.get_visible_child_name()

    # -- bindings ------------------------------------------------------------
    def _changed(self, key, value):
        self.draft.set(key, value)
        self._refresh_buttons()

    def bind_check(self, key, label):
        w = Gtk.CheckButton(label=label)
        w.set_active(bool(self.draft.get(key)))
        w.connect("toggled", lambda b: self._changed(key, b.get_active()))
        self._refreshers.append(lambda: w.set_active(bool(self.draft.get(key))))
        return w

    def bind_combo(self, key, choices):
        w = Gtk.ComboBoxText()
        for cid, label in choices:
            w.append(cid, label)
        w.set_active_id(str(self.draft.get(key)))
        w.connect("changed", lambda c: c.get_active_id() is not None
                  and self._changed(key, c.get_active_id()))
        self._refreshers.append(lambda: w.set_active_id(str(self.draft.get(key))))
        return w

    def bind_spin(self, key, lo, hi, step, digits=0):
        w = Gtk.SpinButton.new_with_range(lo, hi, step)
        w.set_digits(digits)
        w.set_value(self.draft.get(key))
        cast = float if digits else int
        w.connect("value-changed", lambda s: self._changed(key, cast(s.get_value())))
        self._refreshers.append(lambda: w.set_value(self.draft.get(key)))
        return w

    def bind_radio(self, key, choices):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        group, buttons = None, {}
        for cid, label in choices:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = group or rb
            buttons[cid] = rb
            rb.connect("toggled", lambda b, c=cid: b.get_active() and self._changed(key, c))
            box.pack_start(rb, False, False, 0)
        buttons.get(self.draft.get(key), group).set_active(True)
        self._refreshers.append(lambda: buttons.get(self.draft.get(key), group).set_active(True))
        return box

    def bind_shortcut(self, key):
        w = ShortcutButton(self.draft.get(key), lambda b: self._changed(key, b))
        self._refreshers.append(lambda: w.set_binding(self.draft.get(key)))
        return w

    def refresh_from_config(self):
        """Re-read everything that may have changed while the window was hidden."""
        self.draft.reload(self._get_config())
        self._autostart_initial = autostart.is_enabled()
        self.autostart_wanted = self._autostart_initial
        self.refresh_devices()
        self.info.hide()
        self.reload_widgets()

    def reload_widgets(self):
        for refresh in self._refreshers:
            refresh()
        self._refresh_buttons()

    # -- buttons -------------------------------------------------------------
    def _refresh_buttons(self):
        dirty = self.draft.dirty or self.autostart_wanted != self._autostart_initial
        self.apply_button.set_sensitive(dirty)

    def _problem(self, text):
        self.info_label.set_text(text)
        self.info.show()

    def _validate(self) -> str:
        hold, toggle = self.draft.get("hotkey_hold"), self.draft.get("hotkey_toggle")
        if toggle and toggle == hold:
            return _("{key} is already used for Hold to talk — choose another "
                     "Start / stop shortcut.").format(key=toggle)
        return ""

    def apply(self) -> bool:
        problem = self._validate()
        if problem:
            self._problem(problem)
            return False
        self.info.hide()
        ok = True
        problems = []
        if self.autostart_wanted != self._autostart_initial:
            try:
                autostart.set_enabled(self.autostart_wanted)
                self._autostart_initial = self.autostart_wanted
            except OSError as e:
                self.autostart_wanted = self._autostart_initial
                ok = False
                problems.append(_("Could not change Start at login: {error}")
                                .format(error=e.strerror or e))
        if self.draft.dirty:
            before = self.draft.state()
            new = self.draft.commit(self._get_config())
            saved, problem = self._on_apply(new)
            if saved:
                self.draft.reload(self._get_config())
            else:
                # Not written to disk: keep the edits pending so Apply retries.
                self.draft.restore(before)
                ok = False
            if problem:
                problems.append(problem)
        if problems:
            self._problem("\n".join(problems))
        self.reload_widgets()
        return ok

    def cancel(self):
        self.draft.reload(self._get_config())
        self.autostart_wanted = self._autostart_initial
        self.info.hide()
        self.reload_widgets()
        self.hide()

    def defaults(self):
        self.draft.reset_page(self.current_page)
        self.reload_widgets()

    def _on_delete(self, *_a):
        self.cancel()
        return True          # keep the single instance; hide instead of destroy
