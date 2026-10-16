#!/usr/bin/env python3
"""Parakeet Dictation — On-device voice typing with punctuation via sherpa-onnx.

Supports multiple ASR model profiles (Parakeet, Canary, Nemotron) with
configurable hotkeys and VAD-segmented or true streaming transcription.
"""

import signal

from . import audio
from . import i18n
from . import insert
from . import shortcuts
from .config import APP_NAME, AppConfig
from .controller import DictationController
from .diagnostics import DIAG
from .focus import FocusTracker
from .hotkeys import HotkeyManager
from .instance import InstanceLock
from .models import _any_model_downloaded, migrate_legacy_models
from .ui.overlay import PillOverlay
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
    # Before the UI modules below: some build translated strings at import time.
    i18n.setup(config.ui_language)
    from .ui.tray import TrayIcon
    from .ui.windows import WelcomeDialog

    DIAG.set_enabled(config.diagnostics)

    # One instance per session: a second copy would register the same
    # kglobalaccel component and both would act on every push-to-talk press.
    lock = InstanceLock()
    if not lock.acquire():
        who = f" (pid {lock.owner_pid})" if lock.owner_pid else ""
        print(f"{APP_NAME} is already running{who}; this copy is exiting.")
        return 0

    # Models a pre-split checkout kept in <checkout>/models move to the data
    # dir.  A failure here must not stop the app: the welcome dialog offers
    # a fresh download instead.
    try:
        migrate_legacy_models()
    except Exception as e:
        DIAG.log("model_migration_failed", err=type(e).__name__)

    DIAG.log("app_start", profile=config.model_profile,
             threads=config.num_threads, typer=config.typer,
             paste_chord=config.paste_chord, transport=config.paste_transport,
             focus_script=config.focus_script)

    controller = DictationController(config)
    # Warm the model first: the ~1.6 s load runs on its own thread while the
    # rest of the UI is built, instead of after it, and the first dictation
    # start no longer pays it while the user is already speaking.
    controller.preload()

    overlay = PillOverlay(config)
    controller.set_overlay(overlay)

    # Which window is focused, straight from KWin, so the paste chord can be
    # chosen per app.  Loading starts before the main loop; reports arrive on it.
    focus = FocusTracker(enabled=config.focus_script)
    focus.start()
    controller.set_focus_probe(focus.snapshot)
    # The portal session (or, per take, just its first-time grant) is set up
    # NOW: its one-time "Remote Control" prompt must never land mid-insertion.
    controller.init_portal()

    # tray referenced in hotkey lambdas — assigned after creation
    tray = None

    hotkey_mgr = HotkeyManager(
        config,
        on_hold_press=lambda: (controller.hold_press(), tray and tray.update_ui()),
        on_hold_release=lambda: (controller.hold_release(), tray and tray.update_ui()),
        on_toggle=lambda: (controller.toggle(hold_off=shortcuts.has_modifier(controller.config.hotkey_toggle)), tray and tray.update_ui()),
        on_binding_changed=lambda k, b: tray and tray.on_binding_changed(k, b),
    )

    tray = TrayIcon(controller, hotkey_mgr)
    controller.notify(hotkey_mgr.start())

    # SIGINT (Ctrl+C) and SIGTERM (systemd, logout, `kill`) take the same
    # path as Quit in the tray: the take is ended and a pending clipboard
    # restore runs before the loop exits.  On the main loop, not through
    # signal.signal: a Python handler only runs once GTK hands control back.
    def _signal_quit(*_):
        DIAG.log("app_signal_quit")
        controller.shutdown()
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _signal_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _signal_quit)

    # Scripts and other shortcut tools can drive the app over signals too:
    # `parakeet-toggle` sends SIGUSR1 (toggle); SIGUSR2 pauses.
    def _signal_toggle(*_):
        controller.toggle()            # hold-off stays on: usually sent by a KDE shortcut
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
        WelcomeDialog(profiles_data, lambda: controller.config, tray.apply_settings)

    profile_name = controller.profiles.get(
        config.model_profile, {}
    ).get("name", config.model_profile)
    mode_desc = f"Hold {config.hotkey_hold or '-'} to talk · start/stop {config.hotkey_toggle or '-'}"
    print(f"{APP_NAME} running. {mode_desc}.")
    print(f"Model: {profile_name} | Typer: {config.typer} | Threads: {config.num_threads}")

    Gtk.main()
    hotkey_mgr.stop()
    focus.stop()                       # unload the KWin script we loaded
    insert.portal_keyboard().close()
    overlay.shutdown()
    return 0
