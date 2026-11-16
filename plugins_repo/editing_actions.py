# vinopop-plugin-version: 3
"""Editing actions: Cut, Paste, Paste & Enter, Backspace, Select All.

Each sends the usual shortcut through the platform backend. The popup
never takes keyboard focus, so the keys land in the app the text was
selected in; in a read-only view they do nothing.
"""

from __future__ import annotations

from plugin_base import Plugin

# Electron apps debounce input: an Enter sooner than this after the paste
# submits the field empty
_PASTE_SETTLE_S = 0.08


def _send_keys(combo: str) -> None:
    from platform_backend import get_backend

    get_backend().send_key(combo)


def _cut(_text: str) -> None:
    _send_keys("ctrl+x")


def _paste(_text: str) -> None:
    _send_keys("ctrl+v")


# WM_CLASS substrings of apps where plain Return inserts a newline
_NEWLINE_ENTER_CLASSES = (
    "slack",
    "discord",
    "ms-teams",
    "teams-for-linux",
    "element",
    "thunderbird",
)


def _submit_keystroke_for_focus() -> str:
    """Ctrl+Return in the _NEWLINE_ENTER_CLASSES apps, plain Return
    everywhere else (pinned by
    `tests/test_plugins_editing.py::test_paste_and_enter_submits_with_ctrl_return_in_slack`,
    `tests/test_plugins_editing.py::test_paste_and_enter_submits_with_return_elsewhere`)."""
    from platform_backend import get_backend

    blob = " ".join(get_backend().active_window_haystacks())
    for needle in _NEWLINE_ENTER_CLASSES:
        if needle in blob:
            return "ctrl+Return"
    return "Return"


def _paste_and_enter(_text: str) -> None:
    import time as _t

    submit_key = _submit_keystroke_for_focus()
    _send_keys("ctrl+v")
    _t.sleep(_PASTE_SETTLE_S)
    _send_keys(submit_key)


def _backspace(_text: str) -> None:
    _send_keys("BackSpace")


def _select_all(_text: str) -> None:
    _send_keys("ctrl+a")


def register(register_plugin) -> None:
    # Priorities 11-15: right after Copy (10), so the editing buttons sit together
    register_plugin(
        Plugin(
            name="cut",
            icon="edit-cut-symbolic",
            tooltip="Cut",
            handler=_cut,
            content_types=(),
            priority=11,
            requires_editable=True,
        )
    )
    register_plugin(
        Plugin(
            name="paste",
            icon="edit-paste-symbolic",
            tooltip="Paste (replace selection with clipboard)",
            handler=_paste,
            content_types=(),
            priority=12,
            requires_editable=True,
        )
    )
    register_plugin(
        Plugin(
            name="paste-and-enter",
            icon="go-jump-symbolic",
            tooltip="Paste & Enter (paste clipboard, then press Return)",
            handler=_paste_and_enter,
            content_types=(),
            priority=15,
            requires_editable=True,
        )
    )
    register_plugin(
        Plugin(
            name="backspace",
            # Not -rtl: that one points right, for right-to-left languages
            icon="edit-clear-symbolic",
            tooltip="Backspace (delete the selected text)",
            handler=_backspace,
            content_types=(),
            priority=13,
            requires_editable=True,
        )
    )
    # Not requires_editable: Ctrl+A extends a selection in read-only views too
    register_plugin(
        Plugin(
            name="select-all",
            icon="edit-select-all-symbolic",
            tooltip="Select all",
            handler=_select_all,
            content_types=(),
            priority=14,
        )
    )
