"""Classifies selected text so the popup can show context-appropriate actions."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

_OBJECT_REPLACEMENT = "\ufffc"
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u180e"
_SOFT_HYPHEN = "\u00ad"
_INVISIBLE_CHARS = _OBJECT_REPLACEMENT + _ZERO_WIDTH + _SOFT_HYPHEN


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


class ContentType(Enum):
    COMMAND = "command"
    URL = "url"
    EMAIL = "email"
    PATH = "path"
    PLAIN_TEXT = "plain_text"


_URL_RE = re.compile(r"^(https?|ftp|file)://\S+$", re.IGNORECASE)
_NAKED_URL_RE = re.compile(
    r"^(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?$",
    re.IGNORECASE,
)
_LIKELY_TLDS = _words("""
    com no org net io dev app ai co edu gov uk de se dk fi fr it es nl be ch at
    pl cz ru jp cn in au ca br mx info biz me tv us eu xyz site online store shop blog
""")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
_RFC_5321_MAX_ADDRESS_LEN = 320
_PATH_RE = re.compile(r"^(/|~/|\./)[^\s]*$")

_PACKAGE_MANAGERS = _words("""
    sudo doas apt apt-get aptitude pacman paru yay dnf yum zypper snap flatpak brew nix
    nix-env nix-build nix-shell nix-store apk xbps-install xbps-query eopkg emerge
""")
_VCS_AND_BUILD_TOOLS = _words("""
    git svn hg lazygit tig gh glab docker podman nerdctl lima kubectl helm kustomize
    terraform ansible ansible-playbook vagrant packer make cmake ninja meson gradle mvn
    just bazel
""")
_FILE_TOOLS = _words("""
    ls ll la eza exa cd pwd cp mv rm mkdir rmdir chmod chown chgrp ln touch stat file
    tree mc ranger lf nnn vifm
""")
_TEXT_TOOLS = _words("""
    cat bat tac tail head less more view grep egrep fgrep rg ag ack sed awk cut paste tr
    sort uniq wc tee column fold fmt nl find fd locate xargs tar gzip gunzip zip unzip
    7z unrar diff patch cmp jq yq
""")
_NETWORK_TOOLS = _words("""
    ssh mosh scp sftp rsync curl wget httpie http ping traceroute tracepath mtr dig
    nslookup host whois netstat ss ip ifconfig iwconfig iwctl nmap ncat nc socat nmcli
    networkctl wpa_cli ufw iptables nftables firewall-cmd
""")
_SYSTEM_TOOLS = _words("""
    ps kill killall pkill pgrep top htop btop atop systemctl journalctl service dmesg
    uptime df du duf ncdu free lsof fuser mount umount uname hostname whoami id who w
    groups crontab at lsblk blkid fdisk parted dd mkfs swapon swapoff
""")
_EDITORS = _words("""
    vim vi nano emacs code codium gedit kate nvim helix hx micro subl
""")
_LANGUAGE_TOOLS = _words("""
    python python3 pip pip3 pipx uv poetry pdm rye pytest black ruff mypy flake8 isort
    tox twine node nodejs npm yarn pnpm npx deno bun tsx tsc eslint prettier jest vitest
    mocha vite webpack cargo rustc rustup rustfmt clippy go java javac kotlin scala sbt
    ruby gem bundle rake rails irb perl cpan php composer dotnet mono swift lua luarocks
    julia crystal shards mix iex
""")
_DATABASE_CLIENTS = _words("""
    psql mysql sqlite3 redis-cli mongo mongosh duckdb
""")
_CLOUD_CLIS = _words("""
    aws gcloud az doctl linode-cli fly flyctl k9s stern
""")
_SHELL_BUILTINS = _words("""
    echo printf export source alias unalias bash sh zsh fish dash nu elvish env which
    type whereis command date cal sleep watch time timeout man info help tldr history
    exit logout clear reset nohup screen tmux tmate
""")
_MEDIA_TOOLS = _words("""
    ffmpeg ffprobe magick convert sox pandoc yt-dlp youtube-dl
