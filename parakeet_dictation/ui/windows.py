"""First-run welcome dialog."""

import copy
import sys

from gi.repository import GLib, Gtk

from ..config import APP_NAME
from ..i18n import _
from ..models import _download_all_models


class WelcomeDialog(Gtk.Dialog):
    """First-run dialog — downloads all models automatically."""

    def __init__(self, profiles_data: dict, get_config, on_model_ready):
        """`get_config` returns the live config: the download takes minutes,
        and settings changed meanwhile must not be reverted at the end."""
        super().__init__(title=_("Welcome to {app}").format(app=APP_NAME), flags=0)
        self._profiles_data = profiles_data
        self._profiles = profiles_data["profiles"]
        self._get_config = get_config
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
        header.set_markup("<span size='x-large' weight='bold'>{}</span>".format(
            GLib.markup_escape_text(_("Welcome to {app}").format(app=APP_NAME))))
        header.set_halign(Gtk.Align.START)
        box.pack_start(header, False, False, 0)

        total_mb = sum(m["size_mb"] for m in self._profiles.values())
        subtitle = Gtk.Label()
        subtitle.set_markup(
            GLib.markup_escape_text(_("Three speech recognition models will be downloaded\n"
                                      "so you can switch between them freely."))
            + "\n\n" + _("Total download: {size}").format(
                size="<b>~{} {}</b>".format(total_mb, _("MB")))
        )
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_line_wrap(True)
        box.pack_start(subtitle, False, False, 0)

        # Model summary (read-only)
        for mid, mdata in self._profiles.items():
            lbl = Gtk.Label()
            tag = _("Streaming") if mdata.get("streaming") else _("VAD-segmented")
            lbl.set_markup(
                "  \u2022 <b>{name}</b>  ({size} {mb}, {tag})".format(
                    name=GLib.markup_escape_text(mdata["name"]), size=mdata["size_mb"],
                    mb=_("MB"), tag=GLib.markup_escape_text(tag))
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
        dl_hbox.pack_start(Gtk.Label(label=_("Download All Models")), False, False, 0)
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
                config = copy.deepcopy(self._get_config())
                config.model_profile = "desktop"
                self.destroy()
                if self._on_model_ready:
                    self._on_model_ready(config)
            else:
                btn.set_sensitive(True)
                self._progress_bar.hide()
                self._error_label.set_markup("<span color='red'>{}</span>".format(GLib.markup_escape_text(
                    _("Download failed: {error}").format(error=err))))
                self._error_label.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_all_models(self._profiles_data, on_progress, on_done)
