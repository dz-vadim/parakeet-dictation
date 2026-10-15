"""The five settings pages.  Each builder takes the SettingsWindow."""

import subprocess

from gi.repository import GLib, Gtk

from ... import __version__
from ...audio import list_input_devices
from ...config import APP_NAME, DATA_DIR
from ...i18n import _
from ...models import _download_model, _is_model_downloaded
from .widgets import Section, device_choices

_ICONS = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / "data" / "icons"


def _box(*children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for c in children:
        box.pack_start(c, False, False, 0)
    return box


def general(win):
    shortcuts = Section(_("Shortcuts"))
    shortcuts.row(_("Hold to talk:"), win.bind_shortcut("hotkey_hold"))
    shortcuts.row(_("Start / stop:"), win.bind_shortcut("hotkey_toggle"),
                  _("Click, then press the shortcut. Backspace disables it, Esc cancels."))
    kde = Gtk.LinkButton.new_with_label("", _("Open KDE shortcut settings…"))
    kde.connect("activate-link", lambda *_a: (subprocess.Popen(
        ["systemsettings", "kcm_keys"], start_new_session=True), True)[1])
    shortcuts.row(None, kde)

    mic = Section(_("Microphone"))
    try:
        names = [d["name"] for d in list_input_devices()]
    except Exception:
        names = []
    mic.row(_("Device:"), win.bind_combo("audio_device", device_choices(win.draft.get("audio_device"), names)))

    sounds = Section(_("Sounds"))
    beep_on = Gtk.CheckButton(label=_("Beep at start and end"))
    volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.05, 1.0, 0.05)
    volume.set_draw_value(False)
    volume.set_size_request(180, -1)

    syncing = []

    def sync_beep(*_a):
        # Pushes the draft into the widgets; the handlers below must not
        # write the half-updated widget state back into the draft.
        syncing.append(True)
        on = win.draft.get("beep_volume") > 0
        if on:
            volume.set_value(win.draft.get("beep_volume"))
        beep_on.set_active(on)
        volume.set_sensitive(on)
        syncing.pop()

    def on_beep_toggled(b):
        volume.set_sensitive(b.get_active())
        if not syncing:
            win._changed("beep_volume", volume.get_value() if b.get_active() else 0.0)

    def on_volume(s):
        if beep_on.get_active() and not syncing:
            win._changed("beep_volume", s.get_value())
    beep_on.connect("toggled", on_beep_toggled)
    volume.connect("value-changed", on_volume)
    win._refreshers.append(sync_beep)
    sync_beep()
    row = Gtk.Box(spacing=8)
    row.pack_start(beep_on, False, False, 0)
    row.pack_start(volume, False, False, 0)
    sounds.row(None, row)
    quiet = Gtk.Box(spacing=8)
    quiet.pack_start(win.bind_check("night_mode", _("Quiet hours, no beeps:")), False, False, 0)
    quiet.pack_start(win.bind_spin("night_start", 0, 23, 1), False, False, 0)
    quiet.pack_start(Gtk.Label(label="—"), False, False, 0)
    quiet.pack_start(win.bind_spin("night_end", 0, 23, 1), False, False, 0)
    sounds.row(None, quiet)

    system = Section(_("System"))
    system.row(_("Interface language:"), win.bind_combo("ui_language", [
        ("system", _("System default")), ("uk", "Українська"), ("en", "English")]),
        _("Takes effect after the app restarts."))
    login = Gtk.CheckButton(label=_("Start at login"))
    login.set_active(win.autostart_wanted)

    def on_login(b):
        win.autostart_wanted = b.get_active()
        win._refresh_buttons()
    login.connect("toggled", on_login)
    win._refreshers.append(lambda: login.set_active(win.autostart_wanted))
    system.row(None, login)
    return _box(shortcuts, mic, sounds, system)


