"""Keep OpenVINO's bundled telemetry off unless the user opts in.

`import openvino` initialises Intel's `openvino_telemetry` (through ovc)
with the opt-in dialog switched off, so with no consent file it counts as
consent and forks a process that posts a usage event to Google Analytics.
No selected text is in it, but vinoPop says nothing phones home.

The consent file is ~/intel/openvino_telemetry holding "0" (off) or "1"
(on); openvino_telemetry/utils/opt_in_checker.py reads it at import time.
Before the first import we write it from the `openvino_telemetry` setting
(off by default). A file vinoPop didn't write (the user ran Intel's
`opt_in_out`) is left alone and is what Settings shows, until the user
flips the toggle.

We write the file ourselves rather than run `opt_in_out --opt_out`: that
script sends an "opted out" event to Google Analytics on the way out.

The only environment kill switch is CI=true (ovc passes disable_in_ci).
It isn't set: the daemon passes its environment to the terminals and
commands it launches, and CI=true changes how a lot of tools behave.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("vinopop")

SETTING = "openvino_telemetry"
# True once vinoPop has written the consent file, so Settings can tell its
# own file from one Intel's opt_in_out wrote.
OWNED_SETTING = "openvino_telemetry_file_owned"

_prepared = False


def consent_path() -> Path:
    # Path.home() at call time, not import time, so tests can point HOME
    # at a temp dir.
    return Path.home() / "intel" / "openvino_telemetry"


def read_consent() -> bool | None:
    """True/False from the consent file, None if there isn't one. Anything
    but "1" counts as off, as it does for openvino_telemetry."""
    try:
        return consent_path().read_text(encoding="utf-8").strip() == "1"
    except FileNotFoundError:
        return None
    except OSError:
        return False


def write_consent(enabled: bool) -> bool:
    path = consent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("1" if enabled else "0", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("[vino_ai] couldn't write %s: %s", path, exc)
        return False
    return True


def enabled() -> bool:
    """What OpenVINO will do: the consent file if there is one, otherwise
    the setting (which prepare() writes before the first import)."""
    on = read_consent()
    if on is not None:
        return on
    return bool(_settings_get(SETTING, False))


def set_enabled(on: bool) -> bool:
    """The Settings toggle: write the consent file now and remember it's ours."""
    if not write_consent(on):
        return False
    _record(on)
    return True


def prepare() -> None:
    """Call before anything imports openvino, openvino_genai or rapidocr.
    Cheap after the first call, and a no-op without OpenVINO installed so
    a box that never uses the AI actions doesn't get an ~/intel folder."""
    global _prepared
    if _prepared:
        return
    _prepared = True
    if "openvino" in sys.modules:
        # Too late to change consent for this process; shouldn't happen.
        log.warning("[vino_ai] openvino was imported before the telemetry opt-out")
        return
    if importlib.util.find_spec("openvino") is None:
        return
    if read_consent() is not None:
        return
    on = bool(_settings_get(SETTING, False))
    if write_consent(on):
        _record(on)


def _settings_get(key: str, default):
    try:
        from settings import get_settings

        return get_settings().get(key, default)
    except Exception:  # noqa: BLE001 - fall back to telemetry off
        return default


def _record(on: bool) -> None:
    try:
        import settings

        s = settings.get_settings()
        s.set(SETTING, on)
        s.set(OWNED_SETTING, True)
        # `--doctor` reads only; the consent file is still written there
        # (it stops the import writing ~/intel/stats), just not settings.json.
        if settings.write_defaults:
            s.save()
    except Exception as exc:  # noqa: BLE001
        log.warning("[vino_ai] couldn't save the telemetry setting: %s", exc)