""")
_BACKUP_TOOLS = _words("""
    restic borg rclone rsnapshot duplicity
""")
_X11_TOOLS = _words("""
    xdotool xclip xsel wmctrl xrandr xprop xev notify-send xdg-open
""")
_VERSION_MANAGERS = _words("""
    direnv asdf mise nvm pyenv rbenv fnm volta
""")
_KDE_TOOLS = _words("""
    kquitapp6 kquitapp5 kquitapp kstart6 kstart5 kstart kwriteconfig6 kwriteconfig5
    kreadconfig6 kreadconfig5 qdbus qdbus6 qdbus-qt6 kcmshell6 kcmshell5 systemsettings
    kded6 kbuildsycoca6 kioclient6 kscreen-doctor krunner plasmashell kwin_wayland
    kwin_x11 kwin kactivities-cli balooctl6 balooctl kdialog kdeconnect-cli kompare
    loginctl busctl gdbus gtk-launch
""")
_AUDIO_TOOLS = _words("""
    wpctl pactl pacmd pw-cli pw-play pw-record pw-dump pw-metadata amixer alsamixer
    speaker-test aplay arecord
""")
_DESKTOP_TOOLS = _words("""
    gsettings dconf xdg-settings xdg-mime wl-copy wl-paste ydotool wtype grim slurp
    spectacle okular dolphin
""")
_HYPHENATED_TOOLS = _words("""
    gnome-terminal gnome-extensions gtk-update-icon-cache systemd-run systemd-analyze
    dpkg-reconfigure dpkg-query update-alternatives update-initramfs update-grub
    grub2-mkconfig flatpak-spawn docker-compose ssh-keygen ssh-copy-id ssh-add
    rpm-ostree
""")
_COMMAND_TOKENS = (
    _PACKAGE_MANAGERS
    | _VCS_AND_BUILD_TOOLS
    | _FILE_TOOLS
    | _TEXT_TOOLS
    | _NETWORK_TOOLS
    | _SYSTEM_TOOLS
    | _EDITORS
    | _LANGUAGE_TOOLS
    | _DATABASE_CLIENTS
    | _CLOUD_CLIS
    | _SHELL_BUILTINS
    | _MEDIA_TOOLS
    | _BACKUP_TOOLS
    | _X11_TOOLS
    | _VERSION_MANAGERS
    | _KDE_TOOLS
    | _AUDIO_TOOLS
    | _DESKTOP_TOOLS
    | _HYPHENATED_TOOLS
)

_AMBIGUOUS_COMMANDS = _words("""
    apt apk snap brew nix yay emerge make just ninja meson packer vagrant helm lima la
    file tree touch stat ranger cat bat less more view head tail cut paste sort column
    fold tee find locate tar zip patch diff ack mosh curl http ping dig host ip ps kill
    top atop service uptime du free mount hostname who w groups at id parted vim code
    kate micro nano helix python black poetry rye twine node prettier jest mocha cargo
    go java swift ruby gem bundle rake rails perl php composer mono lua julia crystal
    shards mix yarn bun uv az fly stern echo export source alias bash sh fish dash nu
    elvish env which type command date cal sleep watch time timeout man info help tldr
    history exit logout clear reset screen convert sox borg duplicity mise volta grim
    slurp spectacle dolphin
