"""The tray menu as data, so its contents are testable without a display."""

from dataclasses import dataclass, field

from ..config import APP_NAME
from ..i18n import _


@dataclass
class Item:
    kind: str
    label: str = ""
    accel: str = ""
    action: str = ""
    active: bool = False
    children: list = field(default_factory=list)


SEP = Item("separator")


def _mic_items(config, devices):
    saved = config.audio_device
    items = [Item("radio", _("System default"), action="mic:", active=not saved)]
    for name in devices:
        items.append(Item("radio", name, action=f"mic:{name}", active=(name == saved)))
    if saved and saved not in devices and not saved.isdigit():
        items.append(Item("radio", _("{name} (not connected)").format(name=saved),
                          action=f"mic:{saved}", active=True))
    return items


def _model_items(config, profiles, downloaded):
    items = [Item("radio", data.get("name", pid), action=f"model:{pid}",
                  active=(pid == config.model_profile))
             for pid, data in profiles.items() if pid in downloaded]
    if len(downloaded) < len(profiles):
        items += [SEP, Item("action", _("Download More…"), action="download_models")]
    return items


def build_menu(state, config, profiles, downloaded, devices):
    if state == "idle":
        top = [Item("action", _("Start Dictation"), accel=config.hotkey_toggle, action="toggle")]
    else:
        top = [Item("action", _("Stop Dictation"), accel=config.hotkey_toggle, action="toggle"),
               Item("action", _("Resume") if state == "paused" else _("Pause"),
                    action="resume" if state == "paused" else "pause")]
    return top + [
        SEP,
        Item("submenu", _("Microphone"), children=_mic_items(config, devices)),
        Item("submenu", _("Model"), children=_model_items(config, profiles, downloaded)),
        SEP,
        Item("action", _("Settings…"), action="settings"),
        Item("action", _("About"), action="about"),
        Item("action", _("Quit"), action="quit"),
    ]


def tooltip(state, config):
    if state == "recording":
        status = _("listening")
    elif state == "paused":
        status = _("paused")
    elif config.hotkey_hold:
        status = _("ready · hold {key} to talk").format(key=config.hotkey_hold)
    else:
        status = _("ready")
    return f"{APP_NAME} — {status}"
