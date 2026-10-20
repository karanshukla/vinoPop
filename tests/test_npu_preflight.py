"""The NPU preflight ported from vinoWhisper (#37), run against fake
sysfs/dev/proc/lib trees, plus the doctor's {group, name, status, detail,
fix} rows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vino_ai import doctor, npu_preflight

DRIVER_1_35 = b"npu-linux-driver-ci-1.35.0.20260722\0libopenvino_intel_npu_compiler_l\0"
DRIVER_1_32 = b"npu-linux-driver-ci-1.32.0.20260402\0libnpu_driver_compiler.so\0"
NO_ENV: dict[str, str] = {}
REPO = Path(__file__).resolve().parent.parent


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    return path


def make_tree(
    root: Path,
    *,
    pci: bool = True,
    accel: bool = True,
    module: bool = True,
    driver: bytes | None = DRIVER_1_35,
    compiler: bool = True,
    distro: str = "fedora",
) -> Path:
    slot = root / "sys/bus/pci/devices/0000:00:0b.0"
    if pci:
        _write(slot / "vendor", "0x8086\n")
        _write(slot / "device", "0x7d1d\n")
        _write(slot / "class", "0x120000\n")
        (root / "sys/bus/pci/drivers/intel_vpu").mkdir(parents=True)
        (slot / "driver").symlink_to(root / "sys/bus/pci/drivers/intel_vpu")
    # An unrelated Intel device that must not count as an NPU.
    other = root / "sys/bus/pci/devices/0000:00:02.0"
    _write(other / "vendor", "0x8086\n")
    _write(other / "class", "0x030000\n")
    if accel:
        _write(root / "dev/accel/accel0", "")
    _write(root / "proc/modules", "intel_vpu 1 0 - Live 0x0\n" if module else "i915 1 0\n")
    lib = root / "usr/lib64"
    lib.mkdir(parents=True, exist_ok=True)
    if driver is not None:
        version = "1.35.0" if driver is DRIVER_1_35 else "1.32.0"
        _write(lib / f"libze_intel_npu.so.{version}", driver)
        (lib / "libze_intel_npu.so.1").symlink_to(f"libze_intel_npu.so.{version}")
        if compiler:
            if driver is DRIVER_1_35:
                _write(lib / npu_preflight.COMPILER_LOADER, b"loader")
                _write(lib / npu_preflight.COMPILER_LIB, b"2026.3.0-20965-abcdef01234")
            else:
                _write(lib / npu_preflight.LEGACY_COMPILER_LIB, b"2026.0.0-20000-abc")
    _write(root / "etc/os-release", f'ID={distro}\nPRETTY_NAME="{distro}"\n')
    return root


def notes_for(root, access=lambda _p, _m: True):
    notes = npu_preflight.npu_preflight(root, access) + npu_preflight.npu_userspace(root, NO_ENV)
    return {n.label: n for n in notes}


def test_healthy_tree(tmp_path):
    notes = notes_for(make_tree(tmp_path))
    assert all(n.ok for n in notes.values()), notes
    assert "1.35.0.20260722" in notes["level-zero NPU backend"].detail
    assert "2026.3.0" in notes["NPU compiler"].detail
    assert npu_preflight.npu_missing_help(tmp_path, NO_ENV, lambda _p, _m: True) == [
        "no cause found; reinstall the NPU driver: sudo dnf install -y intel-npu-driver"
    ]


def test_no_pci_device(tmp_path):
    root = make_tree(tmp_path, pci=False)
    notes = npu_preflight.npu_preflight(root)
    assert [(n.ok, n.label) for n in notes] == [(None, "pci device")]
    assert npu_preflight.npu_missing_help(root, NO_ENV) == []


def test_unreadable_sysfs_is_no_npu(tmp_path):
    assert npu_preflight.intel_npus(tmp_path / "nothing") == []


def test_no_accel_node_and_module_not_loaded(tmp_path):
    notes = notes_for(make_tree(tmp_path, accel=False, module=False))
    assert notes["kernel driver"].ok is False and "dmesg" in notes["kernel driver"].fix
    assert notes["intel_vpu module"].fix == "sudo modprobe intel_vpu"
    assert "device permissions" not in notes


def test_no_accel_node_with_module_loaded(tmp_path):
    notes = notes_for(make_tree(tmp_path, accel=False))
    assert notes["kernel driver"].ok is False
    assert "intel_vpu module" not in notes


def test_accel_not_read_write(tmp_path):
    root = make_tree(tmp_path)
    seen = []

    def access(path, mode):
        seen.append((path, mode))
        return False

    notes = notes_for(root, access)
    assert seen == [(root / "dev/accel/accel0", os.R_OK | os.W_OK)]
    perms = notes["device permissions"]
    assert perms.ok is False and "usermod -aG render" in perms.fix


def test_no_level_zero(tmp_path):
    notes = notes_for(make_tree(tmp_path, driver=None))
    l0 = notes["level-zero NPU backend"]
    assert l0.ok is False and l0.fix == "sudo dnf install -y intel-npu-driver"
    assert "NPU compiler" not in notes  # nothing to read the routes from


def test_no_level_zero_fix_follows_the_distro(tmp_path):
    notes = notes_for(make_tree(tmp_path, driver=None, distro="ubuntu"))
    assert npu_preflight.NPU_RELEASES_URL in notes["level-zero NPU backend"].fix


def test_compiler_missing(tmp_path):
    notes = notes_for(make_tree(tmp_path, compiler=False))
    comp = notes["NPU compiler"]
    assert comp.ok is False and npu_preflight.COMPILER_LOADER in comp.detail
    assert npu_preflight.NPU_RELEASES_URL in comp.fix


def test_fedora_1_32_rpm_without_its_compiler(tmp_path):
    # Fedora's intel-npu-driver 1.32 ships without libnpu_driver_compiler.so:
    # the NPU enumerates and every compile fails.
    root = make_tree(tmp_path, driver=DRIVER_1_32, compiler=False)
    comp = notes_for(root)["NPU compiler"]
    assert comp.ok is False and npu_preflight.LEGACY_COMPILER_LIB in comp.detail
    help_lines = npu_preflight.npu_missing_help(root, NO_ENV, lambda _p, _m: True)
    assert any(line.startswith("NPU compiler:") and "fix:" in line for line in help_lines)


def test_fedora_1_32_with_its_own_compiler(tmp_path):
    comp = notes_for(make_tree(tmp_path, driver=DRIVER_1_32))["NPU compiler"]
    assert comp.ok is True and npu_preflight.LEGACY_COMPILER_LIB in comp.detail


def test_unknown_compiler_route(tmp_path):
    comp = notes_for(make_tree(tmp_path, driver=b"npu-linux-driver-ci-9.9.9\0"))["NPU compiler"]
    assert comp.ok is None


def test_level_zero_symlink_points_at_the_older_build(tmp_path):
    root = make_tree(tmp_path, driver=DRIVER_1_32)
    _write(root / "usr/lib64/libze_intel_npu.so.1.35.0", DRIVER_1_35)
    note = notes_for(root)["level-zero version"]
    assert note.ok is False and note.fix.startswith("sudo ln -sf libze_intel_npu.so.1.35.0 ")


def test_ld_library_path_is_read_under_root(tmp_path):
    root = make_tree(tmp_path, compiler=False)
    _write(root / "opt/npu/lib" / npu_preflight.COMPILER_LOADER, b"x")
    _write(root / "opt/npu/lib" / npu_preflight.COMPILER_LIB, b"x")
    notes = npu_preflight.npu_userspace(root, {"LD_LIBRARY_PATH": "/opt/npu/lib"})
    assert notes[-1].ok is True


def test_distro_family(tmp_path):
    _write(tmp_path / "etc/os-release", 'ID=bazzite\nID_LIKE="fedora"\n')
    assert npu_preflight.distro_family(tmp_path) == "fedora"
    _write(tmp_path / "etc/os-release", "ID=kubuntu\nID_LIKE=ubuntu debian\n")
    assert npu_preflight.distro_family(tmp_path) == "debian"
    assert npu_preflight.distro_family(tmp_path / "none") == ""
    assert npu_preflight.install_command("wtype", "") == "install wtype with your package manager"


# ---- doctor rows -------------------------------------------------------------


def _npu_rows(monkeypatch, root, found):
    monkeypatch.setattr(doctor, "_ov_devices", lambda: found)
    return {r.label: r for r in doctor._npu(root, NO_ENV)}


def test_doctor_npu_not_enumerated_points_at_the_cause(monkeypatch, tmp_path):
    root = make_tree(tmp_path, compiler=False)
    rows = _npu_rows(monkeypatch, root, ("CPU",))
    assert rows["npu"].status == doctor.WARN
    assert rows["npu"].fix == npu_preflight.COMPILER_FIX
    assert rows["NPU compiler"].status == doctor.WARN  # falls back, so never FAIL


def test_doctor_npu_enumerated_still_checks_the_compiler(monkeypatch, tmp_path):
    rows = _npu_rows(monkeypatch, make_tree(tmp_path, compiler=False), ("CPU", "NPU"))
    assert rows["npu"].status == doctor.OK
    assert rows["NPU compiler"].status == doctor.WARN


def test_doctor_no_npu_is_one_info_row(monkeypatch, tmp_path):
    rows = _npu_rows(monkeypatch, make_tree(tmp_path, pci=False), ("CPU",))
    assert list(rows) == ["npu"] and rows["npu"].status == doctor.UNKNOWN


def test_doctor_npu_without_openvino(monkeypatch, tmp_path):
    rows = _npu_rows(monkeypatch, make_tree(tmp_path, accel=False), None)
    assert rows["npu"].status == doctor.UNKNOWN
    assert rows["kernel driver"].status == doctor.WARN


def test_doctor_consent_file(tmp_path):
    assert doctor._consent_file(tmp_path) == "absent"
    _write(tmp_path / "intel/openvino_telemetry", "0\n")
    assert doctor._consent_file(tmp_path) == "opted out"
    _write(tmp_path / "intel/openvino_telemetry", "1")
    assert doctor._consent_file(tmp_path) == "opted in"


@pytest.fixture
def doctor_home(tmp_path):
    """A child `vinopop --doctor` with an empty HOME and no X display, so it
    runs the same on any machine. Returns (home, run)."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env["HOME"] = str(home)
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        env[var] = str(home / var.lower())

    def run(*args):
        return subprocess.run(
            [sys.executable, str(REPO / "vinopop"), "--doctor", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    return home, run


def test_doctor_json_has_group_and_fix(doctor_home):
    home, run = doctor_home
    out = run("--json")
    checks = json.loads(out.stdout)["checks"]
    for check in checks:
        assert {"group", "name", "status", "detail", "fix", "label"} <= check.keys()
        assert check["group"] and check["name"] == check["label"]
        if check["status"] in (doctor.WARN, doctor.FAIL):
            assert check["fix"], check
    groups = {c["group"] for c in checks}
    assert {"system", "npu", "input"} <= groups
    assert any(c["name"] == "npu" for c in checks)
    # Read-only: nothing under HOME, so no ~/intel and no settings.json.
    # Intel's NPU driver makes its own cache on load, at $HOME/.cache
    # whatever XDG_CACHE_HOME says, so that one is allowed.
    npu_cache = home / ".cache" / "ze_intel_npu_cache"
    written = [
        p
        for p in home.rglob("*")
        if p not in (npu_cache.parent, npu_cache) and npu_cache not in p.parents
    ]
    assert written == []


def test_doctor_text_prints_fixes(doctor_home, monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "collect",
        lambda: [
            doctor.Result(doctor.OK, "fine", "x", group="g"),
            doctor.Result(doctor.WARN, "broken", "y", "do the thing", group="g"),
        ],
    )
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert "g: broken" in out and "fix:" in out and "do the thing" in out
    assert out.count("fix:") == 1


def test_every_warn_or_fail_row_in_this_module_has_a_fix(monkeypatch, tmp_path):
    # Each failing branch of the preflight, through the doctor.
    for kw in (
        {"accel": False, "module": False},
        {"driver": None},
        {"compiler": False},
        {"driver": DRIVER_1_32, "compiler": False},
    ):
        root = make_tree(tmp_path / str(len(list(tmp_path.iterdir()))), **kw)
        for row in _npu_rows(monkeypatch, root, ("CPU",)).values():
            if row.status in (doctor.WARN, doctor.FAIL):
                assert row.fix, (kw, row)