""")
_UNAMBIGUOUS_COMMANDS = frozenset(_COMMAND_TOKENS - _AMBIGUOUS_COMMANDS)


_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "apt": _words(
        "install update upgrade remove purge autoremove search show list full-upgrade policy"
    ),
    "snap": _words("install remove refresh list info find run connect connections"),
    "brew": _words("install uninstall upgrade update list search info services tap doctor cleanup"),
    "nix": _words("run build develop shell flake profile search eval repl store"),
    "make": _words("install uninstall clean distclean all check test build lint docs release"),
    "go": _words("build mod install vet fmt generate work tool version"),
    "cargo": _words(
        "build run test check clippy fmt install add new init update publish bench doc tree clean"
    ),
    "yarn": _words("add install build dev start test run remove upgrade workspace"),
    "poetry": _words("install add run shell build lock update new init publish show env"),
    "uv": _words("pip run sync add venv tool lock init python"),
    "bun": _words("install add run build test x"),
    "gem": _words("install update uninstall list"),
    "bundle": _words("install exec update"),
    "rails": _words("server console generate new routes"),
    "composer": _words("install require update dump-autoload"),
    "helm": _words("install upgrade repo list uninstall template"),
    "vagrant": _words("up ssh halt destroy provision reload status"),
}
_HOST_ARG_COMMANDS = frozenset({"curl", "http", "ping", "dig", "host", "mosh"})
_COUNT_ARG_COMMANDS = frozenset({"kill", "sleep"})

_LOGICAL_OPERATOR = r"&&|\|\|"
_SUBSTITUTION = r"\$\(|\$\{"
_HEREDOC_START = r"<<-?\s*['\"]?[A-Za-z_]\w*"
_COMMAND_HINTS = re.compile("|".join((_LOGICAL_OPERATOR, _SUBSTITUTION, _HEREDOC_START)))
_COMMAND_HINTS_MAX_LEN = 4000

_SHELL_SEP_RE = re.compile(r"(&&|\|\||;|\|)")

_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][\w-]*(?:=\S*)?$")
_ABSOLUTE_PATH_ARG = r"/[\w.@+-]{2,}|/[\w.@+-]+/"
_HOME_PATH_ARG = r"~(?:/|$)|~\w"
_DOT_PATH_ARG = r"\.{1,2}(?:/|$)"
_DOTFILE_ARG = r"\.[a-z_][\w.-]*"
_VARIABLE_ARG = r"\$[A-Za-z_{(]"
_RELATIVE_DIR_ARG = r"[\w.@+-]+/(?:[\w.@+/-]*)?$"
_PATH_ARG_RE = re.compile(
    r"^(?:"
    + "|".join(
        (
            _ABSOLUTE_PATH_ARG,
            _HOME_PATH_ARG,
            _DOT_PATH_ARG,
            _DOTFILE_ARG,
            _VARIABLE_ARG,
            _RELATIVE_DIR_ARG,
        )
    )
    + r")"
)
_FILE_EXT_RE = re.compile(
    r"\w\.(?:sh|bash|zsh|fish|py|pl|rb|js|mjs|ts|go|rs|c|h|cpp|hpp|java|jar|lua|php"
    r"|txt|md|json|ya?ml|toml|ini|conf|cfg|log|csv|xml|html?|css|sql|db|lock"
    r"|tar|gz|tgz|xz|zst|bz2|zip|deb|rpm|iso|img|appimage|flatpakref|service|desktop"
    r"|png|jpe?g|svg|webp|mp3|mp4|mkv|wav|pdf|patch|diff)$",
    re.IGNORECASE,
)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][\w-]*=")
_REDIRECT_RE = re.compile(r"^(?:\d?>>?|&>|<)(.*)$")
_HOST_RE = re.compile(r"^(?:\w+://\S+|[\w-]+(?:\.[\w-]+)+(?::\d+)?)$")
_COUNT_RE = re.compile(r"^(?:%?\d+[smhd]?)$")
_UNKNOWN_TOOL_RE = re.compile(r"^[a-z][a-z0-9_.+]*$")
_HYPHEN_NAME_RE = re.compile(r"^[a-z][a-z0-9]*-[a-z][\w-]*$")
_EXEC_PATH_RE = re.compile(r"^(?:\.{1,2}/|~/)\S|^/\S*/s?bin/\S")

_SHELL_NEUTRAL_HEADS = frozenset({"fi", "done", "esac", "then", "else", "do", "}", "{", ")", ";;"})
_SHELL_OPENERS = frozenset({"if", "elif", "for", "while", "until", "case", "select"})
_OPENER_TAIL_RE = re.compile(r";\s*(?:then|do)\s*$|\[\[?\s")
_FUNC_DEF_RE = re.compile(r"^(?:function\s+)?[A-Za-z_][\w-]*\s*\(\)\s*\{?\s*$|^function\s+\w")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?")


def _is_redirect(tok: str, nxt: str | None) -> bool:
    """'> out.txt', '>>~/.bashrc', '2>&1', '< /dev/null'. A bare '>' needs
    a file-looking target so "Settings > Privacy" doesn't count."""
    m = _REDIRECT_RE.match(tok)
    if not m:
        return False
    target = m.group(1) or (nxt or "")
    if target.startswith("&") and target[1:].isdigit():
        return True
    return bool(_PATH_ARG_RE.match(target) or _FILE_EXT_RE.search(target))