def recognition(win):
    section = Section(_("Model"))
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    frame = Gtk.Frame()
    frame.add(listbox)
    progress = Gtk.ProgressBar(show_text=True)
    progress.set_no_show_all(True)
    error = Gtk.Label(xalign=0.0)
    error.set_line_wrap(True)
    error.set_no_show_all(True)
    profiles = win.profiles_data["profiles"]

    def populate():
        for child in listbox.get_children():
            listbox.remove(child)
        for mid, data in profiles.items():
            row = Gtk.Box(spacing=12, margin=8)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = Gtk.Label(label=data["name"], xalign=0.0)
            meta = Gtk.Label(label=data.get("description", ""), xalign=0.0)
            meta.set_line_wrap(True)
            meta.get_style_context().add_class("dim-label")
            text.pack_start(name, False, False, 0)
            text.pack_start(meta, False, False, 0)
            row.pack_start(text, True, True, 0)
            if mid == win.draft.get("model_profile"):
                row.pack_end(Gtk.Label(label=_("In use")), False, False, 0)
            elif _is_model_downloaded(mid, profiles):
                use = Gtk.Button(label=_("Use"))
                use.connect("clicked", lambda _b, m=mid: (win._changed("model_profile", m), populate()))
                row.pack_end(use, False, False, 0)
            else:
                dl = Gtk.Button(label=_("Download"))
                dl.connect("clicked", lambda b, m=mid: download(b, m))
                row.pack_end(dl, False, False, 0)
            listbox.add(row)
        listbox.show_all()

    def download(button, model_id):
        button.set_sensitive(False)
        progress.show()
        error.hide()

        def on_progress(msg, fraction):
            progress.set_text(msg)
            if fraction >= 0:
                progress.set_fraction(min(fraction, 1.0))
            else:
                progress.pulse()

        def on_done(ok, err):
            progress.hide()
            if ok:
                populate()
                win.on_models_changed()
            else:
                button.set_sensitive(True)
                error.set_text(_("Download failed: {error}").format(error=err))
                error.show()
        _download_model(model_id, win.profiles_data, on_progress, on_done)

    win._refreshers.append(populate)
    populate()
    section.add_full(frame)
    section.add_full(progress)
    section.add_full(error)

    text = Section(_("Text"))
    text.row(_("Language:"), win.bind_combo("language", [
        ("en", _("Automatic (Parakeet) / English")), ("uk", "Українська"),
        ("de", "Deutsch"), ("es", "Español"), ("fr", "Français")]),
        _("Parakeet detects the language itself; this matters for Canary only."))
    text.row(None, win.bind_check("preview", _("Show the text live while dictating")))
    text.row(None, win.bind_check("filter_fillers", _("Remove filler words (“um”, “uh”)")))
    return _box(section, text)


def output(win):
    when = Section(_("When to insert text"))
    when.row(None, win.bind_radio("insert_mode", [
        ("end_of_take", _("When dictation ends")),
        ("per_segment", _("After each phrase"))]),
        _("Hold to talk always inserts when the key is released."))
    clip = Section(_("Clipboard"))
    clip.row(None, win.bind_check("keep_on_clipboard", _("Keep dictated text on the clipboard")),
             _("If pasting fails, the text can still be pasted by hand with Ctrl+V."))
    ind = Section(_("On-screen indicator"))
    ind.row(None, win.bind_check("overlay", _("Show the recording indicator")))
    ind.row(_("Position:"), win.bind_combo("overlay_position", [
        ("bottom", _("Bottom of the screen")), ("top", _("Top of the screen"))]))
    tray = Section(_("Tray icon"))
    icons = Gtk.Box(spacing=18)
    group = None
    buttons = {}
    for style, label in (("mic", _("Microphone")), ("parrot", _("Parrot")), ("wave", _("Waveform"))):
        rb = Gtk.RadioButton.new_from_widget(group)
        group = group or rb
        inner = Gtk.Box(spacing=6)
        inner.pack_start(Gtk.Image.new_from_file(str(_ICONS / f"parakeet-{style}-listening.svg")), False, False, 0)
        inner.pack_start(Gtk.Label(label=label), False, False, 0)
        rb.add(inner)
        rb.connect("toggled", lambda b, s=style: b.get_active() and win._changed("tray_icon_style", s))
        buttons[style] = rb
        icons.pack_start(rb, False, False, 0)
    buttons.get(win.draft.get("tray_icon_style"), group).set_active(True)
    win._refreshers.append(lambda: buttons.get(win.draft.get("tray_icon_style"), group).set_active(True))
    tray.row(None, icons)
    return _box(when, clip, ind, tray)


