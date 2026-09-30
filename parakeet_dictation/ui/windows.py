"""Main window, settings dialog, first-run welcome dialog, hotkey capture button."""

import sys

from gi.repository import Gdk, GLib, Gtk

from .. import audio
from ..audio import list_input_devices
from ..config import APP_NAME, AppConfig
from ..controller import DictationController
from ..hotkeys import HotkeyManager, qt_key_sequence
from ..models import _download_all_models, _download_model, _is_model_downloaded

# ---------------------------------------------------------------------------
# Hotkey capture widget
# ---------------------------------------------------------------------------

class HotkeyCaptureButton(Gtk.Button):
    def __init__(self, current_binding: str):
        super().__init__(label=self._display(current_binding))
        self._binding = current_binding
        self._capturing = False
        self._key_handler = None
        self.connect("clicked", self._on_clicked)

    @property
    def binding(self) -> str:
        return self._binding

    @staticmethod
    def _display(binding: str) -> str:
        return binding.replace("<", "").replace(">", "").replace("+", " + ").title()

    def _on_clicked(self, _btn):
        if self._capturing:
            return
        self._capturing = True
        self.set_label("Press a key combo...")
        self._key_handler = self.get_toplevel().connect("key-press-event", self._on_key)

    def _on_key(self, _widget, event):
        if not self._capturing:
            return False
        self._capturing = False
        self.get_toplevel().disconnect(self._key_handler)

        parts = []
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            parts.append("<ctrl>")
        if event.state & Gdk.ModifierType.MOD1_MASK:
            parts.append("<alt>")
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            parts.append("<shift>")

        keyname = Gdk.keyval_name(event.keyval).lower()
        if keyname in ("control_l", "control_r", "alt_l", "alt_r",
                       "shift_l", "shift_r", "super_l", "super_r",
                       "meta_l", "meta_r"):
            self.set_label(self._display(self._binding))
            return True

        parts.append(keyname)
        self._binding = "+".join(parts)
        self.set_label(self._display(self._binding))
        return True


# ---------------------------------------------------------------------------
# Settings dialog (tabbed: Models, Hotkeys, About)
# ---------------------------------------------------------------------------

LANG_LABELS = {"en": "English", "es": "Spanish", "de": "German", "fr": "French"}


