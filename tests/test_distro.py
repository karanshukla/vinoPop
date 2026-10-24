from pathlib import Path

import pytest

from core import distro


def _root(tmp_path: Path, os_release: str, *, ostree=False, transactional=False) -> Path:
    (tmp_path / "etc").mkdir(parents=True)
    (tmp_path / "etc/os-release").write_text(os_release)
    (tmp_path / "usr/sbin").mkdir(parents=True)
    if ostree:
        (tmp_path / "run").mkdir()
        (tmp_path / "run/ostree-booted").touch()
    if transactional:
        (tmp_path / "usr/sbin/transactional-update").touch()
    return tmp_path


@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("ID=fedora\n", "fedora"),
        ('ID=bazzite\nID_LIKE="fedora"\n', "fedora"),
        ('ID=pop\nID_LIKE="ubuntu debian"\n', "debian"),
        ("ID=cachyos\nID_LIKE=arch\n", "arch"),
        ('ID="opensuse-aeon"\nID_LIKE="suse opensuse opensuse-tumbleweed"\n', "suse"),
        ('ID="opensuse-tumbleweed"\n', "suse"),
        ("ID=nixos\n", ""),
    ],
)
def test_family(tmp_path, text, family):
    assert distro.family(_root(tmp_path, text)) == family


def test_family_without_os_release(tmp_path):
    assert distro.family(tmp_path) == ""


def test_mutable_fedora_installs_in_place(tmp_path):
    root = _root(tmp_path, "ID=fedora\n")
    assert distro.immutable(root) == ""
    assert distro.install_command(["grim", "slurp"], root) == "sudo dnf install -y grim slurp"
    assert distro.install_argv(["maim"], root) == ["pkexec", "dnf", "install", "-y", "maim"]


def test_pkexec_never_waits_for_a_prompt(tmp_path):
    arch = _root(tmp_path / "a", "ID=arch\n")
    suse = _root(tmp_path / "s", 'ID="opensuse-tumbleweed"\n')
    assert distro.install_argv(["maim"], arch) == [
        "pkexec",
        "pacman",
        "-S",
        "--needed",
        "--noconfirm",
        "maim",
    ]
    assert distro.install_argv(["maim"], suse) == [
        "pkexec",
        "zypper",
        "--non-interactive",
        "install",
        "maim",
    ]
    assert distro.install_command(["maim"], suse) == "sudo zypper install maim"


def test_fedora_atomic_layers_with_rpm_ostree(tmp_path):
    root = _root(tmp_path, "ID=fedora\nVARIANT_ID=kinoite\n", ostree=True)
    assert distro.immutable(root) == distro.OSTREE
    assert distro.install_argv(["grim"], root) is None
    assert distro.install_command(["grim"], root) == "rpm-ostree install grim, then reboot"


def test_aeon_uses_transactional_update(tmp_path, monkeypatch):
    root = _root(tmp_path, 'ID="opensuse-aeon"\nID_LIKE="suse"\n', transactional=True)
    monkeypatch.setattr(distro, "_usr_read_only", lambda _root: True)
    assert distro.immutable(root) == distro.TRANSACTIONAL
    assert distro.install_argv(["grim"], root) is None
    assert "transactional-update pkg install grim" in distro.install_command(["grim"], root)


def test_unknown_read_only_usr(tmp_path, monkeypatch):
    root = _root(tmp_path, "ID=nixos\n")
    monkeypatch.setattr(distro, "_usr_read_only", lambda _root: True)
    assert distro.immutable(root) == distro.READ_ONLY
    assert distro.install_argv(["grim"], root) is None


def test_family_override(tmp_path):
    root = _root(tmp_path, "ID=fedora\n")
    assert distro.install_command(["wtype"], root, fam="") == (
        "install wtype with your package manager"
    )
    assert distro.install_command(["wtype"], root, fam="arch") == "sudo pacman -S --needed wtype"


@pytest.mark.parametrize(
    ("wayland", "desktop", "packages"),
    [
        ("", "XFCE", ["maim"]),
        ("wayland-0", "KDE", ["spectacle"]),
        ("wayland-0", "ubuntu:GNOME", ["gnome-screenshot"]),
        ("wayland-1", "sway", ["grim", "slurp"]),
        ("wayland-1", "Hyprland", ["grim", "slurp"]),
    ],
)
def test_ocr_capture_tool_fits_the_desktop(monkeypatch, wayland, desktop, packages):
    from capture import screen_ocr

    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert screen_ocr._capture_packages() == packages
