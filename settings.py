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

from xdg_paths import CONFIG_DIR, make_private, open_private, private_dir

CONFIG_PATH = CONFIG_DIR / "settings.json"

DEFAULTS: dict[str, Any] = {
    # UI theme: "dark", "light", or "system" (auto-detect from the
    # GTK theme name + prefer-dark-theme flag). Dark by default - the
    # premium palette was designed against the cobalt + violet scheme.
    "theme": "dark",
    # Popup button (action chip) size in pixels - the min-width / min-
    # height of each button in the floating selection popup. Icon
    # scales proportionally. Clamped to [14, 48] by popup.py.
    "popup_button_size": 22,
    # Show the popup automatically when the PRIMARY selection changes
    "show_on_selection": True,
    # PopClip-style: modifier+double-click anywhere pops the edit menu
    # (Paste / Select all / Backspace) at the cursor. The modifier
    # is required so the gesture doesn't collide with the app's own
    # double-click-to-select-a-word behaviour.
    # Off by default because turning it on means vinoPop watches all
    # mouse clicks globally via the X11 RECORD extension. Nothing is
    # logged or transmitted - we only look for the chosen chord.
    "double_click_popup_enabled": False,
    # Which modifier key has to be held for the double-click chord.
    # One of "ctrl", "shift", "alt", "super". Read fresh each click
    # so a setting change takes effect without restarting the daemon.
    "double_click_modifier": "ctrl",
    # Modifier the user holds while making a selection to force the
    # popup to show EVERY installed plugin, bypassing the classifier's
    # content-type filter. Useful when the classifier got it wrong and
    # the action you actually want isn't in the filtered set (e.g. the
    # selection was misclassified as 'command' so Send-to-AI buttons
    # disappeared). One of "alt", "ctrl", "shift", "super", or empty
    # to disable.
    #
    # Off by default because every option conflicts with something:
    #   alt   - Cinnamon/GNOME mouse-button-modifier (Alt+drag moves
    #           windows on default installs)
    #   super - Same on some setups; often the WM's primary modifier
    #   ctrl  - Text editors / browsers use Ctrl+drag for word-by-word
    #           selection or multi-cursor
    #   shift - Extends an existing selection in every text widget
    # The user picks the lesser evil in Settings > Activation.
    "popup_force_all_modifier": "",
    # Hotkey to summon the popup with the current PRIMARY selection at the cursor.
    # Format: "<modifiers>+<key>", e.g. "super+shift+y", "ctrl+alt+y", "super+space".
    # Set to null/empty to disable. Use the recorder in Settings to capture
    # a combo by pressing it.
    "hotkey": "super+shift+y",
    # Master on/off for the clipboard plugin. When False, the background
    # selection-watcher thread is NOT started, the picker hotkey does
    # nothing, and the popup button is hidden. Use this if you'd rather
    # not have vinoPop track your clipboard at all.
    "clipboard_history_enabled": True,
    # Hotkey to open the clipboard / snippets picker. Press, type to filter,
    # Enter to paste at the cursor. Ignored if clipboard_history_enabled
    # is False.
    # Default avoids Meta+V, which KDE Klipper / clipboard managers grab.
    "clipboard_hotkey": "super+shift+v",
    # Hotkey to capture a screen region and OCR it (offline, via RapidOCR;
    # needs a screen-capture tool, see Settings). Empty string disables. Useful for
    # reaching text inside PDF viewers, video frames, OS chrome, and
    # custom editors that don't propagate to X11 PRIMARY selection.
    "ocr_hotkey": "shift+super+o",
    # Snippet triggers - when ON, typing a snippet's shortcode followed
    # by space/tab/enter/punctuation auto-expands it in place. Requires
    # global keystroke monitoring via the X11 RECORD extension. Off by
    # default because of the privacy implication; keystrokes are matched
    # against your snippet triggers locally and never logged or sent.
    "snippet_triggers_enabled": False,
    # Per-app/site blocklist for trigger expansion only. Case-insensitive
    # substring match against the focused window's title AND WM_CLASS.
    # Use it to silence expansion in password managers, terminals, your
    # bank's website, etc. without disabling triggers globally.
    "trigger_blocklist_patterns": [],
    # Shell extension in snippets - when ON, {shell:CMD} tokens execute
    # bash and inject stdout. Off by default because importing a snippet
    # from elsewhere with a hostile {shell:...} would run code on your
    # machine; treat this like enabling macros in a document. 5 s timeout.
    "snippet_shell_enabled": False,
    # Clipboard history depth. 25 is enough that you can scroll back
    # to anything you copied within the last day of work; bigger
    # values just make the picker harder to search.
    "clipboard_history_size": 25,
    # Capture image content too (not just text). PNG copies from
    # screenshot tools or browser drag-out land here as well.
    "clipboard_history_images": True,
    # Copies bigger than this aren't kept in history (a 20 MB log would
    # otherwise be rewritten to disk on every later copy). Text in KB,
    # images in MB.
    "clipboard_history_max_text_kb": 1024,
    "clipboard_history_max_image_mb": 20,
    # Fallback poll interval when the XFixes selection-event path
    # isn't available. Most modern X servers do support XFixes so
    # this rarely fires.
    "clipboard_poll_interval": 0.5,
    # Shared snippet variables. Reusable values that snippets can pull
    # in via {var:NAME}. Define once (your email, signature, phone,
    # company name) and reference everywhere - change it here, every
    # snippet picks up the new value next paste. Stored as a dict of
    # {name: value} string pairs.
    "snippet_variables": {},
    # Which selection the hotkey reads: "primary" (highlight) or "clipboard"
    "hotkey_source": "primary",
    # Default ON. Poll the keyboard state every 50 ms instead of
    # registering an XGrabKey. CPU cost measured at <0.1 % per hotkey
    # in `top` (the calls complete in microseconds; theoretical worst-
    # case is 0.4 %). Bypasses WM-level grab conflicts - Cinnamon's
    # muffin defers Super-key dispatching to detect tap-vs-hold and
    # eats the first press of Shift+Super combos on its way through
    # the compositor event filter (see linuxmint/cinnamon #549). The
    # XGrabKey path is still wired up for power users who want pure
    # event-driven behaviour: flip this off in Settings.
    "hotkey_use_polling": True,
    # Target language for the Translate plugin (ISO code, e.g. "en", "no",
    # "de"). Changeable from the dropdown in the translation bubble itself.
    "translate_target_lang": "en",
    # OpenVINO device for the offline AI actions: "auto" (LLM on the NPU,
    # OCR on the CPU, the rest as fallback), or force "NPU", "GPU" or "CPU".
    "ai_device": "auto",
    # Local LLM for summarize / rewrite / explain / translate. Keys are in
    # vino_ai/models.py: "gemma-4-e2b" (default) or "gemma-4-e4b".
    "ai_llm_model": "gemma-4-e2b",
    # Download the LLM in the background at daemon start, so the first AI
    # click doesn't wait for it. It's ~4 GB, so it's off until the user says
    # yes: onboarding asks, and Settings > Advanced has the switch. Left off,
    # the model downloads on the first AI click instead.
    "ai_prefetch_model": False,
    # Set once the user has answered (onboarding or the Settings switch).
    # ai_prefetch_model used to default to True and save() writes every
    # default into settings.json, so a stored True without this isn't a
    # yes. See vino_ai.models.prefetch_consented.
    "ai_prefetch_asked": False,
    # After an OCR capture, let the AI model (Gemma 4) describe the image:
    # a Describe image button on the notification when OCR found text, and
    # straight away when it found none. ~10 s per image on the NPU.
    "ocr_describe_images": False,
    # Seconds to wait for a device to load the LLM before falling back to
    # the next (NPU -> GPU -> CPU). A slow first NPU compile keeps running
    # in the background and the model moves to the NPU once it's done.
    "ai_load_timeout_seconds": 120.0,
    # Minutes the LLM may sit unused before it's unloaded to free its
    # memory (~4 GB). The compile cache keeps the next load quick. 0 keeps
    # it loaded for the life of the daemon.
    "ai_idle_unload_minutes": 5.0,
    # Let OpenVINO's bundled telemetry send Intel its anonymous usage stats
    # (through Google Analytics; never your text). Off by default: vinoPop
    # writes "0" to ~/intel/openvino_telemetry before the first OpenVINO
    # import. See vino_ai/telemetry.py.
    "openvino_telemetry": False,
    # Set once vinoPop has written that file. A file it didn't write (from
    # Intel's opt_in_out script) is left alone until the toggle is flipped.
    "openvino_telemetry_file_owned": False,
    # Milliseconds before the popup auto-hides if the mouse never enters
    # it. 6.5 s leaves enough time to read the buttons without overstaying
    # - 8 s felt sluggish in practice. Tunable via Settings → Timing.
    "auto_hide_initial_ms": 6500,
    # Milliseconds before hide after the mouse leaves the popup's safe
    # zone. 4 s is forgiving for re-entry; 1.5 s felt twitchy.
    "auto_hide_leave_ms": 4000,
    # Minimum text length to trigger the popup on selection. 2 chars
    # filters most accidental single-letter selections (drag-overshoot,
    # stray double-click) without blocking real one-syllable words -
    # those are typically 3+ chars anyway.
    "min_selection_length": 2,
    # How long to wait (ms) after the last selection change before showing
    # the popup. Suppresses popup churn while the user is still dragging
    # to extend a selection. ~250-400 ms feels natural; lower = snappier
    # but more likely to interrupt; higher = calmer but feels laggy.
    # How long to wait after the last selection event before showing
    # the popup. The watcher fires an XFixes event each time the user
    # extends the highlight, so we need *some* debounce to let a
    # drag-to-extend gesture settle - but the cost of being too high
    # is that the popup feels laggy after a fast highlight + release.
    # 150 ms is in the sweet spot: humans rarely re-extend within
    # 150 ms of the last move, and 150 ms is below the "this feels
    # delayed" threshold.
    "selection_debounce_ms": 150,
    # If True, ignore selections that contain only whitespace
    "ignore_whitespace_only": True,
    # Substrings that, if any matches the active window's title or
    # WM_CLASS (case-insensitive), suppress the popup entirely. Useful
    # for password managers, banking sites, etc. One entry per pattern.
    # Examples: "KeePassXC", "DNB - Mozilla Firefox", "1Password".
    "blocklist_patterns": [],
    # Extra WM_CLASS substrings to treat as read-only contexts. The
    # built-in list already covers Evince, Okular, image viewers, file
    # managers, etc.; add app classes here if their windows are mostly
    # for reading (Cut/Paste/Backspace buttons get hidden in them).
    # Only consulted when AT-SPI didn't return a definite answer.
    "readonly_app_classes": [],
    # Hard cap on how many action buttons the popup will draw. Plugins
    # are ranked by priority + your custom plugin_order; anything past
    # the cap is silently dropped (NOT moved to an overflow menu -
    # raise this if you want everything visible). Stops the popup from
    # being a 25-icon bar when many plugins are installed.
    # Cap before the popup either wraps to two rows or appends a "+N"
    # overflow chip. Default sized to fit the typical engaged-user
    # plugin set (12-20) across two rows without truncation. Bump it
    # if you want every action visible regardless of how many you
    # enable; drop it for a tighter single-row look.
    "max_popup_buttons": 24,
    # What the popup does when more actions match than fit on one line:
    #   "wrap"   -> spill onto a second row (classic two-line look),
    #   "expand" -> keep one tidy line, with a chevron that reveals the rest
    #               on click (recommended: compact, nothing dropped),
    #   "cap"    -> one row only; the remainder sit behind a "+N" chip.
    # All modes are still bounded by max_popup_buttons above.
    "popup_overflow_mode": "expand",
    # Icon style for branded/utility plugins:
    #   "color" -> vibrant gradient tiles (default)
    #   "glyph" -> uniform mono glyphs that match the plain text-edit icons
    "icon_style": "color",
    # System-tray icon appearance. Auto-recolouring isn't reliable on KDE
    # (plasmashell won't recolour a custom symbolic icon, and the app's own
    # colour scheme can differ from the panel theme), so the user picks:
    #   "color" -> symbolic glyph recoloured to the panel (default)
    #   "light" -> light monochrome glyph; for DARK panels
    #   "dark"  -> dark monochrome glyph; for LIGHT panels
    "tray_icon_style": "color",
    # If True (default): after the command, drop into an interactive shell so
    #   output stays visible. Close with exit/Ctrl-D/X.
    # If False: terminal closes immediately after the command exits (output lost).
    "terminal_keep_open": True,
    # User-defined ordering of plugin buttons in the popup. List of plugin
    # names (the `name` field, e.g. "copy", "clipboard-history"). Plugins
    # listed here appear first in this order; unlisted plugins fall back
    # to their built-in priority. Edit via Plugin Manager → Order tab.
    "plugin_order": [],
    # Plugin names (Plugin.name) the user has switched off inside a
    # bundled .py file. The plugin_loader silently drops these at
    # register time. Lets users keep "Paste" but hide "Paste & Enter"
    # without removing the editing_actions bundle, etc.
    "disabled_plugins": [],
    # If True, "Run in terminal" pops a confirmation dialog showing the exact
    # command before launching. Recommended - protects against highlighting
    # a malicious-looking string and clicking the wrong button.
    "terminal_confirm_run": True,
    # Which search engine the "Search the web" popup button uses. Recognised
    # values: "google", "duckduckgo", "bing", "brave", "startpage", "ecosia",
    # "kagi", "qwant", "yandex", or "custom".
    # See actions.SEARCH_ENGINES for the full table.
    "search_engine": "google",
    # Used when search_engine == "custom". Must contain '{q}' which gets
    # replaced with the URL-encoded selection. Example for searx:
    #   "https://searx.example.com/search?q={q}"
    "search_engine_custom_url": "",
    # 'Skip short auto-popup selections' filter. Off by default to
    # match PopClip out of the box (no minimum-size knob there). When
    # on, the watcher silently drops selections shorter than
    # min_selection_length - useful if a flaky app keeps firing
    # X selection events for accidental clicks. The hotkey always
    # ignores this filter; see main.py _start_watcher.
    "min_selection_length_enabled": False,
    # AT-SPI focus-event listener. Lets us tell apart 'cursor in chat
    # input' from 'cursor in chat history' inside Electron apps where
    # the synchronous AT-SPI tree-walk dead-ends. Removed from the
    # Settings UI 2026-05-29 because the modifier+double-click path
    # covers the main use case and AT-SPI was correlated with a
    # Cinnamon panel segfault on 2026-05-25 (xapp-sn-watcher threw
    # ATK_IS_STATE_SET assertions, cinnamon dereferenced a freed
    # GObject and crashed the panel).
    # Stays here as a hidden settings.json knob for the rare user who
    # explicitly wants AT-SPI back on. Default off.
    "editable_atspi_listener_enabled": False,
    # Anchor the popup to the SELECTED-TEXT rectangle (via AT-SPI screen
    # extents) instead of the mouse pointer. On by default, but it only has
    # any effect when editable_atspi_listener_enabled is also True AND the
    # focused app exposes accessibility - otherwise the popup silently falls
    # back to the mouse pointer (so leaving this on costs nothing).
    "popup_anchor_to_selection": True,
    # If True, show the one-time welcome dialog on first run. Set to False
    # to skip it (mostly useful for screencasts / CI testing).
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
        "openvino_telemetry",
        "openvino_telemetry_file_owned",
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
