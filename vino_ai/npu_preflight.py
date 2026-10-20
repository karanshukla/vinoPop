"""Why OpenVINO doesn't see the NPU, or sees it and can't compile for it.

Ported from vinoWhisper (vinowhisper/devices.py: npu_preflight,
COMPILER_ROUTES, _compiler_notes, npu_missing_help, plus the sysfs and
library lookups they need). Keep the two in sync: a driver quirk found by
one is a driver quirk for the other.

Pure reads, no OpenVINO. Every path is looked up under `root` ("/" for
real), so the tests run each branch against a fake tree in a temp dir.
"""

from __future__ import annotations

import mmap
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path("/")
NPU_RELEASES_URL = "https://github.com/intel/linux-npu-driver/releases"

INTEL = "0x8086"
NPU_DRIVER = "intel_vpu"

LEVEL_ZERO_STEM = "libze_intel_npu.so"
COMPILER_LIB = "libopenvino_intel_npu_compiler.so"
COMPILER_LOADER = "libopenvino_intel_npu_compiler_loader.so"
LEGACY_COMPILER_LIB = "libnpu_driver_compiler.so"

# (name the driver binary carries, libraries that route needs). 1.35.0 stores
# the loader's name split, so only its prefix is searchable.
COMPILER_ROUTES = (
    (b"libopenvino_intel_npu_compiler_l", (COMPILER_LOADER, COMPILER_LIB)),
    (LEGACY_COMPILER_LIB.encode(), (LEGACY_COMPILER_LIB,)),
)

LIBRARY_DIRS = (
    "/usr/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib",
    "/usr/local/lib64",
    "/usr/local/lib",
)

_DRIVER_MARK = re.compile(rb"npu-linux-driver-ci-([\d][\d.]*\d)")
_OPENVINO_MARK = re.compile(rb"20\d\d\.\d+\.\d+(?:-\w+){0,2}")

COMPILER_FIX = (
    "no distro packages the NPU compiler: take intel-driver-compiler-npu from the "
    f"linux-npu-driver release matching your driver ({NPU_RELEASES_URL}), then "
    "`dpkg-deb -x intel-driver-compiler-npu_*.deb x && sudo install -m 0755 "
    "$(find x -name '*compiler*.so') /usr/lib64/ && sudo ldconfig`"
)


@dataclass(frozen=True)
class Note:
    ok: bool | None  # None: can't tell, or nothing to fix
    label: str
    detail: str
    fix: str = ""


def _under(root: Path, path: str) -> Path:
    return root / path.lstrip("/")


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


# ---- distro ------------------------------------------------------------------

_FAMILIES = {
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "nobara": "fedora",
    "bazzite": "fedora",
    "debian": "debian",
    "ubuntu": "debian",
    "pop": "debian",
    "linuxmint": "debian",
    "arch": "arch",
    "cachyos": "arch",
    "endeavouros": "arch",
    "manjaro": "arch",
    "opensuse": "suse",
    "opensuse-tumbleweed": "suse",
    "opensuse-leap": "suse",
}
_INSTALL = {
    "fedora": "sudo dnf install -y",
    "debian": "sudo apt install -y",
    "arch": "sudo pacman -S --needed",
    "suse": "sudo zypper install -y",
}


def distro_family(root: Path = ROOT) -> str:
    """fedora/debian/arch/suse from os-release, or "" if unrecognised."""
    values: dict[str, str] = {}
    try:
        text = _under(root, "/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            values[key] = value.strip().strip("\"'").lower()
    for candidate in [values.get("ID", "")] + values.get("ID_LIKE", "").split():
        if candidate in _FAMILIES:
            return _FAMILIES[candidate]
    return ""


def install_command(package: str, family: str) -> str:
    prefix = _INSTALL.get(family)
    return f"{prefix} {package}" if prefix else f"install {package} with your package manager"


def driver_fix(family: str) -> str:
    if family == "fedora":
        return "sudo dnf install -y intel-npu-driver"
    if family == "arch":
        return "paru -S intel-npu-driver (AUR; needs level-zero-loader)"
    return (
        "install intel-level-zero-npu, intel-driver-compiler-npu and intel-fw-npu from "
        f"{NPU_RELEASES_URL} (`sudo dpkg -i ./*.deb` on Debian/Ubuntu)"
    )


# ---- kernel side -------------------------------------------------------------


def intel_npus(root: Path = ROOT) -> list[str]:
    """PCI slots of Intel NPUs (class 0x12xx, or bound to intel_vpu)."""
    try:
        slots = sorted(_under(root, "/sys/bus/pci/devices").iterdir())
    except OSError:
        return []
    found = []
    for slot in slots:
        if _read_attr(slot / "vendor") != INTEL:
            continue
        try:
            driver = (slot / "driver").resolve(strict=True).name
        except OSError:
            driver = ""
        if _read_attr(slot / "class").startswith("0x12") or driver == NPU_DRIVER:
            device = _read_attr(slot / "device").removeprefix("0x")
            bound = f"driver {driver}" if driver else "no driver bound"
            found.append(f"8086:{device} at {slot.name}, {bound}")
    return found


def accel_nodes(root: Path = ROOT) -> list[Path]:
    try:
        return sorted(_under(root, "/dev/accel").glob("accel*"))
    except OSError:
        return []


def module_loaded(name: str, root: Path = ROOT) -> bool | None:
    try:
        modules = _under(root, "/proc/modules").read_text(encoding="utf-8")
    except OSError:
        return None
    return any(line.startswith(f"{name} ") for line in modules.splitlines())