def advanced(win):
    perf = Section(_("Performance"))
    perf.row(_("CPU threads:"), win.bind_spin("num_threads", 1, 16, 1))
    pause = Gtk.Box(spacing=6)
    pause.pack_start(win.bind_spin("vad_min_silence", 0.2, 3.0, 0.1, digits=1), False, False, 0)
    pause.pack_start(Gtk.Label(label=_("s")), False, False, 0)
    perf.row(_("Pause that ends a phrase:"), pause)
    paste = Section(_("Pasting"))
    delay = Gtk.Box(spacing=6)
    delay.pack_start(win.bind_spin("paste_min_delay_ms", 0, 2000, 50), False, False, 0)
    delay.pack_start(Gtk.Label(label=_("ms")), False, False, 0)
    paste.row(_("Delay after a shortcut with Meta/Ctrl/Alt:"), delay,
              _("Only when the take was ended by a shortcut with a modifier."))
    paste.row(_("Key press method:"), win.bind_combo("paste_transport", [
        ("auto", _("Automatic (portal, else ydotool)")), ("portal", _("KDE portal")),
        ("ydotool", "ydotool")]))
    terms = Gtk.Entry()
    terms.set_width_chars(40)
    terms.set_text(", ".join(win.draft.get("terminal_window_classes")))
    terms.connect("changed", lambda e: win._changed(
        "terminal_window_classes", [t.strip() for t in e.get_text().split(",") if t.strip()]))
    win._refreshers.append(lambda: terms.get_text() != ", ".join(win.draft.get("terminal_window_classes"))
                           and terms.set_text(", ".join(win.draft.get("terminal_window_classes"))))
    paste.row(_("Terminals (Ctrl+Shift+V):"), terms, _("Window classes, comma-separated."))
    diag = Section(_("Diagnostics"))
    row = Gtk.Box(spacing=8)
    row.pack_start(win.bind_check("diagnostics", _("Write a diagnostics log")), False, False, 0)
    folder = Gtk.Button(label=_("Open folder"))
    folder.connect("clicked", lambda _b: subprocess.Popen(["xdg-open", str(DATA_DIR)], start_new_session=True))
    row.pack_start(folder, False, False, 0)
    diag.row(None, row, _("Timings and character counts only. Text and audio are never recorded."))
    return _box(perf, paste, diag)


def about(win):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10)
    head = Gtk.Box(spacing=10)
    img = Gtk.Image.new_from_file(str(_ICONS / "parakeet-wave-listening.svg"))
    head.pack_start(img, False, False, 0)
    title = Gtk.Label(xalign=0.0)
    title.set_markup(f"<big><b>{GLib.markup_escape_text(APP_NAME)} {__version__}</b></big>")
    head.pack_start(title, False, False, 0)
    box.pack_start(head, False, False, 0)
    desc = Gtk.Label(label=_("Offline dictation for KDE Plasma: hold a key, speak, release.\n"
                             "Recognition runs on this computer; nothing is sent over the network."),
                     xalign=0.0)
    desc.set_line_wrap(True)
    box.pack_start(desc, False, False, 0)
    for line in (_("MIT licence · © Daniel Rosehill, Vadym Dziuba"),
                 _("Parakeet TDT model — NVIDIA, CC-BY-4.0 · TEN VAD — Agora, Apache-2.0 with additional conditions")):
        lb = Gtk.Label(label=line, xalign=0.0)
        lb.set_line_wrap(True)
        lb.get_style_context().add_class("dim-label")
        box.pack_start(lb, False, False, 0)
    links = Gtk.Box(spacing=12)
    links.pack_start(Gtk.LinkButton.new_with_label("https://github.com/dz-vadim/parakeet-dictation", "GitHub"), False, False, 0)
    links.pack_start(Gtk.LinkButton.new_with_label("https://aur.archlinux.org/packages/parakeet-dictation-git", "AUR"), False, False, 0)
    box.pack_start(links, False, False, 0)
    return box


PAGES = [
    ("general", _("General"), "preferences-system", general),
    ("recognition", _("Recognition"), "audio-input-microphone", recognition),
    ("output", _("Text Output"), "edit-paste", output),
    ("advanced", _("Advanced"), "configure", advanced),
    ("about", _("About"), "help-about", about),
]
