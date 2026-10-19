# vinopop-plugin-version: 1
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

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402
from plugin_base import Plugin  # noqa: E402

log = logging.getLogger("vinopop")

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
        # A feeder tied to this run: a language switch makes it stale, which
        # stops the run at its next token (or before it starts, if it was
        # still queued) and drops anything it already sent.
        sink = bubble.for_run()
        ai_worker.run_into(sink, "translate", text, lang=_ENGLISH_NAME[state["code"]])

    def _picker() -> Gtk.Widget:
        codes = [code for code, _label, _name in _LANGS]
        drop = Gtk.DropDown.new(Gtk.StringList.new([label for _code, label, _n in _LANGS]))
        drop.set_selected(codes.index(state["code"]))
        drop.set_focusable(False)

        def _changed(d: Gtk.DropDown, _pspec) -> None:
            idx = d.get_selected()
            code = codes[idx] if idx < len(codes) else None
            bubble = state["bubble"]
            if not code or code == state["code"] or bubble is None:
                return
            state["code"] = code
            _save_target_lang(code)
            bubble.restart()
            import threading

            threading.Thread(target=_run, args=(bubble,), daemon=True).start()

        def _popup(pop: Gtk.Popover, _pspec) -> None:
            # Picking a language happens on a separate surface, outside the bubble
            if state["bubble"] is not None:
                state["bubble"].keep_open = pop.get_visible()

        drop.connect("notify::selected", _changed)
        # GtkDropDown has no "popup-shown"; watch its internal popover instead.
        child = drop.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover):
                child.connect("notify::visible", _popup)
            child = child.get_next_sibling()
        return drop

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