def _is_glob(tok: str) -> bool:
    """'*.txt', 'src/*' or '*', but not markdown's '*emphasis*' (pinned by
    `tests/test_classifier.py::test_markdown_emphasis_is_not_a_glob`)."""
    return "*" in tok and (tok == "*" or "." in tok or "/" in tok)


def _token_is_evidence(tok: str, nxt: str | None) -> bool:
    if _FLAG_RE.match(tok) or _PATH_ARG_RE.match(tok) or _FILE_EXT_RE.search(tok):
        return True
    if _ASSIGN_RE.match(tok) or _is_redirect(tok, nxt):
        return True
    return _is_glob(tok)


def _has_evidence(head: str, args: list[str]) -> bool:
    """Does the rest of a segment back up an ambiguous command word? A known
    subcommand, a host for ping-like commands, a number for kill and sleep,
    or a flag, path, file, assignment, redirect or glob anywhere (pinned by
    `tests/test_classifier.py::test_go_run_and_go_get_are_not_subcommands`,
    `tests/test_classifier.py::test_ping_counts_with_a_host`,
    `tests/test_classifier.py::test_ping_without_a_host_is_prose`,
    `tests/test_classifier.py::test_kill_counts_with_a_pid`,
    `tests/test_classifier.py::test_kill_without_a_number_is_prose`)."""
    if args:
        first = args[0]
        if first in _SUBCOMMANDS.get(head, ()) or first in _UNAMBIGUOUS_COMMANDS:
            return True
        if head in _COUNT_ARG_COMMANDS and _COUNT_RE.match(first):
            return True
    if head in _HOST_ARG_COMMANDS and any(_HOST_RE.match(a) for a in args):
        return True
    return any(
        _token_is_evidence(tok, args[i + 1] if i + 1 < len(args) else None)
        for i, tok in enumerate(args)
    )


def _without_env_assignments(tokens: list[str]) -> list[str]:
    """'FOO=1 make' -> ['make']."""
    while tokens and _ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]
    return tokens


def _segment_is_command(segment: str, *, prompted: bool, piped_into_command: bool) -> bool:
    """One ;/&&/||/| separated piece of a line. 'prompted' means the line
    had a '$ ' or root '# ' prompt, which is evidence on its own. A word that
    is everyday English needs evidence (pinned by
    `tests/test_classifier.py::test_prose_is_not_a_command`); an unknown
    tool needs a flag, as a path after a word is as often prose (pinned by
    `tests/test_classifier.py::test_an_unknown_tool_counts_with_a_flag_but_not_with_a_path`);
    a listed hyphenated tool needs nothing (pinned by
    `tests/test_classifier.py::test_a_listed_hyphenated_tool_counts_with_a_bare_argument`,
    `tests/test_classifier.py::test_an_unlisted_hyphenated_word_needs_evidence`)."""
    tokens = _without_env_assignments(segment.split())
    if not tokens:
        return False
    head = tokens[0].lstrip("({!")
    args = tokens[1:]
    if head in _UNAMBIGUOUS_COMMANDS or _EXEC_PATH_RE.match(head) or _FLAG_RE.match(head):
        return True
    if head in _AMBIGUOUS_COMMANDS or _HYPHEN_NAME_RE.match(head):
        return prompted or piped_into_command or _has_evidence(head, args)
    if _UNKNOWN_TOOL_RE.match(head):
        return bool(args) and bool(_FLAG_RE.match(args[0]))
    return False


