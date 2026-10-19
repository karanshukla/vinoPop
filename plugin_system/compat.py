"""Bare-name imports for plugins written before the package split.

User plugins in ~/.config/vinopop/plugins were written against a flat
layout (`from classifier import ContentType`, `import actions`). This keeps
them working: a bare name that used to be a top-level module resolves to
the module that replaced it, and it is the same module object, so state is
shared with the app. New code should import from the packages.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

MOVED = {
    "classifier": "core.classifier",
    "actions": "core.actions",
    "settings": "core.settings",
    "xdg_paths": "core.xdg_paths",
    "version": "core.version",
    "autostart": "core.autostart",
    "hotkey": "triggers.hotkey",
    "watcher": "triggers.watcher",
    "mouse_watcher": "triggers.mouse_watcher",
    "editable_detect": "triggers.editable_detect",
    "plugin_base": "plugin_system.plugin_base",
    "plugin_loader": "plugin_system.plugin_loader",
    "recipe_loader": "plugin_system.recipe_loader",
    "gtk4_app": "ui.gtk4_app",
    "theme": "ui.theme",
    "popup": "ui.popup",
    "ai_bubble": "ui.ai_bubble",
    "icon_picker": "ui.icon_picker",
    "icon_style": "ui.icon_style",
    "welcome": "ui.welcome",
    "onboarding": "ui.onboarding",
    "settings_gui": "ui.settings_gui",
    "plugin_manager": "ui.plugin_manager",
    "recipe_wizard": "ui.recipe_wizard",
    "screen_ocr": "capture.screen_ocr",
    "ocr_selector": "capture.ocr_selector",
    "tray": "tray.tray",
    "tray_qt": "tray.tray_qt",
}


class _LegacyFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if path is None and fullname in MOVED:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return importlib.import_module(MOVED[spec.name])

    def exec_module(self, module) -> None:
        pass


def install() -> None:
    """Idempotent. Last on sys.meta_path, so a real top-level module wins."""
    if not any(isinstance(f, _LegacyFinder) for f in sys.meta_path):
        sys.meta_path.append(_LegacyFinder())