def npu_preflight(root: Path = ROOT, access: Callable[[Path, int], bool] = os.access) -> list[Note]:
    """PCI device, /dev/accel node, its permissions, the intel_vpu module."""
    npus = intel_npus(root)
    if not npus:
        return [Note(None, "pci device", "no Intel NPU on the PCI bus")]
    notes = [Note(True, "pci device", npus[0])]

    nodes = accel_nodes(root)
    if not nodes:
        notes.append(
            Note(
                False,
                "kernel driver",
                "no /dev/accel/accel* node: the intel_vpu driver did not bind. Either "
                "the kernel predates this NPU or probing failed",
                "`sudo dmesg | grep -i vpu` says which; a newer kernel fixes the first",
            )
        )
        if module_loaded(NPU_DRIVER, root) is False:
            notes.append(
                Note(False, "intel_vpu module", "not loaded", f"sudo modprobe {NPU_DRIVER}")
            )
        return notes

    node = nodes[0]
    notes.append(Note(True, "kernel driver", f"{node} present"))
    if access(node, os.R_OK | os.W_OK):
        notes.append(Note(True, "device permissions", f"{node} is readable and writable"))
    else:
        notes.append(
            Note(
                False,
                "device permissions",
                f"cannot open {node} for reading and writing",
                "sudo usermod -aG render $USER, then log out and back in",
            )
        )
    return notes


# ---- userspace ---------------------------------------------------------------


def library_dirs(root: Path = ROOT, env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    seen: dict[Path, None] = {}
    raw = [entry for entry in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if entry]
    for text in raw + list(LIBRARY_DIRS):
        path = _under(root, text)
        try:
            if path.is_dir():
                seen.setdefault(path, None)
        except OSError:
            continue
    return list(seen)


def find_library(name: str, root: Path = ROOT, env: Mapping[str, str] | None = None) -> Path | None:
    for directory in library_dirs(root, env):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in text.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


def _marker(path: Path, pattern: re.Pattern[bytes]) -> str | None:
    try:
        with (
            path.open("rb") as handle,
            mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
        ):
            match = pattern.search(data)
            if match is None:
                return None
            group = match.group(1) if match.groups() else match.group()
            return group.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def compiler_routes(driver: Path) -> list[tuple[str, ...]]:
    """The compiler libraries the driver binary names, one tuple per route."""
    try:
        with (
            driver.open("rb") as handle,
            mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
        ):
            return [libs for needle, libs in COMPILER_ROUTES if data.find(needle) != -1]
    except (OSError, ValueError):
        return []


def _level_zero_notes(root: Path, env: Mapping[str, str] | None, family: str) -> list[Note]:
    soname = find_library(f"{LEVEL_ZERO_STEM}.1", root, env)
    if soname is None:
        return [
            Note(
                False,
                "level-zero NPU backend",
                f"no {LEVEL_ZERO_STEM}.1 on the library path, so OpenVINO's NPU plugin "
                "has nothing to load and the device will not enumerate",
                driver_fix(family),
            )
        ]

    target = soname.resolve()
    version = _marker(target, _DRIVER_MARK) or "unknown version"
    notes = [Note(True, "level-zero NPU backend", f"{target} ({version})")]

    installed = sorted(
        (path for path in target.parent.glob(f"{LEVEL_ZERO_STEM}.*") if not path.is_symlink()),
        key=lambda path: _version_tuple(path.name.split(".so.", 1)[-1]),
    )
    if installed and installed[-1] != target:
        newer = installed[-1]
        notes.append(
            Note(
                False,
                "level-zero version",
                f"{soname.name} points at {target.name} while {newer.name} is also "
                "installed. A package reinstall rewrites this symlink to the distro's "
                "version, which enumerates the NPU and then fails to compile for it",
                f"sudo ln -sf {newer.name} {soname}",
            )
        )
    return notes


def _compiler_notes(root: Path = ROOT, env: Mapping[str, str] | None = None) -> list[Note]:
    soname = find_library(f"{LEVEL_ZERO_STEM}.1", root, env)
    if soname is None:
        return []
    driver = soname.resolve()
    routes = compiler_routes(driver)
    if not routes:
        return [
            Note(
                None,
                "NPU compiler",
                f"{driver.name} names no compiler library vinoPop recognises, so whether "
                "it can compile is unknown",
            )
        ]

    for libs in routes:
        present = [path for lib in libs if (path := find_library(lib, root, env)) is not None]
        if len(present) == len(libs):
            version = next(
                (v for path in present if (v := _marker(path, _OPENVINO_MARK))),
                "unknown version",
            )
            return [Note(True, "NPU compiler", f"{present[-1]} (OpenVINO {version})")]

    wanted = ", or ".join(" + ".join(libs) for libs in routes)
    return [
        Note(
            False,
            "NPU compiler",
            f"{driver.name} loads {wanted}, which is not on the library path. The NPU "
            "still enumerates, and every compile fails with "
            "ZE_RESULT_ERROR_UNSUPPORTED_FEATURE (Fedora's intel-npu-driver 1.32 ships "
            "without it)",
            COMPILER_FIX,
        )
    ]


def npu_userspace(root: Path = ROOT, env: Mapping[str, str] | None = None) -> list[Note]:
    family = distro_family(root)
    return _level_zero_notes(root, env, family) + _compiler_notes(root, env)


def npu_missing_help(
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    access: Callable[[Path, int], bool] = os.access,
) -> list[str]:
    """What to fix when OpenVINO doesn't list the NPU. Empty when there's no
    Intel NPU on the PCI bus to begin with."""
    if not intel_npus(root):
        return []
    lines: list[str] = []
    for note in npu_preflight(root, access) + npu_userspace(root, env):
        if note.ok is not True:
            lines.append(
                f"{note.label}: {note.detail}" + (f"; fix: {note.fix}" if note.fix else "")
            )
    if not lines:
        lines.append(f"no cause found; reinstall the NPU driver: {driver_fix(distro_family(root))}")
    return lines
