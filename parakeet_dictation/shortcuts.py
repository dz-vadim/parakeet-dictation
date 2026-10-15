"""KDE shortcut strings ("Meta+Alt+D") — parsing, display and key capture.

kglobalaccel speaks QKeySequence ints: modifier bits high (Meta 0x10000000,
Alt 0x08000000, Ctrl 0x04000000, Shift 0x02000000), the key code low.  This
module is the one translation between that, the config's strings and the
key events the settings window captures.  No GTK, no D-Bus.
"""

import re

MODIFIERS = ("Meta", "Ctrl", "Alt", "Shift")
QT_MODIFIER_BITS = {
    "shift": 0x02000000,
    "ctrl": 0x04000000, "control": 0x04000000,
    "alt": 0x08000000,
    "meta": 0x10000000, "super": 0x10000000, "win": 0x10000000,
}
_BIT_TO_NAME = (("Meta", 0x10000000), ("Ctrl", 0x04000000),
                ("Alt", 0x08000000), ("Shift", 0x02000000))
_MOD_MASK = 0x1E000000

# Canonical key name -> Qt key code.  Canonical spelling is what the config
# holds and the UI shows.
_NAMED = {
    "Escape": 0x01000000, "Tab": 0x01000001, "Backtab": 0x01000002,
    "Backspace": 0x01000003, "Return": 0x01000004, "Enter": 0x01000005,
    "Insert": 0x01000006, "Delete": 0x01000007, "Pause": 0x01000008,
    "Print": 0x01000009, "SysReq": 0x0100000A, "Clear": 0x0100000B,
    "Home": 0x01000010, "End": 0x01000011, "Left": 0x01000012,
    "Up": 0x01000013, "Right": 0x01000014, "Down": 0x01000015,
    "PageUp": 0x01000016, "PageDown": 0x01000017, "CapsLock": 0x01000024,
    "NumLock": 0x01000025, "ScrollLock": 0x01000026, "Menu": 0x01000055,
    "Space": 0x20,
}
QT_NAMED_KEYS = {k.lower(): v for k, v in _NAMED.items()}
_CODE_TO_NAME = {v: k for k, v in _NAMED.items()}

# Gdk keyval names that differ from the canonical name.
_GDK_ALIASES = {
    "space": "Space", "Scroll_Lock": "ScrollLock", "Prior": "PageUp",
    "Next": "PageDown", "Page_Up": "PageUp", "Page_Down": "PageDown",
    "Caps_Lock": "CapsLock", "Num_Lock": "NumLock", "KP_Enter": "Enter",
    "ISO_Left_Tab": "Backtab", "Sys_Req": "SysReq", "BackSpace": "Backspace",
}
_LONE_MODIFIERS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift", "ISO_Level5_Shift", "Caps_Lock",
}
# Keys that may be a shortcut on their own: nothing types them.
_BARE_OK = {"Insert", "Pause", "ScrollLock", "Menu", "Print"} | {f"F{n}" for n in range(1, 25)}


def qt_key_sequence(binding: str) -> int:
    """KDE shortcut string -> Qt key int.  Raises ValueError.

    kglobalaccel speaks QKeySequence, not X11 keysyms.  Cross-check against a
    shortcut KDE ships: Close Window reads back as 150994995 = 0x09000033 =
    Alt+F4, and Meta+Alt+D is 0x18000044.
    """
    parts = [p.strip() for p in binding.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    *mods, key = parts
    value = 0
    for mod in mods:
        bit = QT_MODIFIER_BITS.get(mod.lower())
        if bit is None:
            raise ValueError(f"unknown modifier {mod!r}")
        value |= bit
    low = key.lower()
    if low in QT_NAMED_KEYS:
        return value | QT_NAMED_KEYS[low]
    m = re.fullmatch(r"f(\d{1,2})", low)
    if m and 1 <= int(m.group(1)) <= 35:
        return value | (0x01000030 + int(m.group(1)) - 1)
    if len(key) == 1 and key.isascii() and key.isprintable():
        return value | ord(key.upper())
    raise ValueError(f"unknown key {key!r}")


def binding_from_qt(key: int) -> str:
    """Qt key int -> canonical KDE string; "" for 0 or an unknown code."""
    if not key:
        return ""
    mods = [name for name, bit in _BIT_TO_NAME if key & bit]
    code = key & ~_MOD_MASK
    if code in _CODE_TO_NAME:
        name = _CODE_TO_NAME[code]
    elif 0x01000030 <= code <= 0x01000052:
        name = f"F{code - 0x01000030 + 1}"
    elif 0x21 <= code <= 0x7E:
        name = chr(code).upper()
    else:
        return ""
    return "+".join([*mods, name])


def is_valid(binding: str) -> bool:
    if binding == "":
        return True
    if not isinstance(binding, str):
        return False
    try:
        qt_key_sequence(binding)
    except ValueError:
        return False
    return True


def has_modifier(binding: str) -> bool:
    try:
        return bool(qt_key_sequence(binding) & _MOD_MASK)
    except ValueError:
        return False


def display(binding: str) -> str:
    return binding or ""


def _canonical_key(keyname: str):
    name = _GDK_ALIASES.get(keyname, keyname)
    if name in _NAMED:
        return name
    m = re.fullmatch(r"F(\d{1,2})", name)
    if m and 1 <= int(m.group(1)) <= 35:
        return name
    if len(name) == 1 and name.isascii() and name.isprintable() and name != " ":
        return name.upper()
    return None


def capture(keyname: str, mods: frozenset) -> tuple:
    """One key-press of the shortcut-capture state machine.

    `keyname` is the Gdk keyval name, `mods` the held modifiers as a subset of
    MODIFIERS.  Returns (status, binding):
      wait   – a lone modifier went down; keep listening
      cancel – Escape with no modifiers
      clear  – Backspace with no modifiers (disable the action)
      done   – binding is the captured shortcut
      reject – not usable (a bare typing key, an unknown key)
    """
    if keyname in _LONE_MODIFIERS:
        return ("wait", "")
    if not mods and keyname == "Escape":
        return ("cancel", "")
    if not mods and keyname == "BackSpace":
        return ("clear", "")
    key = _canonical_key(keyname)
    if key is None:
        return ("reject", "")
    if not mods and key not in _BARE_OK:
        return ("reject", "")
    binding = "+".join([m for m in MODIFIERS if m in mods] + [key])
    if not is_valid(binding):
        return ("reject", "")
    return ("done", binding)