class WelcomeDialog(Gtk.Dialog):
    """First-run dialog — downloads all models automatically."""

    def __init__(self, profiles_data: dict, config: AppConfig, on_model_ready):
        super().__init__(title=f"Welcome to {APP_NAME}", flags=0)
        self._profiles_data = profiles_data
        self._profiles = profiles_data["profiles"]
        self._config = config
        self._on_model_ready = on_model_ready
        self.set_default_size(440, 280)
        self.set_deletable(False)

        box = self.get_content_area()
        box.set_spacing(12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)

        header = Gtk.Label()
        header.set_markup(
            f"<span size='x-large' weight='bold'>Welcome to {APP_NAME}</span>"
        )
        header.set_halign(Gtk.Align.START)
        box.pack_start(header, False, False, 0)

        total_mb = sum(m["size_mb"] for m in self._profiles.values())
        subtitle = Gtk.Label()
        subtitle.set_markup(
            f"Three speech recognition models will be downloaded\n"
            f"so you can switch between them freely.\n\n"
            f"Total download: <b>~{total_mb} MB</b>"
        )
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_line_wrap(True)
        box.pack_start(subtitle, False, False, 0)

        # Model summary (read-only)
        for mid, mdata in self._profiles.items():
            lbl = Gtk.Label()
            tag = "Streaming" if mdata.get("streaming") else "VAD-segmented"
            lbl.set_markup(
                f"  \u2022 <b>{mdata['name']}</b>  ({mdata['size_mb']} MB, {tag})"
            )
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(lbl, False, False, 0)

        # Download button
        self._dl_btn = Gtk.Button()
        dl_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dl_hbox.set_halign(Gtk.Align.CENTER)
        dl_hbox.pack_start(
            Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        dl_hbox.pack_start(Gtk.Label(label="Download All Models"), False, False, 0)
        self._dl_btn.add(dl_hbox)
        self._dl_btn.get_style_context().add_class("suggested-action")
        self._dl_btn.set_margin_top(8)
        self._dl_btn.connect("clicked", self._on_download_all)
        box.pack_start(self._dl_btn, False, False, 0)

        # Progress bar (hidden until download starts)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_no_show_all(True)
        box.pack_start(self._progress_bar, False, False, 0)

        # Error label (hidden until error)
        self._error_label = Gtk.Label()
        self._error_label.set_line_wrap(True)
        self._error_label.set_max_width_chars(60)
        self._error_label.set_halign(Gtk.Align.START)
        self._error_label.set_no_show_all(True)
        box.pack_start(self._error_label, False, False, 0)

        self.show_all()

    def _update_progress(self, msg, fraction):
        self._progress_bar.set_text(msg)
        if fraction >= 0:
            self._progress_bar.set_fraction(min(fraction, 1.0))
        else:
            self._progress_bar.pulse()

    def _on_download_all(self, btn):
        btn.set_sensitive(False)
        self._progress_bar.show()
        self._error_label.hide()

        def on_progress(msg, fraction):
            self._update_progress(msg, fraction)

        def on_done(success, err):
            if success:
                self._config.model_profile = "desktop"
                self.destroy()
                if self._on_model_ready:
                    self._on_model_ready(self._config)
            else:
                btn.set_sensitive(True)
                self._progress_bar.hide()
                self._error_label.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._error_label.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_all_models(self._profiles_data, on_progress, on_done)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, config: AppConfig, profiles_data: dict, on_save):
        super().__init__(title=f"{APP_NAME} — Settings", flags=0)
        self._config = config
        self._profiles_data = profiles_data
        self._profiles = profiles_data["profiles"]
        self._on_save = on_save
        self.set_default_size(520, 560)

        notebook = Gtk.Notebook()
        self.get_content_area().pack_start(notebook, True, True, 0)

        notebook.append_page(self._build_models_tab(), Gtk.Label(label="Models"))
        notebook.append_page(self._build_hotkeys_tab(), Gtk.Label(label="Hotkeys"))
        notebook.append_page(self._build_general_tab(), Gtk.Label(label="General"))
        notebook.append_page(self._build_about_tab(), Gtk.Label(label="About"))

        self.show_all()

    # --- Models tab ---

    def _build_models_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        self._model_status_label = Gtk.Label()
        self._model_status_label.set_halign(Gtk.Align.START)
        box.pack_start(self._model_status_label, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._model_list = Gtk.ListBox()
        self._model_list.set_selection_mode(Gtk.SelectionMode.NONE)
        sw.add(self._model_list)
        box.pack_start(sw, True, True, 0)

        # Download All button
        self._dl_all_btn = Gtk.Button()
        dl_all_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dl_all_hbox.set_halign(Gtk.Align.CENTER)
        dl_all_hbox.pack_start(
            Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        total_mb = sum(m["size_mb"] for m in self._profiles.values())
        self._dl_all_label = Gtk.Label(label=f"Download All Models ({total_mb} MB)")
        dl_all_hbox.pack_start(self._dl_all_label, False, False, 0)
        self._dl_all_btn.add(dl_all_hbox)
        self._dl_all_btn.connect("clicked", self._on_download_all)
        box.pack_start(self._dl_all_btn, False, False, 0)

        # Progress bar (hidden until download starts)
        self._dl_progress = Gtk.ProgressBar()
        self._dl_progress.set_show_text(True)
        self._dl_progress.set_no_show_all(True)
        box.pack_start(self._dl_progress, False, False, 0)

        # Error label (hidden until error)
        self._dl_error = Gtk.Label()
        self._dl_error.set_line_wrap(True)
        self._dl_error.set_max_width_chars(60)
        self._dl_error.set_halign(Gtk.Align.START)
        self._dl_error.set_no_show_all(True)
        box.pack_start(self._dl_error, False, False, 0)

        self._populate_models()
        return box

    def _populate_models(self):
        for child in self._model_list.get_children():
            self._model_list.remove(child)

        active = self._config.model_profile
        self._model_status_label.set_markup(
            f"Active: <b>{self._profiles.get(active, {}).get('name', active)}</b>"
        )

        for mid, mdata in self._profiles.items():
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)

            # Info column
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name_label = Gtk.Label()
            name_label.set_markup(f"<b>{mdata['name']}</b>")
            name_label.set_halign(Gtk.Align.START)
            vbox.pack_start(name_label, False, False, 0)

            desc = mdata.get("description", "")
            desc_label = Gtk.Label(label=desc)
            desc_label.set_halign(Gtk.Align.START)
            desc_label.set_line_wrap(True)
            desc_label.set_max_width_chars(50)
            desc_label.get_style_context().add_class("dim-label")
            vbox.pack_start(desc_label, False, False, 0)

            rec = mdata.get("recommended_for", "")
            hw = mdata.get("hardware_label", "CPU")
            langs = mdata.get("languages")
            tag_parts = [f"{mdata['params']} params", f"{mdata['size_mb']} MB", hw]
            if rec:
                tag_parts.append(rec)
            if langs:
                tag_parts.append("/".join(l.upper() for l in langs))
            if mdata.get("streaming"):
                tag_parts.append("Streaming")
            tag_label = Gtk.Label()
            tag_label.set_markup(f"<small>{' · '.join(tag_parts)}</small>")
            tag_label.set_halign(Gtk.Align.START)
            tag_label.get_style_context().add_class("dim-label")
            vbox.pack_start(tag_label, False, False, 0)

            hbox.pack_start(vbox, True, True, 0)

            # Buttons column
            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            btn_box.set_valign(Gtk.Align.CENTER)

            downloaded = _is_model_downloaded(mid, self._profiles)
            is_active = (mid == active)

            if downloaded:
                if is_active:
                    active_label = Gtk.Label(label="Active")
                    active_label.get_style_context().add_class("dim-label")
                    btn_box.pack_start(active_label, False, False, 0)
                else:
                    use_btn = Gtk.Button(label="Use")
                    use_btn.connect("clicked", self._on_use_model, mid)
                    btn_box.pack_start(use_btn, False, False, 0)
            else:
                dl_btn = Gtk.Button()
                dl_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                dl_hbox.pack_start(
                    Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
                    False, False, 0,
                )
                dl_hbox.pack_start(Gtk.Label(label="Download"), False, False, 0)
                dl_btn.add(dl_hbox)
                dl_btn.connect("clicked", self._on_download_model, mid, dl_btn)
                btn_box.pack_start(dl_btn, False, False, 0)

            hbox.pack_end(btn_box, False, False, 0)
            row.add(hbox)
            self._model_list.add(row)

        self._model_list.show_all()

    def _on_use_model(self, _btn, model_id):
        self._config.model_profile = model_id
        if self._on_save:
            self._on_save(self._config)
        self._populate_models()

    def _update_dl_progress(self, msg, fraction):
        self._dl_progress.set_text(msg)
        if fraction >= 0:
            self._dl_progress.set_fraction(min(fraction, 1.0))
        else:
            self._dl_progress.pulse()

    def _on_download_model(self, _btn, model_id, btn_widget):
        btn_widget.set_sensitive(False)
        self._dl_progress.show()
        self._dl_error.hide()

        def on_progress(msg, fraction):
            self._update_dl_progress(msg, fraction)

        def on_done(success, err):
            self._dl_progress.hide()
            if success:
                self._populate_models()
            else:
                btn_widget.set_sensitive(True)
                self._dl_error.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._dl_error.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_model(model_id, self._profiles_data, on_progress, on_done)

    def _on_download_all(self, _btn):
        self._dl_all_btn.set_sensitive(False)
        self._dl_progress.show()
        self._dl_error.hide()

        def on_progress(msg, fraction):
            self._update_dl_progress(msg, fraction)

        def on_done(success, err):
            self._dl_progress.hide()
            if success:
                self._dl_all_label.set_text("All models downloaded")
                self._populate_models()
            else:
                self._dl_all_btn.set_sensitive(True)
                self._dl_error.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._dl_error.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_all_models(self._profiles_data, on_progress, on_done)

    # --- Hotkeys tab ---

    def _build_hotkeys_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # Mode
        self._mode_toggle = Gtk.RadioButton.new_with_label(
            None, "Toggle (one key starts and stops)")
        self._mode_startstop = Gtk.RadioButton.new_with_label_from_widget(
            self._mode_toggle, "Start/Stop (separate keys)")
        self._mode_hold = Gtk.RadioButton.new_with_label_from_widget(
            self._mode_toggle, "Push-to-talk (dictate only while the key is held)")
        if self._config.hotkey_mode == "start_stop":
            self._mode_startstop.set_active(True)
        elif self._config.hotkey_mode == "hold":
            self._mode_hold.set_active(True)
        # Fires on both the way in and the way out of hold mode.
        self._mode_hold.connect("toggled", self._reflect_hold_mode)
        box.pack_start(self._mode_toggle, False, False, 0)
        box.pack_start(self._mode_startstop, False, False, 4)
        box.pack_start(self._mode_hold, False, False, 4)

        hold_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hold_box.set_margin_start(24)
        hold_box.pack_start(Gtk.Label(label="Hold key:", halign=Gtk.Align.END),
                            False, False, 0)
        self._hold_entry = Gtk.Entry()
        self._hold_entry.set_text(self._config.hotkey_hold)
        self._hold_entry.set_width_chars(16)
        self._hold_entry.set_tooltip_text(
            "KDE shortcut syntax, e.g. Meta+Alt+D.  Push-to-talk registers "
            "this with kglobalaccel itself, which is the only route that "
            "reports the key being released.")
        hold_box.pack_start(self._hold_entry, False, False, 0)
        self._hold_hint = Gtk.Label()
        self._hold_hint.get_style_context().add_class("dim-label")
        hold_box.pack_start(self._hold_hint, False, False, 0)
        box.pack_start(hold_box, False, False, 0)

        # Bindings
        hint = Gtk.Label(label="Click a button, then press your desired key combo.")
        hint.set_halign(Gtk.Align.START)
        hint.set_margin_top(8)
        box.pack_start(hint, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_top(4)

        grid.attach(Gtk.Label(label="Toggle:", halign=Gtk.Align.END), 0, 0, 1, 1)
        self._hk_toggle = HotkeyCaptureButton(self._config.hotkey_toggle)
        grid.attach(self._hk_toggle, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Start:", halign=Gtk.Align.END), 0, 1, 1, 1)
        self._hk_start = HotkeyCaptureButton(self._config.hotkey_start)
        grid.attach(self._hk_start, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Stop:", halign=Gtk.Align.END), 0, 2, 1, 1)
        self._hk_stop = HotkeyCaptureButton(self._config.hotkey_stop)
        grid.attach(self._hk_stop, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Pause:", halign=Gtk.Align.END), 0, 3, 1, 1)
        self._hk_pause = HotkeyCaptureButton(self._config.hotkey_pause)
        grid.attach(self._hk_pause, 1, 3, 1, 1)

        box.pack_start(grid, False, False, 0)

        # Save
        save_btn = Gtk.Button(label="Save Hotkeys")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._save_hotkeys)
        save_btn.set_margin_top(12)
        box.pack_start(save_btn, False, False, 0)

        return box

    def _save_hotkeys(self, _btn):
        if self._mode_hold.get_active():
            self._config.hotkey_mode = "hold"
        elif self._mode_startstop.get_active():
            self._config.hotkey_mode = "start_stop"
        else:
            self._config.hotkey_mode = "toggle"
        binding = self._hold_entry.get_text().strip()
        try:
            qt_key_sequence(binding)
            self._config.hotkey_hold = binding
            self._hold_hint.set_markup("")
        except ValueError as e:
            # Keep the last working binding rather than registering nothing.
            self._hold_hint.set_markup(
                f"<small>{GLib.markup_escape_text(str(e))}</small>")
            self._hold_entry.set_text(self._config.hotkey_hold)
        self._config.hotkey_toggle = self._hk_toggle.binding
        self._config.hotkey_start = self._hk_start.binding
        self._config.hotkey_stop = self._hk_stop.binding
        self._config.hotkey_pause = self._hk_pause.binding
        if self._on_save:
            self._on_save(self._config)

    # --- General tab ---

    def _build_general_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # Microphone selector
        hbox_mic = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_mic.pack_start(Gtk.Label(label="Microphone:"), False, False, 0)
        self._mic_combo = Gtk.ComboBoxText()
        self._mic_combo.append("", "System Default")
        devices = list_input_devices()
        for dev in devices:
            self._mic_combo.append(str(dev["index"]), dev["name"])
        self._mic_combo.set_active_id(self._config.audio_device or "")
        hbox_mic.pack_start(self._mic_combo, True, True, 0)
        box.pack_start(hbox_mic, False, False, 0)

        # Typing method
        hbox_typer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_typer.pack_start(Gtk.Label(label="Typing method:"), False, False, 0)
        self._typer_combo = Gtk.ComboBoxText()
        self._typer_combo.append("clipboard", "Clipboard paste (recommended)")
        self._typer_combo.append("wtype", "wtype (GNOME/Sway only)")
        self._typer_combo.append("ydotool", "ydotool (needs daemon+uinput)")
        self._typer_combo.set_active_id(self._config.typer)
        hbox_typer.pack_start(self._typer_combo, False, False, 0)
        box.pack_start(hbox_typer, False, False, 0)

        # When the text lands
        hbox_ins = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_ins.pack_start(Gtk.Label(label="Insert text:"), False, False, 0)
        self._insert_mode_combo = Gtk.ComboBoxText()
        self._insert_mode_combo.append("per_segment", "As each phrase decodes")
        self._insert_mode_combo.append("end_of_take", "Once, when I let the key go")
        self._insert_mode_combo.set_active_id(
            self._config.insert_mode
            if self._config.insert_mode in ("per_segment", "end_of_take")
            else "per_segment")
        hbox_ins.pack_start(self._insert_mode_combo, False, False, 0)
        box.pack_start(hbox_ins, False, False, 0)
        self._reflect_hold_mode()

        sep0 = Gtk.Separator()
        sep0.set_margin_top(4)
        box.pack_start(sep0, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.pack_start(Gtk.Label(label="Beep volume:"), False, False, 0)
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self._vol_scale.set_value(self._config.beep_volume)
        hbox.pack_start(self._vol_scale, True, True, 0)
        box.pack_start(hbox, False, False, 0)

        hbox2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox2.pack_start(Gtk.Label(label="CPU threads:"), False, False, 0)
        self._threads_spin = Gtk.SpinButton.new_with_range(1, 16, 1)
        self._threads_spin.set_value(self._config.num_threads)
        hbox2.pack_start(self._threads_spin, False, False, 0)
        box.pack_start(hbox2, False, False, 0)

        hbox_vad = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_vad.pack_start(Gtk.Label(label="Pause that ends a phrase:"),
                            False, False, 0)
        self._vad_silence_spin = Gtk.SpinButton.new_with_range(0.2, 3.0, 0.1)
        self._vad_silence_spin.set_digits(1)
        self._vad_silence_spin.set_value(self._config.vad_min_silence)
        self._vad_silence_spin.set_tooltip_text(
            "Seconds of silence before the phrase is treated as finished and "
            "sent to the model.  Raise it if pausing to think splits your "
            "sentences; lower it only if you want shorter phrases.")
        hbox_vad.pack_start(self._vad_silence_spin, False, False, 0)
        hbox_vad.pack_start(Gtk.Label(label="seconds"), False, False, 0)
        box.pack_start(hbox_vad, False, False, 0)

        # Language (applies to Canary model)
        hbox_lang = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_lang.pack_start(Gtk.Label(label="Language:"), False, False, 0)
        self._lang_combo = Gtk.ComboBoxText()
        for code, label in LANG_LABELS.items():
            self._lang_combo.append(code, label)
        self._lang_combo.set_active_id(self._config.language)
        hbox_lang.pack_start(self._lang_combo, False, False, 0)
        lang_hint = Gtk.Label()
        lang_hint.set_markup("<small>Used by Canary model. Parakeet auto-detects.</small>")
        lang_hint.get_style_context().add_class("dim-label")
        hbox_lang.pack_start(lang_hint, False, False, 0)
        box.pack_start(hbox_lang, False, False, 0)

        # Streaming options
        sep_stream = Gtk.Separator()
        sep_stream.set_margin_top(4)
        box.pack_start(sep_stream, False, False, 0)

        self._partial_overwrite_check = Gtk.CheckButton(
            label="Streaming partial-overwrite (type text as you speak)")
        self._partial_overwrite_check.set_active(self._config.partial_overwrite)
        self._partial_overwrite_check.set_tooltip_text(
            "When enabled, streaming models type partial results into the active window "
            "and revise them in place.  When disabled, text only appears on final endpoint.")
        box.pack_start(self._partial_overwrite_check, False, False, 4)

        self._filter_fillers_check = Gtk.CheckButton(
            label="Filter filler words (um, uh, ehm …)")
        self._filter_fillers_check.set_active(self._config.filter_fillers)
        box.pack_start(self._filter_fillers_check, False, False, 4)

        # Overlay
        sep_ov = Gtk.Separator()
        sep_ov.set_margin_top(8)
        box.pack_start(sep_ov, False, False, 0)

        self._overlay_check = Gtk.CheckButton(
            label="Show pill overlay while dictating")
        self._overlay_check.set_active(self._config.overlay)
        self._overlay_check.set_tooltip_text(
            "A small capsule with a live microphone level meter, so you can "
            "see the microphone is actually picking you up.")
        box.pack_start(self._overlay_check, False, False, 4)

        self._preview_check = Gtk.CheckButton(
            label="Show the decoded text above the pill while I hold the key")
        self._preview_check.set_active(self._config.preview)
        self._preview_check.set_tooltip_text(
            "A floating, read-only panel with the phrases already recognised in "
            "this take.  It is drawn on screen, never typed: inserting text "
            "while the push-to-talk key is held would end the take.")
        self._preview_check.set_margin_start(20)
        box.pack_start(self._preview_check, False, False, 4)

        hbox_ov = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_ov.pack_start(Gtk.Label(label="Overlay position:"), False, False, 0)
        self._overlay_pos_combo = Gtk.ComboBoxText()
        self._overlay_pos_combo.append("bottom", "Bottom centre")
        self._overlay_pos_combo.append("top", "Top centre")
        self._overlay_pos_combo.set_active_id(self._config.overlay_position)
        hbox_ov.pack_start(self._overlay_pos_combo, False, False, 0)
        box.pack_start(hbox_ov, False, False, 0)

        # Night mode
        sep = Gtk.Separator()
        sep.set_margin_top(8)
        box.pack_start(sep, False, False, 0)

        self._night_check = Gtk.CheckButton(label="Night mode (suppress beeps)")
        self._night_check.set_active(self._config.night_mode)
        box.pack_start(self._night_check, False, False, 4)

        hbox3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox3.pack_start(Gtk.Label(label="Quiet hours:"), False, False, 0)
        self._night_start_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._night_start_spin.set_value(self._config.night_start)
        hbox3.pack_start(self._night_start_spin, False, False, 0)
        hbox3.pack_start(Gtk.Label(label="to"), False, False, 0)
        self._night_end_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._night_end_spin.set_value(self._config.night_end)
        hbox3.pack_start(self._night_end_spin, False, False, 0)
        box.pack_start(hbox3, False, False, 0)

        save_btn = Gtk.Button(label="Save General")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._save_general)
        save_btn.set_margin_top(12)
        box.pack_start(save_btn, False, False, 0)

        return box

    INSERT_MODE_TIP = (
        "Per phrase keeps the text flowing while you hold the key.  Once "
        "per take gives one paste and one undo step for the whole take, "
        "but you see nothing until you let go.")
    INSERT_MODE_HOLD_TIP = (
        "Push-to-talk always inserts once, when you let the key go: a paste "
        "while the key is held makes KWin report the key released, which "
        "would end the take mid-sentence.  The live preview shows the text "
        "meanwhile.")

    def _reflect_hold_mode(self, *_args):
        """Pin the insert mode to end_of_take while push-to-talk is selected —
        the same rule AppConfig.enforce applies to whatever gets saved."""
        combo = getattr(self, "_insert_mode_combo", None)
        if combo is None:
            return          # the General tab is built after the Hotkeys tab
        hold = self._mode_hold.get_active()
        if hold:
            combo.set_active_id("end_of_take")
        combo.set_sensitive(not hold)
        combo.set_tooltip_text(self.INSERT_MODE_HOLD_TIP if hold else self.INSERT_MODE_TIP)

    def _save_general(self, _btn):
        self._config.audio_device = self._mic_combo.get_active_id() or ""
        self._config.typer = self._typer_combo.get_active_id() or "wtype"
        self._config.insert_mode = (self._insert_mode_combo.get_active_id()
                                    or "per_segment")
        self._config.beep_volume = self._vol_scale.get_value()
        self._config.num_threads = int(self._threads_spin.get_value())
        self._config.vad_min_silence = round(self._vad_silence_spin.get_value(), 2)
        self._config.language = self._lang_combo.get_active_id() or "en"
        self._config.partial_overwrite = self._partial_overwrite_check.get_active()
        self._config.filter_fillers = self._filter_fillers_check.get_active()
        self._config.overlay = self._overlay_check.get_active()
        self._config.preview = self._preview_check.get_active()
        self._config.overlay_position = self._overlay_pos_combo.get_active_id() or "bottom"
        self._config.night_mode = self._night_check.get_active()
        self._config.night_start = int(self._night_start_spin.get_value())
        self._config.night_end = int(self._night_end_spin.get_value())
        audio._active_config = self._config
        if self._on_save:
            self._on_save(self._config)

    # --- About tab ---

    def _build_about_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        about_text = Gtk.Label()
        about_text.set_markup(
            f"<b>{APP_NAME}</b>\n\n"
            "On-device voice typing with punctuation.\n"
            "Powered by sherpa-onnx + NVIDIA NeMo models.\n\n"
            "<b>Model recommendations:</b>\n\n"
            "<b>Parakeet TDT 0.6B v3</b> (639 MB)\n"
            "Best overall accuracy. Ideal for desktops and workstations.\n"
            "Works on CPU at ~30x real-time. Even faster with GPU.\n"
            "Supports 25 European languages.\n\n"
            "<b>Canary 180M Flash</b> (198 MB)\n"
            "Lightweight model for laptops and low-RAM machines.\n"
            "Good accuracy for its size. Supports EN/ES/DE/FR.\n"
            "Only 198 MB download — ideal for travel.\n\n"
            "<b>Nemotron Streaming 0.6B</b> (631 MB)\n"
            "True real-time streaming — text appears as you speak\n"
            "with no pause needed. English only.\n"
            "Higher latency tradeoff: slightly less accurate on\n"
            "sentence boundaries vs. VAD-segmented models.\n\n"
            "<b>General tips:</b>\n"
            "• All models include punctuation and capitalization\n"
            "• Non-streaming models wait for a brief pause, then transcribe\n"
            "• Streaming model transcribes continuously but may revise text\n"
            "• More CPU threads = faster transcription (4-8 recommended)\n"
            "• Pause hotkey mutes mic without unloading model (fast resume)"
        )
        about_text.set_halign(Gtk.Align.START)
        about_text.set_valign(Gtk.Align.START)
        about_text.set_line_wrap(True)
        about_text.set_selectable(True)
        about_text.set_max_width_chars(60)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(about_text)
        box.pack_start(sw, True, True, 0)

        return box


# ---------------------------------------------------------------------------
# Main window (undockable full-size UI)
# ---------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    def __init__(self, controller: DictationController, hotkey_mgr: HotkeyManager):
        super().__init__(title=APP_NAME)
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        # app.py points this at TrayIcon.apply_settings, so a model change
        # made here rebuilds the tray menu and reports a hotkey conflict too.
        self.on_apply_settings = None
        self.set_default_size(480, -1)
        self.set_resizable(False)
        self.set_icon_name("audio-input-microphone")
        self.connect("delete-event", self._on_delete)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        self.add(vbox)

        # --- Status ---
        self._status_label = Gtk.Label()
        self._status_label.set_markup("<span size='large'>Idle</span>")
        self._status_label.set_halign(Gtk.Align.CENTER)
        vbox.pack_start(self._status_label, False, False, 0)

        # --- Controls ---
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)

        self._toggle_btn = Gtk.Button()
        self._toggle_btn.get_style_context().add_class("suggested-action")
        self._toggle_btn.connect("clicked", self._on_toggle)
        btn_box.pack_start(self._toggle_btn, False, False, 0)

        self._pause_btn = Gtk.Button(label="Pause")
        self._pause_btn.set_sensitive(False)
        self._pause_btn.connect("clicked", lambda _: self._controller.pause())
        btn_box.pack_start(self._pause_btn, False, False, 0)

        vbox.pack_start(btn_box, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 0)

        # --- Microphone selector ---
        mic_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_box.pack_start(Gtk.Label(label="Microphone:"), False, False, 0)
        self._mic_combo = Gtk.ComboBoxText()
        self._mic_combo.append("", "System Default")
        for dev in list_input_devices():
            self._mic_combo.append(str(dev["index"]), dev["name"])
        self._mic_combo.set_active_id(self._controller.config.audio_device or "")
        self._mic_combo.connect("changed", self._on_mic_changed)
        mic_box.pack_start(self._mic_combo, True, True, 0)
        vbox.pack_start(mic_box, False, False, 0)

        # --- Model selector ---
        model_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        model_box.pack_start(Gtk.Label(label="Model:"), False, False, 0)
        self._model_combo = Gtk.ComboBoxText()
        for mid, mdata in self._controller.profiles.items():
            downloaded = _is_model_downloaded(mid, self._controller.profiles)
            label = mdata["name"]
            if not downloaded:
                label += " (not downloaded)"
            self._model_combo.append(mid, label)
        self._model_combo.set_active_id(self._controller.config.model_profile)
        self._model_combo.connect("changed", self._on_model_changed)
        model_box.pack_start(self._model_combo, True, True, 0)
        vbox.pack_start(model_box, False, False, 0)

        # --- Streaming toggle ---
        self._streaming_check = Gtk.CheckButton(label="Streaming mode (type as you speak)")
        self._streaming_check.set_tooltip_text(
            "Switch between real-time streaming (Nemotron) and "
            "VAD-segmented transcription (waits for pause, higher accuracy).")
        is_streaming = self._controller.profiles.get(
            self._controller.config.model_profile, {}).get("streaming", False)
        self._streaming_check.set_active(is_streaming)
        # Remember the non-streaming model so we can restore it
        if is_streaming:
            self._non_streaming_model = "desktop"
        else:
            self._non_streaming_model = self._controller.config.model_profile
        self._streaming_check.connect("toggled", self._on_streaming_toggled)
        vbox.pack_start(self._streaming_check, False, False, 0)

        self._update_controls()

    def _on_delete(self, _win, _event):
        self.hide()
        return True

    def _on_toggle(self, _btn):
        self._controller.toggle()
        self._update_controls()

    def _on_mic_changed(self, combo):
        dev_id = combo.get_active_id() or ""
        cfg = self._controller.config
        cfg.audio_device = dev_id
        cfg.save()

    def _on_model_changed(self, combo):
        model_id = combo.get_active_id()
        if not model_id or model_id == self._controller.config.model_profile:
            return
        if not _is_model_downloaded(model_id, self._controller.profiles):
            self._model_combo.set_active_id(self._controller.config.model_profile)
            return
        cfg = self._controller.config
        cfg.model_profile = model_id
        self._apply_settings(cfg)
        # Keep streaming checkbox in sync
        is_streaming = self._controller.profiles.get(model_id, {}).get("streaming", False)
        self._streaming_check.handler_block_by_func(self._on_streaming_toggled)
        self._streaming_check.set_active(is_streaming)
        self._streaming_check.handler_unblock_by_func(self._on_streaming_toggled)
        if not is_streaming:
            self._non_streaming_model = model_id

    def _on_streaming_toggled(self, check):
        if check.get_active():
            # Remember current non-streaming model, switch to streaming
            cur = self._controller.config.model_profile
            cur_profile = self._controller.profiles.get(cur, {})
            if not cur_profile.get("streaming", False):
                self._non_streaming_model = cur
            target = "streaming"
        else:
            # Restore previous non-streaming model
            target = getattr(self, "_non_streaming_model", "desktop")

        if not _is_model_downloaded(target, self._controller.profiles):
            # Can't switch — revert checkbox
            check.handler_block_by_func(self._on_streaming_toggled)
            check.set_active(not check.get_active())
            check.handler_unblock_by_func(self._on_streaming_toggled)
            return

        cfg = self._controller.config
        cfg.model_profile = target
        self._apply_settings(cfg)
        # Sync the model combo
        self._model_combo.set_active_id(target)

    def _apply_settings(self, cfg):
        if self.on_apply_settings:
            self.on_apply_settings(cfg)
        else:
            self._controller.apply_config(cfg)
            self._hotkey_mgr.rebuild(cfg)

    def _update_controls(self):
        running = self._controller.is_running
        paused = self._controller.is_paused
        cfg = self._controller.config

        if running:
            if paused:
                self._toggle_btn.set_label("Resume")
                self._status_label.set_markup("<span size='large'>Paused</span>")
            else:
                self._toggle_btn.set_label("Stop")
                self._status_label.set_markup("<span size='large'>Listening...</span>")
            self._pause_btn.set_sensitive(True)
        else:
            self._toggle_btn.set_label("Start Dictation")
            self._status_label.set_markup("<span size='large'>Idle</span>")
            self._pause_btn.set_sensitive(False)

        # Sync model combo and streaming checkbox if changed externally
        if self._model_combo.get_active_id() != cfg.model_profile:
            self._model_combo.set_active_id(cfg.model_profile)
        is_streaming = self._controller.profiles.get(
            cfg.model_profile, {}).get("streaming", False)
        if self._streaming_check.get_active() != is_streaming:
            self._streaming_check.handler_block_by_func(self._on_streaming_toggled)
            self._streaming_check.set_active(is_streaming)
            self._streaming_check.handler_unblock_by_func(self._on_streaming_toggled)

    def on_status_update(self, text: str):
        self._update_controls()
        if text and text not in ("Listening...", "Ready", "Resumed", "Paused"):
            display = text[:60] + "\u2026" if len(text) > 60 else text
            self._status_label.set_markup(f"<span size='large'>\u25b6 {GLib.markup_escape_text(display)}</span>")
