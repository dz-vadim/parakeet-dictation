#!/usr/bin/env python3
"""Parakeet Dictation — On-device voice typing with punctuation via sherpa-onnx.

Supports multiple ASR model profiles (Parakeet, Canary, Nemotron) with
configurable hotkeys and VAD-segmented or true streaming transcription.
"""

import signal

from . import audio
from . import insert
from .config import APP_NAME, AppConfig
from .controller import DictationController
from .diagnostics import DIAG
from .focus import FocusTracker
from .hotkeys import HotkeyManager
from .models import _any_model_downloaded
from .ui.overlay import PillOverlay
from .ui.tray import TrayIcon
from .ui.windows import MainWindow, WelcomeDialog
# After the .ui imports: gi.require_version() runs in parakeet_dictation/ui/__init__.py.
from gi.repository import GLib, Gtk  # noqa: E402

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Problems with the file are reported, not swallowed: a config_error line
    # in the diagnostics log (and on stderr) says which key was dropped, or
    # where the unparseable file was backed up to.
    config = AppConfig.load(log=DIAG.log)
    audio._active_config = config

    # Ensure typer is a valid Wayland method
    if config.typer not in ("clipboard", "wtype", "ydotool"):
        config.typer = "clipboard"

    DIAG.set_enabled(config.diagnostics)
    DIAG.log("app_start", profile=config.model_profile,
             threads=config.num_threads, typer=config.typer,
             paste_chord=config.paste_chord, transport=config.paste_transport,
             focus_script=config.focus_script)

    controller = DictationController(config)
    overlay = PillOverlay(config)
    controller.set_overlay(overlay)

    # Which window is focused, straight from KWin, so the paste chord can be
    # chosen per app.  Loaded before the main loop; reports arrive on it.
    focus = FocusTracker(enabled=config.focus_script)
    focus.start()
    controller.set_focus_probe(focus.snapshot)
    # The portal session is opened NOW, not at the first paste: its one-time
    # "Remote Control" prompt must never land in the middle of an insertion.
    if config.paste_transport != "ydotool" and config.typer != "ydotool":
        insert.portal_keyboard().start_async()
    # Warm the model now; otherwise the first dictation start pays the ~1.6 s
    # load while the user is already speaking.
    controller.preload()

    # tray referenced in hotkey lambdas — assigned after creation
    tray = None

    hotkey_mgr = HotkeyManager(
        config,
        on_toggle=lambda: (controller.toggle(), tray and tray.update_ui()),
        on_start=lambda: (controller.start(), tray and tray.update_ui()),
        on_stop=lambda: (controller.stop(), tray and tray.update_ui()),
        on_pause=lambda: controller.pause(),
        on_hold_press=lambda: (controller.hold_press(), tray and tray.update_ui()),
        on_hold_release=lambda: (controller.hold_release(), tray and tray.update_ui()),
    )

    main_window = MainWindow(controller, hotkey_mgr)

    tray = TrayIcon(controller, hotkey_mgr, main_window)
    main_window._tray = tray  # So model changes from window rebuild tray menu
    controller.notify(hotkey_mgr.start())

    signal.signal(signal.SIGINT, lambda *_: (controller.shutdown(), Gtk.main_quit()))

    # Global hotkeys on Wayland: pynput cannot grab keys outside the app's own
    # windows, so the desktop's shortcut system drives us over signals instead.
    # Bind a KDE shortcut to `parakeet-toggle` (SIGUSR1 = toggle, SIGUSR2 = pause).
    def _signal_toggle(*_):
        controller.toggle()
        if tray:
            tray.update_ui()
        return GLib.SOURCE_CONTINUE

    def _signal_pause(*_):
        controller.pause()
        if tray:
            tray.update_ui()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, _signal_toggle)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, _signal_pause)

    # First-run: show welcome dialog if no models are downloaded
    profiles_data = controller.profiles_data
    if not _any_model_downloaded(profiles_data["profiles"]):
        def _on_model_ready(new_config):
            controller.apply_config(new_config)
            hotkey_mgr.rebuild(new_config)
            tray._build_menu()
            tray.update_ui()
        WelcomeDialog(profiles_data, config, _on_model_ready)

    profile_name = controller.profiles.get(
        config.model_profile, {}
    ).get("name", config.model_profile)
    if config.hotkey_mode == "hold":
        mode_desc = f"Push-to-talk: hold {config.hotkey_hold}"
    elif config.hotkey_mode == "toggle":
        mode_desc = f"Toggle: {config.hotkey_toggle}"
    else:
        mode_desc = f"Start: {config.hotkey_start}, Stop: {config.hotkey_stop}"
    print(f"{APP_NAME} running. {mode_desc}. Pause: {config.hotkey_pause}")
    print(f"Model: {profile_name} | Typer: {config.typer} | Threads: {config.num_threads}")

    Gtk.main()
    hotkey_mgr.stop()
    focus.stop()                       # unload the KWin script we loaded
    insert.portal_keyboard().close()
    overlay.shutdown()