def _peel_prompt(line: str) -> tuple[str, str]:
    """Split a leading prompt off a stripped line. Returns (prompt, rest):
    '$'/'%' (user prompt), '#' (root prompt, only before an unambiguous
    command, otherwise it's a comment), '>' (PS2 continuation or an
    email quote) or '' for none."""
    if len(line) >= 2 and line[0] in "$%>#" and line[1] in " \t":
        rest = line[2:].strip()
        if line[0] == "#":
            first = rest.split(maxsplit=1)[0] if rest else ""
            if first not in _UNAMBIGUOUS_COMMANDS:
                return "", line
        return line[0], rest
    return "", line


def _line_is_command(line: str, *, prompted: bool) -> bool:
    """Does one prompt-free line read as a shell command? Checks the
    first segment, any segment after ;/&&/|| that starts with an
    unambiguous command, and segments after | only when the left side
    was a command."""
    parts = _SHELL_SEP_RE.split(line)
    segments, seps = parts[0::2], ["", *parts[1::2]]
    prev_cmd = False
    for i, seg in enumerate(segments):
        nxt = segments[i + 1].split() if i + 1 < len(segments) else []
        piped = (
            i + 1 < len(seps)
            and seps[i + 1] == "|"
            and bool(nxt)
            and nxt[0] in _UNAMBIGUOUS_COMMANDS
        )
        if i == 0:
            is_cmd = _segment_is_command(seg, prompted=prompted, piped_into_command=piped)
        elif seps[i] == "|":
            is_cmd = prev_cmd and bool(seg.split())
        else:
            words = seg.split()
            is_cmd = bool(words) and words[0].lstrip("({!") in _UNAMBIGUOUS_COMMANDS
        if is_cmd:
            return True
        prev_cmd = is_cmd
    return False


def _looks_like_command(stripped: str) -> bool:
    """Do most lines of the selection read as shell?

    Each non-blank line is a command, prose, or neutral (comments, shell
    structure like 'fi' / 'done', backslash continuations, heredoc
    bodies, bare assignments). The selection is a command when at least
    one line is and commands are not outnumbered by prose, so

        # Install foo
        sudo apt install foo

    still classifies, while a hard-wrapped paragraph with one line that
    starts "at the end..." doesn't. A shebang, or any '$ cmd' prompt line
    (a copied terminal session, output lines and all), settles it (pinned by
    `tests/test_classifier.py::test_real_commands_stay_commands`,
    `tests/test_classifier.py::test_prose_is_not_a_command`).
    """
    commands = prose = 0
    continued = False
    heredoc_end: str | None = None
    for raw in stripped.splitlines():
        line = raw.strip()
        if heredoc_end is not None:
            if line == heredoc_end:
                heredoc_end = None
            continue
        if not line:
            continued = False
            continue
        was_continued, continued = continued, line.endswith("\\")
        if was_continued:
            continue
        if line.startswith("#!"):
            return True
        prompt, body = _peel_prompt(line)
        if prompt in ("$", "%", "#"):
            if body and _line_is_command(body, prompted=True):
                return True
        elif body.startswith("#"):
            continue
        heredoc = _HEREDOC_RE.search(body)
        if heredoc:
            heredoc_end = heredoc.group(1)
        words = body.split()
        head = words[0] if words else ""
        if head in _SHELL_NEUTRAL_HEADS or (
            _ASSIGN_RE.match(head) and len(words) == 1 and not prompt
        ):
            continue
        if head in _SHELL_OPENERS or _FUNC_DEF_RE.match(body):
            if _OPENER_TAIL_RE.search(body) or _FUNC_DEF_RE.match(body) or head == "case":
                commands += 1
            else:
                prose += 1
            continue
        if _line_is_command(body, prompted=False):
            commands += 1
        else:
            prose += 1
    return commands > 0 and commands >= prose


