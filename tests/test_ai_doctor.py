"""Statuses of the doctor's package and telemetry rows, with the imports and
the consent file faked."""

import types

import pytest

from vino_ai import doctor, telemetry


def _rows_with_missing(monkeypatch, missing):
    monkeypatch.setattr(doctor, "_version", lambda m: None if m == missing else "1.0")
    return {r.label: r for r in doctor._packages()}


def test_a_missing_optional_package_is_a_warn(monkeypatch):
    rows = _rows_with_missing(monkeypatch, "vino_speak")
    assert rows["vinospeak"].status == doctor.WARN and rows["vinospeak"].fix


def test_a_missing_core_package_is_a_fail(monkeypatch):
    rows = _rows_with_missing(monkeypatch, "openvino_genai")
    assert rows["openvino-genai"].status == doctor.FAIL


@pytest.fixture
def blocked_telemetry(monkeypatch):
    """openvino looks installed and its telemetry blocked; returns a setter
    for what the consent file says."""
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _m: types.SimpleNamespace())
    monkeypatch.setattr(telemetry, "prepare", lambda: None)
    monkeypatch.setattr(telemetry, "blocked", lambda: True)

    def consent(state):
        monkeypatch.setattr(doctor, "_consent_file", lambda home=None: state)
        return {r.label: r for r in doctor._telemetry()}["telemetry opt-out"]

    return consent


def test_an_opt_out_is_ok(blocked_telemetry):
    assert blocked_telemetry("opted out").status == doctor.OK


@pytest.mark.parametrize("state", ["absent", "opted in"])
def test_no_opt_out_is_info_not_a_warn(blocked_telemetry, state):
    row = blocked_telemetry(state)
    assert row.status == doctor.UNKNOWN and row.fix
