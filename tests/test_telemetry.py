"""OpenVINO telemetry is blocked, not opted out of (#19, #55). Stand-in
`openvino` and `openvino_telemetry` packages do what the real ones do at
import: openvino loads openvino_telemetry if it can, and that posts to
Google Analytics. So these run without OpenVINO installed."""

import importlib
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from vino_ai import doctor, telemetry

FAKE_OPENVINO = """
try:
    import openvino_telemetry
except ImportError:
    pass  # ovc's telemetry_stub
else:
    openvino_telemetry.send()
__version__ = "0.0-fake"
"""

FAKE_TELEMETRY = """
import urllib.request

def send():
    urllib.request.urlopen("https://www.google-analytics.com/mp/collect")
"""

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp HOME, fake packages on sys.path, urlopen recorded, and neither
    package loaded or blocked in this process yet."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    site = tmp_path / "site"
    for name, source in (("openvino", FAKE_OPENVINO), ("openvino_telemetry", FAKE_TELEMETRY)):
        (site / name).mkdir(parents=True)
        (site / name / "__init__.py").write_text(source)
    monkeypatch.syspath_prepend(str(site))
    for name in ("openvino", "openvino_telemetry"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.invalidate_caches()

    calls = []

    def _urlopen(url, *a, **kw):
        calls.append(url)
        raise OSError("no network in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    yield home, calls
    for name in ("openvino", "openvino_telemetry"):
        sys.modules.pop(name, None)


def test_fake_openvino_phones_home_unless_blocked(env):
    # Guards the stand-in: without prepare() the import does call out.
    _home, calls = env
    with pytest.raises(OSError):
        importlib.import_module("openvino")
    assert calls


def test_prepare_blocks_it_before_import(env):
    home, calls = env
    telemetry.prepare()
    telemetry.prepare()  # repeat calls are fine
    assert telemetry.blocked()
    assert importlib.import_module("openvino").__version__ == "0.0-fake"
    assert calls == []
    assert not (home / "intel").exists()


def test_users_consent_file_is_left_alone(env):
    home, calls = env
    (home / "intel").mkdir()
    consent = home / "intel" / "openvino_telemetry"
    consent.write_text("1")  # opted in for their other OpenVINO tools
    telemetry.prepare()
    importlib.import_module("openvino")
    assert calls == []
    assert consent.read_text() == "1"


def test_doctor_reports_the_block(env):
    _home, calls = env
    assert doctor._version("openvino") == "0.0-fake"
    assert calls == []
    row = doctor._telemetry()[0]
    assert row.status == doctor.OK and "blocked" in row.detail


def test_real_openvino_writes_nothing_to_intel(tmp_path):
    """The real package, in a fresh process: no GA client id, no stats."""
    pytest.importorskip("openvino_telemetry")
    env = {**os.environ, "HOME": str(tmp_path), "https_proxy": "http://127.0.0.1:9"}
    code = "from vino_ai import telemetry; telemetry.prepare(); import openvino; openvino.Core()"
    subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, check=True, timeout=120)
    assert not (tmp_path / "intel").exists()