def strip_shell_prompts(text: str) -> str:
    """Turn a selection the classifier called COMMAND into something bash
    can run: drop leading '$ ', '% ', '> ' and root '# ' prompts.

    Only applies when the text is a prompt paste, i.e. some line has a
    '$ '/'% ' prompt, or every line has some prompt. Then unprompted
    lines are command output and are dropped (backslash continuations of
    a kept line stay). A script with '# comments' and no prompts is
    returned unchanged, so its comments stay comments.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    peeled = [_peel_prompt(ln.strip()) for ln in lines]
    user_prompt = any(p in ("$", "%") for p, _ in peeled)
    if not lines or not (user_prompt or all(p for p, _ in peeled)):
        return text
    kept: list[str] = []
    continued = False
    for raw, (prompt, body) in zip(lines, peeled, strict=True):
        if prompt:
            kept.append(body)
        elif continued:
            kept.append(raw.rstrip())
        else:
            continue
        continued = kept[-1].endswith("\\")
    return "\n".join(kept)


def _normalize(text: str) -> str:
    """`text` without surrounding whitespace, Unicode format characters (Cf:
    BOM, ZWJ, direction marks) or _INVISIBLE_CHARS."""
    cleaned = "".join(
        c for c in text if unicodedata.category(c) != "Cf" and c not in _INVISIBLE_CHARS
    )
    return cleaned.strip()


def _is_naked_url(s: str) -> bool:
    """A scheme-less URL, which needs a likely TLD so code like "foo.bar"
    stays text (pinned by `tests/test_classifier.py::test_a_naked_url_needs_a_likely_tld`)."""
    if not _NAKED_URL_RE.match(s) or "@" in s:
        return False
    host = s.split("/", 1)[0]
    return host.rsplit(".", 1)[-1].lower() in _LIKELY_TLDS


def _is_email(s: str) -> bool:
    """No dots inside a domain label, so the regex stays linear (pinned by
    `tests/test_classifier.py::test_email_regex_is_linear`,
    `tests/test_classifier.py::test_email_still_matches_real_addresses`)."""
    return len(s) <= _RFC_5321_MAX_ADDRESS_LEN and bool(_EMAIL_RE.match(s))


def _has_shell_only_syntax(s: str) -> bool:
    """&&, ||, $( ), ${ } or a heredoc anywhere is a command, in a selection
    short enough to search (pinned by
    `tests/test_classifier.py::test_a_shell_operator_in_prose_is_enough_up_to_the_length_cap`).
    Flags, paths, redirects and single pipes are judged per segment instead."""
    return len(s) < _COMMAND_HINTS_MAX_LEN and bool(_COMMAND_HINTS.search(s))


def classify(text: str) -> ContentType:
    """Best-effort classification of a single selection."""
    if not text:
        return ContentType.PLAIN_TEXT

    stripped = _normalize(text)
    if not stripped:
        return ContentType.PLAIN_TEXT

    if _URL_RE.match(stripped) or _is_naked_url(stripped):
        return ContentType.URL
    if _is_email(stripped):
        return ContentType.EMAIL
    if "\n" not in stripped and _PATH_RE.match(stripped):
        return ContentType.PATH
    if _looks_like_command(stripped) or _has_shell_only_syntax(stripped):
        return ContentType.COMMAND
    return ContentType.PLAIN_TEXT


_RUNNABLE_PATH_EXTS = (
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".py",
    ".pl",
    ".rb",
    ".js",
    ".ts",
    ".lua",
    ".tcl",
    ".php",
    ".awk",
    ".sed",
    ".out",
    ".bin",
)


def is_runnable_path(text: str) -> bool:
    """Whether a path selection also gets Run in terminal beside Open path:
    a script's extension, or './foo' and '~/bin/foo' without one."""
    s = _normalize(text)
    if not s or "\n" in s:
        return False
    if not _PATH_RE.match(s):
        return False
    low = s.lower()
    if low.endswith(_RUNNABLE_PATH_EXTS):
        return True
    return bool(s.startswith(("./", "~/bin/")))
