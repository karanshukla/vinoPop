"""Translate the selection in place, offline.

Clicking Translate runs the selection through the local OpenVINO model and
streams the translation into a small bubble over the selection. The bubble
has a language picker to change the target on the fly; the choice is
remembered in settings (`translate_target_lang`). Nothing leaves the
machine.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from plugin_base import Plugin

log = logging.getLogger("linuxpop")

# (code, label in its own language, English name for the prompt)
_LANGS = [
    ("en", "English", "English"),
    ("no", "Norsk", "Norwegian"),
    ("sv", "Svenska", "Swedish"),
    ("da", "Dansk", "Danish"),
    ("de", "Deutsch", "German"),
    ("fr", "Français", "French"),
    ("es", "Español", "Spanish"),
    ("it", "Italiano", "Italian"),
    ("nl", "Nederlands", "Dutch"),
    ("pt", "Português", "Portuguese"),
    ("pl", "Polski", "Polish"),
    ("ru", "Русский", "Russian"),
    ("uk", "Українська", "Ukrainian"),
    ("fi", "Suomi", "Finnish"),
    ("cs", "Čeština", "Czech"),
    ("tr", "Türkçe", "Turkish"),
    ("ar", "العربية", "Arabic"),
    ("hi", "हिन्दी", "Hindi"),
    ("zh-CN", "中文", "Simplified Chinese"),
    ("ja", "日本語", "Japanese"),
    ("ko", "한국어", "Korean"),
]
_ENGLISH_NAME = {code: name for code, _label, name in _LANGS}


def _target_lang() -> str:
    try:
        from settings import get_settings

        code = get_settings().get("translate_target_lang") or "en"
    except Exception:
        code = "en"
    return code if code in _ENGLISH_NAME else "en"


def _save_target_lang(code: str) -> None:
    try:
        from settings import get_settings

        s = get_settings()
        s.set("translate_target_lang", code)
        s.save()
    except Exception:
        log.exception("[translate] couldn't save the target language")


def _translate(text: str) -> None:
    text = text.strip()
    if not text:
        return
    import ai_bubble
    from vino_ai import ai_worker

    state = {"bubble": None, "code": _target_lang()}

    def _run(bubble) -> None:
        state["bubble"] = bubble
        ai_worker.run_into(bubble, "translate", text, lang=_ENGLISH_NAME[state["code"]])

    def _picker() -> Gtk.Widget:
        combo = Gtk.ComboBoxText()
        for code, label, _name in _LANGS:
            combo.append(code, label)
        combo.set_active_id(state["code"])

        def _changed(c: Gtk.ComboBoxText) -> None:
            code = c.get_active_id()
            bubble = state["bubble"]
            if not code or code == state["code"] or bubble is None:
                return
            state["code"] = code
            _save_target_lang(code)
            bubble.restart()
            import threading

            threading.Thread(target=_run, args=(bubble,), daemon=True).start()

        def _popup(c: Gtk.ComboBoxText, _pspec) -> None:
            # Picking a language happens on a separate surface, outside the bubble
            if state["bubble"] is not None:
                state["bubble"].keep_open = bool(c.get_property("popup-shown"))

        combo.connect("changed", _changed)
        combo.connect("notify::popup-shown", _popup)
        return combo

    ai_bubble.show("Translate to", _run, header_widget_factory=_picker)


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="translate",
            icon="preferences-desktop-locale-symbolic",
            tooltip="Translate",
            handler=_translate,
            content_types=(),  # offer for any selection
            priority=45,
        )
    )
