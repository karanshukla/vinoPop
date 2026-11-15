"""Simple JSON-backed settings for vinoPop.

Lives at ~/.config/vinopop/settings.json. Missing file or missing keys
fall back to defaults. Unknown keys in the file are preserved on save so
the user can hand-edit and add comments-as-keys.

Only keys the user set are written, so a default changed in a later release
reaches everyone who never touched it. Files from before that held every
default; they're thinned once on load, see Settings._migrate().
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

from core.xdg_paths import CONFIG_DIR, make_private, open_private, private_dir

CONFIG_PATH = CONFIG_DIR / "settings.json"

# docs/how-it-works.md#settings describes every key and why its default is what
# it is; tests/test_settings_docs.py fails when a key is missing there.
DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "popup_button_size": 22,
    "show_on_selection": True,
    "double_click_popup_enabled": False,
    "double_click_modifier": "ctrl",
    "popup_force_all_modifier": "",
    "hotkey": "super+shift+y",
    "clipboard_history_enabled": True,
    "clipboard_hotkey": "super+shift+v",
    "ocr_hotkey": "shift+super+o",
    "snippet_triggers_enabled": False,
    "trigger_blocklist_patterns": [],
    "snippet_shell_enabled": False,
    "clipboard_history_size": 25,
    "clipboard_history_images": True,
    "clipboard_history_max_text_kb": 1024,
    "clipboard_history_max_image_mb": 20,
    "clipboard_poll_interval": 0.5,
    "snippet_variables": {},
    "hotkey_source": "primary",
    "hotkey_use_polling": True,
    "translate_target_lang": "en",
    "ai_device": "auto",
    "ai_llm_model": "gemma-4-e2b",
    "ai_prefetch_model": False,
    "ai_prefetch_asked": False,
    "ocr_describe_images": False,
    "ai_load_timeout_seconds": 120.0,
    "ai_idle_unload_minutes": 5.0,
    "auto_hide_initial_ms": 6500,
    "auto_hide_leave_ms": 4000,
    "min_selection_length": 2,
    "selection_debounce_ms": 150,
    "ignore_whitespace_only": True,
    "blocklist_patterns": [],
    "readonly_app_classes": [],
    "max_popup_buttons": 24,
    "popup_overflow_mode": "expand",
    "icon_style": "color",
    "popup_text_labels": False,
    "tray_icon_style": "color",
    "tray_idle_minutes": 0,
    "terminal_keep_open": True,
    "plugin_order": [],
    "disabled_plugins": [],
    "terminal_confirm_run": True,
    "search_engine": "google",
    "search_engine_custom_url": "",
    "min_selection_length_enabled": False,
    "editable_atspi_listener_enabled": False,
    "popup_anchor_to_selection": True,
    "show_welcome_dialog": True,
}


# Bumped when the file needs a one-off migration on load. 1: the file holds
# only keys the user set, not every default.
SETTINGS_VERSION = 1
VERSION_KEY = "_settings_version"

# Kept as stored by the version-1 migration even when they equal today's
# default: consent and privacy switches, where the stored value may be an
# answer the user gave, and a later default flip mustn't override it.
# ai_prefetch_model's old default (True) is also read on purpose by
# vino_ai.models.prefetch_consented (PR #47), so it has to stay put.
_KEEP_ON_MIGRATION = frozenset(
    {
        "ai_prefetch_model",
        "ai_prefetch_asked",
        "clipboard_history_enabled",
        "clipboard_history_images",
        "double_click_popup_enabled",
        "snippet_triggers_enabled",
        "snippet_shell_enabled",
        "editable_atspi_listener_enabled",
    }
)


def _current(version: Any) -> bool:
    return type(version) is int and version >= SETTINGS_VERSION


def _same(a: Any, b: Any) -> bool:
    # type() too, so a hand-written 1 isn't taken for the default True
    return type(a) is type(b) and a == b


def as_number(key: str, value: Any, kind: type[int] | type[float], fallback: Any) -> Any:
    """`value` as an int or float, or `fallback` (with a log line) when a
    hand edit made it something else: text, true/false, null, NaN."""
    try:
        if isinstance(value, bool):
            raise ValueError("true/false isn't a number")
        num = kind(value)
        if not math.isfinite(num):
            raise ValueError("not a finite number")
        return num
    except (TypeError, ValueError, OverflowError) as exc:
        print(f"[settings] {key} = {value!r} isn't usable ({exc}), using {fallback!r}")
        return fallback


# What "Reset to defaults" leaves alone: which plugins are switched off and
# their button order belong to the Plugin Manager, and the reset dialog
# promises installed plugins are kept.
RESET_KEEPS = ("disabled_plugins", "plugin_order")


class Settings:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        # Set when reload() migrated the file; ensure_written() saves it.
        self._migrated = False
        self.reload()

    def reload(self) -> None:
        self._data = {}
        if self.path.is_file():
            make_private(self.path)  # files from older versions were 0644
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
                    self._migrate()
                else:
                    print(f"[settings] {self.path} is not a JSON object, ignoring")
                    self._keep_backup()
            # ValueError covers JSONDecodeError and UnicodeDecodeError (a
            # hand edit saved as latin-1).
            except (OSError, ValueError) as exc:
                print(f"[settings] failed to read {self.path}: {exc}")
                self._keep_backup()

    def _migrate(self) -> None:
        """Bring a file from an older version up to SETTINGS_VERSION.

        Before version 1, save() wrote every default into the file, so a
        stored value equal to today's default is almost always one the user
        never chose. Those are dropped, once, so later default changes reach
        them. The cost: a user who deliberately picked what is also the
        default reads the same value today but follows the default if a
        later release changes it."""
        if _current(self._data.get(VERSION_KEY)):
            return
        for key, default in DEFAULTS.items():
            if key in _KEEP_ON_MIGRATION or key not in self._data:
                continue
            if _same(self._data[key], default):
                del self._data[key]
        self._data[VERSION_KEY] = SETTINGS_VERSION
        self._migrated = True

    def _keep_backup(self) -> None:
        """Park an unreadable file next to itself: the next save() would
        otherwise replace the user's hand-edited (and merely malformed)
        settings with defaults."""
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        try:
            shutil.copy2(self.path, backup)
            print(f"[settings] kept a copy of the unreadable file at {backup}")
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        if key in DEFAULTS:
            # Lists/dicts are copied so a caller mutating the result can't
            # change the defaults for everyone else.
            return copy.deepcopy(DEFAULTS[key])
        return default

    def get_int(self, key: str) -> int:
        """get() for a numeric setting that has a default in DEFAULTS. A
        hand-edited value that isn't a number falls back to the default,
        with a log line, instead of raising in whoever reads it."""
        return int(as_number(key, self.get(key), int, DEFAULTS[key]))

    def get_float(self, key: str) -> float:
        """get_int() for a float setting."""
        return float(as_number(key, self.get(key), float, DEFAULTS[key]))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def reset(self) -> None:
        """Forget every preference (unknown keys too). Nothing is saved
        until save()."""
        self._data = {VERSION_KEY: SETTINGS_VERSION}

    def save(self) -> None:
        private_dir(self.path.parent)
        # Only what the user set, so a later change to a default reaches them.
        merged = dict(self._data)
        if not _current(merged.get(VERSION_KEY)):
            merged[VERSION_KEY] = SETTINGS_VERSION
        self._migrated = False
        # Atomic write: write to tmp, fsync, then rename. Avoids losing
        # all settings if the process dies (OOM, power) mid-write.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open_private(tmp) as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)

    def reset_to_defaults(self) -> None:
        """Put every preference back to its default and save. Keys in
        RESET_KEEPS are plugin choices made in the Plugin Manager, not
        preferences, so they survive."""
        kept = {k: self._data[k] for k in RESET_KEEPS if k in self._data}
        self.reset()
        self._data.update(kept)
        self.save()

    def ensure_written(self) -> None:
        """Create the file if it doesn't exist yet (it holds no defaults,
        only the version), or save it once reload() has migrated it so the
        migration runs only once."""
        if not self.path.is_file():
            self.save()
            print(f"[settings] created {self.path}")
        elif self._migrated:
            # The old file, for anyone who wants to see what was dropped.
            old = self.path.with_suffix(self.path.suffix + f".pre-v{SETTINGS_VERSION}")
            try:
                if not old.exists():
                    shutil.copy2(self.path, old)
            except OSError:
                pass
            self.save()
            print(f"[settings] dropped stored defaults from {self.path}, old file at {old}")


# Module-level singleton for convenience
_singleton: Settings | None = None

# `--doctor` and other read-only callers switch this off so merely looking at
# a setting never creates ~/.config/vinopop/settings.json.
write_defaults = True


def get_settings() -> Settings:
    global _singleton
    if _singleton is None:
        _singleton = Settings()
        if write_defaults:
            _singleton.ensure_written()
    return _singleton
