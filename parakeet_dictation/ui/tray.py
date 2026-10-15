"""System tray indicator: renders ui.menu_model, owns the settings window."""

import copy
from pathlib import Path

from gi.repository import AyatanaAppIndicator3, GLib, Gtk

from ..audio import list_input_devices
from ..config import APP_ID, AppConfig
from ..models import _is_model_downloaded
from . import menu_model

_ICONS_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
_LEGACY_ICON = {"idle": "audio-input-microphone-muted",
                "listening": "audio-input-microphone",
                "paused": "audio-input-microphone-muted"}


class TrayIcon:
    def __init__(self, controller, hotkey_mgr):
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self._settings = None
        self._state_shown = None
        self._items = None
        self._menu = None
        self._indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID, "audio-input-microphone-muted",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self._custom_icons = (_ICONS_DIR / "parakeet-mic-idle.svg").is_file()
        if self._custom_icons:
            self._indicator.set_icon_theme_path(str(_ICONS_DIR))
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        controller.set_status_callback(self._on_status)
        self.update_ui()

    # -- state ---------------------------------------------------------------
    def _state(self) -> str:
        if not self._controller.is_running:
            return "idle"
        return "paused" if self._controller.is_paused else "recording"

    def _icon_name(self, state: str) -> str:
        icon_state = {"idle": "idle", "recording": "listening", "paused": "paused"}[state]
        if self._custom_icons:
            return f"parakeet-{self._controller.config.tray_icon_style}-{icon_state}"
        return _LEGACY_ICON[icon_state]

    def _on_status(self, _text):
        # Status text (streaming partials, "Listening...") arrives many times
        # per take; neither the icon, the tooltip nor the menu depend on it.
        if self._state() != self._state_shown:
            self.update_ui()

    def update_ui(self):
        state = self._state()
        self._state_shown = state
        cfg = self._controller.config
        self._indicator.set_icon_full(self._icon_name(state), state)
        self._indicator.set_title(menu_model.tooltip(state, cfg))
        try:
            devices = [d["name"] for d in list_input_devices()]
        except Exception:
            devices = []
        profiles = self._controller.profiles
        downloaded = {m for m in profiles if _is_model_downloaded(m, profiles)}
        items = menu_model.build_menu(state, cfg, profiles, downloaded, devices)
        if items == self._items:
            return          # re-exporting would collapse a menu the user has open
        old, self._items = self._menu, items
        self._menu = self._render(items)
        self._indicator.set_menu(self._menu)
        if old is not None:
            # Later: this may run from one of the old menu's own item handlers.
            GLib.idle_add(old.destroy)

    # -- rendering -------------------------------------------------------------
    def _render(self, items) -> Gtk.Menu:
        menu = Gtk.Menu()
        group = None
        for item in items:
            if item.kind == "separator":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if item.kind == "submenu":
                mi = Gtk.MenuItem(label=item.label)
                mi.set_submenu(self._render(item.children))
            elif item.kind == "radio":
                if group is None:
                    # A GTK radio group always has one member active.  This
                    # one is never shown: it holds the selection when no
                    # real choice is current (saved model not downloaded, a
                    # legacy device index), so none looks chosen and a click
                    # on any of them is a change that fires "toggled".
                    group = Gtk.RadioMenuItem()
                    menu.radio_placeholder = group
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, item.label)
                mi.set_active(item.active)
                mi.connect("toggled", lambda w, a=item.action: w.get_active() and self._do(a))
            else:
                mi = Gtk.MenuItem(label=f"{item.label}\t{item.accel}" if item.accel else item.label)
                mi.connect("activate", lambda _w, a=item.action: self._do(a))
            menu.append(mi)
        menu.show_all()
        return menu

    # -- actions ---------------------------------------------------------------
    def _do(self, action: str):
        cfg = self._controller.config
        if action == "toggle":
            self._controller.toggle(hold_off=False)
        elif action in ("pause", "resume"):
            self._controller.pause()
        elif action == "settings":
            self.open_settings("general")
        elif action == "about":
            self.open_settings("about")
        elif action == "download_models":
            self.open_settings("recognition")
        elif action == "quit":
            self._controller.shutdown()
            Gtk.main_quit()
            return
        elif action.startswith("mic:"):
            new = copy.deepcopy(cfg)
            new.audio_device = action[4:]
            if new.audio_device != cfg.audio_device:
                self.apply_settings(new)
            return
        elif action.startswith("model:"):
            new = copy.deepcopy(cfg)
            new.model_profile = action[6:]
            if new.model_profile != cfg.model_profile:
                self.apply_settings(new)
            return
        self.update_ui()

    def open_settings(self, page: str = "general"):
        if self._settings is None:
            from .settings import SettingsWindow
            self._settings = SettingsWindow(lambda: self._controller.config,
                                            self._controller.profiles_data,
                                            on_apply=self.apply_settings,
                                            on_models_changed=self.update_ui)
        elif not self._settings.get_visible():
            # Long-lived window: the config, autostart and devices may have
            # changed (tray menu, KDE shortcuts) since it was last shown.
            self._settings.refresh_from_config()
        self._settings.show_page(page)
        self._settings.present()

    def apply_settings(self, cfg: AppConfig) -> tuple:
        """The one place a settings change lands (window, menu, KDE).

        Returns (saved, problem): saved is False when the config file could
        not be written; problem joins every message for the user."""
        save_problem = self._controller.apply_config(cfg)
        hotkey_problem = self._hotkey_mgr.rebuild(cfg)
        problem = "; ".join(p for p in (save_problem, hotkey_problem) if p)
        self._controller.notify(problem)
        self.update_ui()
        return (not save_problem, problem)

    def on_binding_changed(self, key: str, binding: str):
        new = copy.deepcopy(self._controller.config)
        setattr(new, key, binding)
        self.apply_settings(new)
