#!/usr/bin/env bash
# Install or upgrade vinoPop from a GitHub release, for this user:
#
#   curl -fsSL https://github.com/karanshukla/vinoPop/releases/latest/download/get.sh | bash
#
# Options go after `bash -s --`, e.g. `| bash -s -- --with-ai`:
#
#   vN.N.N      that release instead of the newest
#   --with-ai   also the offline AI (openvino, rapidocr, vinospeak) in a venv
#               that sees the system's GTK (--system-site-packages), several
#               hundred MB. An upgrade keeps and updates the venv if it's there.
#   --uninstall the release's own install.sh --uninstall, then the files here
#
# It downloads the release's SHA256SUMS and source tarball, refuses the tarball
# unless it's listed there and matches, and unpacks it into
# ~/.local/share/vinopop/<version>, behind the ~/.local/share/vinopop/app link
# the launchers use, so an upgrade only moves the link. Then it runs that
# release's install.sh, which checks the system packages and writes the
# launchers. No sudo; it says which system packages to install if any are
# missing. It keeps a checked copy of itself as vinopop-upgrade, which
# Settings' Check for updates runs with VINO_UPDATE_TAG set.
#
# From a git clone, bash install.sh instead.

# Everything is in main, called on the last line: piped into bash, a download
# cut short must not run half a script.
set -euo pipefail

REPO="karanshukla/vinoPop"
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/vinopop"
APP="$ROOT/app"
VENV="$ROOT/venv"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"

say() { printf '== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# fetch NAME: download a release asset into $WORK and check it against
# SHA256SUMS, where it must be listed exactly once.
fetch() {
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
        -o "$WORK/$1" "$BASE/$1" || die "download failed: $BASE/$1"
    (cd "$WORK" \
        && awk -v f="$1" '$2 == f || $2 == "*" f { print $1 "  " f }' SHA256SUMS > "want-$1" \
        && [ "$(wc -l < "want-$1")" -eq 1 ] \
        && sha256sum --check --strict --quiet "want-$1") \
        || die "$1 doesn't match the release's SHA256SUMS. Nothing was changed."
}

main() {
    local tag="${VINO_UPDATE_TAG:-}" ai=0 uninstall=0 arg
    local passthrough=()
    for arg in "$@"; do
        case "$arg" in
            v[0-9]*) tag="$arg" ;;
            --with-ai) ai=1 ;;
            --uninstall) uninstall=1 ;;
            --purge|--yes) passthrough+=("$arg") ;;
            *) die "unknown option: $arg" ;;
        esac
    done
    if [ -n "$tag" ] && ! [[ "$tag" =~ ^v[0-9]+(\.[0-9]+)*$ ]]; then
        die "not a release tag: '$tag'"
    fi
    [ "$(id -u)" -ne 0 ] || die "run this as yourself, not root: vinoPop installs into your home"

    if [ "$uninstall" = 1 ]; then
        [ -x "$APP/install.sh" ] || [ -f "$APP/install.sh" ] || die "no release install in $APP"
        bash "$APP/install.sh" --uninstall "${passthrough[@]}"
        rm -rf "$ROOT"/v[0-9]* "$APP" "$VENV"
        rm -f "$BIN/vinopop-upgrade"
        echo "vinoPop's release files removed"
        return
    fi

    local tool
    for tool in curl tar sha256sum awk python3; do
        command -v "$tool" >/dev/null || die "missing $tool: install it, then run this again"
    done
    # An upgrade keeps the offline AI where it's installed.
    [ -x "$VENV/bin/python" ] && ai=1

    # Where the assets are: a mirror, or CI's file:// fake release.
    if [ -n "${VINOPOP_RELEASE_BASE:-}" ]; then
        BASE="$VINOPOP_RELEASE_BASE"
    elif [ -n "$tag" ]; then
        BASE="https://github.com/$REPO/releases/download/$tag"
    else
        BASE="https://github.com/$REPO/releases/latest/download"
    fi
    WORK="$(mktemp -d)"
    # shellcheck disable=SC2064 # expand now
    trap "rm -rf '$WORK'" EXIT

    say "Downloading ${tag:-the latest release} from $BASE"
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
        -o "$WORK/SHA256SUMS" "$BASE/SHA256SUMS" || die "download failed: $BASE/SHA256SUMS"
    local tarball
    tarball="$(awk '{ sub(/^\*/, "", $2); print $2 }' "$WORK/SHA256SUMS" \
        | grep -E '^vinopop-v[0-9][0-9.]*\.tar\.gz$' | head -1 || true)"
    [ -n "$tarball" ] || die "the release's SHA256SUMS lists no vinopop tarball"
    fetch "$tarball"
    fetch get.sh
    echo "checksums verified"

    local version="${tarball#vinopop-}"
    version="${version%.tar.gz}"
    local dest="$ROOT/$version"
    mkdir -p "$ROOT"
    rm -rf "$dest.new"
    mkdir "$dest.new"
    tar -xzf "$WORK/$tarball" -C "$dest.new" --strip-components=1 --no-same-owner
    [ "$(cat "$dest.new/VERSION" 2>/dev/null)" = "$version" ] || die "$tarball isn't the $version release"
    rm -rf "$dest"
    mv "$dest.new" "$dest"

    if [ "$ai" = 1 ]; then
        say "Offline AI packages (in $VENV)"
        # The system Python: GTK's bindings come from the distro, not pip.
        [ -x "$VENV/bin/python" ] || /usr/bin/python3 -m venv --system-site-packages "$VENV"
        "$VENV/bin/python" -m pip install --quiet --upgrade -r "$dest/requirements-ai.txt"
    fi

    # Before install.sh, so a failed dependency check still leaves the upgrade
    # to rerun once the packages are in.
    install -Dm755 "$WORK/get.sh" "$BIN/vinopop-upgrade"

    # The launchers run "$APP/vinopop", so the link is all an upgrade moves.
    local previous
    previous="$(readlink "$APP" 2>/dev/null || true)"
    ln -sfn "$version" "$APP.new"
    mv -Tf "$APP.new" "$APP"
    say "Writing the launchers"
    if [ -x "$VENV/bin/python" ]; then
        VINOPOP_PYTHON="$VENV/bin/python" bash "$APP/install.sh" </dev/null
    else
        bash "$APP/install.sh" </dev/null
    fi

    # A running vinoPop still has the previous release's files open until it
    # restarts, so that one stays; anything older goes.
    local old
    for old in "$ROOT"/v[0-9]*; do
        [ "$old" = "$dest" ] || [ "${old##*/}" = "$previous" ] || rm -rf "$old"
    done
    say "Installed vinoPop $version"
}

main "$@"
