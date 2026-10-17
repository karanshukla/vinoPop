"""OpenVINO telemetry opt-out (#19). A stand-in `openvino` package does
what the real one does at import: no consent file, or "1", means it posts
to Google Analytics. So these run without OpenVINO installed."""

import importlib
import sys
import urllib.request

import pytest

import settings
from settings import Settings
from vino_ai import doctor, telemetry

FAKE_OPENVINO = """
from pathlib import Path
import urllib.request

_consent = Path.home() / "intel" / "openvino_telemetry"
if not _consent.exists() or _consent.read_text().strip() == "1":
    urllib.request.urlopen("https://www.google-analytics.com/mp/collect")
__version__ = "0.0-fake"
"""


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp HOME and settings.json, a fake openvino on sys.path, urlopen
    recorded, and prepare() not yet run in this process."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "_singleton", Settings(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "write_defaults", True)
    monkeypatch.setattr(telemetry, "_prepared", False)

    pkg = tmp_path / "site" / "openvino"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(FAKE_OPENVINO)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.delitem(sys.modules, "openvino", raising=False)
    importlib.invalidate_caches()

    calls = []

    def _urlopen(url, *a, **kw):
        calls.append(url)
        raise OSError("no network in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    yield home, calls
    sys.modules.pop("openvino", None)


def _consent(home):
    return (home / "intel" / "openvino_telemetry").read_text()


def test_fake_openvino_phones_home_without_opt_out(env):
    # Guards the stand-in: without prepare() the import does call out.
    _home, calls = env
    with pytest.raises(OSError):
        importlib.import_module("openvino")
    assert calls


def test_prepare_opts_out_before_import(env):
    home, calls = env
    telemetry.prepare()
    importlib.import_module("openvino")
    assert _consent(home) == "0"
    assert calls == []
    s = settings.get_settings()
    assert s.get("openvino_telemetry") is False
    assert s.get("openvino_telemetry_file_owned") is True


def test_prepare_writes_opt_in_from_setting(env):
    home, _calls = env
    settings.get_settings().set("openvino_telemetry", True)
    telemetry.prepare()
    assert _consent(home) == "1"


def test_prepare_leaves_foreign_consent_file(env):
    # The user ran Intel's opt_in_out --opt_in themselves.
    home, _calls = env
    (home / "intel").mkdir()
    (home / "intel" / "openvino_telemetry").write_text("1")
    telemetry.prepare()
    assert _consent(home) == "1"
    assert telemetry.enabled() is True
    assert settings.get_settings().get("openvino_telemetry_file_owned") is False


def test_toggle_writes_consent_file(env):
    home, _calls = env
    (home / "intel").mkdir()
    (home / "intel" / "openvino_telemetry").write_text("1")
    assert telemetry.set_enabled(False)
    assert _consent(home) == "0"
    assert telemetry.set_enabled(True)
    assert _consent(home) == "1"
    s = Settings(settings.get_settings().path)  # what was saved to disk
    assert s.get("openvino_telemetry") is True
    assert s.get("openvino_telemetry_file_owned") is True


def test_prepare_skips_without_openvino(env, monkeypatch):
    home, _calls = env
    monkeypatch.setattr(telemetry.importlib.util, "find_spec", lambda _name: None)
    telemetry.prepare()
    assert not (home / "intel").exists()


def test_doctor_version_opts_out_first(env, monkeypatch):
    home, calls = env
    monkeypatch.setattr(settings, "write_defaults", False)  # as doctor.main() does
    assert doctor._version("openvino") == "0.0-fake"
    assert _consent(home) == "0"
    assert calls == []
    # --doctor reads only: settings.json is not written.
    assert not settings.get_settings().path.exists()


def test_doctor_telemetry_row(env):
    home, _calls = env
    (home / "intel").mkdir()
    path = home / "intel" / "openvino_telemetry"
    path.write_text("0")
    assert doctor._telemetry()[0].status == doctor.OK
    path.write_text("1")
    row = doctor._telemetry()[0]
    assert row.status == doctor.WARN and "ENABLED" in row.detail


def test_real_openvino_telemetry_reads_our_file(env):
    """Our "0" is what Intel's checker treats as declined."""
    checker_mod = pytest.importorskip("openvino_telemetry.utils.opt_in_checker")
    home, _calls = env
    telemetry.prepare()
    checker = checker_mod.OptInChecker()
    assert checker.consent_file() == str(home / "intel" / "openvino_telemetry")
    assert checker.check(enable_opt_in_dialog=False) == checker_mod.ConsentCheckResult.DECLINED
