import time

import pytest

from core.classifier import (
    _AMBIGUOUS_COMMANDS,
    _COMMAND_HINTS,
    _COMMAND_TOKENS,
    _EMAIL_RE,
    ContentType,
    classify,
    strip_shell_prompts,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/karanshukla/vinoPop", ContentType.URL),
        ("someone@example.com", ContentType.EMAIL),
        ("sudo dnf install openvino", ContentType.COMMAND),
        ("ls -la ~/Downloads", ContentType.COMMAND),
        ("/etc/os-release", ContentType.PATH),
        ("The quick brown fox jumps over the lazy dog.", ContentType.PLAIN_TEXT),
    ],
)
def test_classify(text, expected):
    assert classify(text) is expected


def test_classify_ignores_surrounding_whitespace():
    assert classify("  https://example.com/\n") is ContentType.URL


COMMANDS = [
    "sudo dnf install foo",
    "sudo apt update && sudo apt upgrade -y",
    "git log --oneline | head",
    "$ ls -la",
    "# sudo dnf update",
    "> sudo dnf update",
    "kquitapp6 plasmashell ; kstart6 plasmashell",
    "cat x | grep y",
    "mkdir d && cd d",
    "--dry-run",
    "./configure --prefix=/usr",
    "obscuretool --foo",
    "xdg-settings get default-web-browser",
    "gtk-update-icon-cache -f -t /usr/share/icons/hicolor",
    "systemd-run --user foo",
    "dpkg-reconfigure tzdata",
    'export PATH="$HOME/.local/bin:$PATH"',
    "python3 -m venv .venv",
    "python main.py",
    "make install",
    "go build ./...",
    "cargo build --release",
    "echo $XDG_SESSION_TYPE",
    "echo hello > out.txt",
    "kill -9 1234",
    "kill 1234",
    "sleep 5",
    "htop",
    "free -h",
    "which python3",
    "curl -fsSL https://example.com/install.sh | bash",
    "ping 1.1.1.1",
    "code .",
    "LANG=C sort foo.txt",
    "find . -name '*.py' -delete",
    "man ls",
    "wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+",
    'for f in *.png; do convert "$f" "${f%.png}.jpg"; done',
    "#!/bin/bash\necho hi",
    "# Install foo\nsudo apt install foo",
    "# Restart plasma\nkquitapp6 plasmashell\nkstart plasmashell &",
    "sudo dnf install -y \\\n    gtk4-devel \\\n    libadwaita-devel",
    "git clone https://github.com/x/y\ncd y\n./install.sh",
    "cat <<EOF > ~/.config/x.conf\nfoo=bar\nbaz=qux\nEOF",
    "if [ -f ~/.bashrc ]; then\n  source ~/.bashrc\nfi",
    "echo 'export FOO=1' >> ~/.bashrc\nsource ~/.bashrc",
    # copied terminal session, output lines included
    "$ ls -la\ntotal 8\ndrwxr-xr-x  2 dev dev 4096 Oct 17 10:00 .\n"
    "-rw-r--r--  1 dev dev   12 Oct 17 10:00 notes.txt",
]


@pytest.mark.parametrize("text", COMMANDS)
def test_real_commands_stay_commands(text):
    assert classify(text) is ContentType.COMMAND


PROSE = [
    # the #29 repros
    "go home",
    "file a bug please",
    "id like to go",
    "Fine; time will tell",
    "the results | more info below",
    "We spent most of the week on the migration and\n"
    "at the end of it the old cluster was finally gone,\n"
    "which meant the on-call rota could go back to normal.\n"
    "file handling is still slow though.",
    # same class
    "Settings > Privacy > Camera",
    "Set `terminal_keep_open` to false in settings.",
    "self-hosted apps are great",
    "python is a great language",
    "make sure you save first",
    "cat food is on sale",
    "free time",
    "help",
    "Channel Name | Category",
    "Learn Git | git tutorial for beginners",
    "time flies when you're having fun",
    "which one do you prefer?",
    "go away /s",
    "watch the video first",
    "for the record, it works",
    "if you want, ping me",
    "First, update your system:\nsudo dnf upgrade\nThen reboot and log back in.\n"
    "Your settings should carry over.",
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_is_not_a_command(text):
    assert classify(text) is not ContentType.COMMAND


def _elapsed_ms(fn, *args):
    start = time.perf_counter()
    fn(*args)
    return (time.perf_counter() - start) * 1000


def test_email_regex_is_linear():
    # used to backtrack across every dot: 32k chars took ~7 s on the GTK thread
    adversarial = "a@" + "b." * 16000 + "@"
    assert _elapsed_ms(_EMAIL_RE.match, adversarial) < 50
    assert _elapsed_ms(classify, adversarial) < 50


def test_email_still_matches_real_addresses():
    assert classify("first.last+tag@mail.example.co.uk") is ContentType.EMAIL
    assert classify("a@b..c") is not ContentType.EMAIL
    assert classify("x" * 320 + "@example.com") is not ContentType.EMAIL


@pytest.mark.parametrize(
    "adversarial",
    [
        "x-" + "a-" * 16000 + "!",  # old hyphenated-name hint was exponential
        "x\n" + "\n" * 32000 + "X",  # and quadratic on blank-line runs
        "-" + "a-" * 16000 + "!",
        ".a{b:c}" * 4000 + "@media x{}",  # minified CSS with one '@'
    ],
)
def test_classify_is_fast_on_adversarial_input(adversarial):
    assert _elapsed_ms(classify, adversarial) < 50


def test_command_hints_regex_is_fast():
    assert _elapsed_ms(_COMMAND_HINTS.search, "x-" + "a-" * 2000 + "<<" + "\n" * 2000) < 50


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$ sudo dnf update", "sudo dnf update"),
        ("% ls -la", "ls -la"),
        ("# sudo dnf update", "sudo dnf update"),
        ("> sudo dnf update", "sudo dnf update"),
        # session paste: output lines go, continuations stay
        ("$ ls -la\ntotal 8\n$ cd /tmp", "ls -la\ncd /tmp"),
        (
            "$ sudo dnf install \\\n    foo bar\nLast metadata expiration",
            "sudo dnf install \\\n    foo bar",
        ),
        ("$ for x in a b; do\n> echo $x\n> done\na\nb", "for x in a b; do\necho $x\ndone"),
        # scripts without prompts are left alone, comments included
        ("# Install foo\nsudo apt install foo", "# Install foo\nsudo apt install foo"),
        (
            "# make sure foo is installed\nmake install",
            "# make sure foo is installed\nmake install",
        ),
        ("echo hi # comment", "echo hi # comment"),
    ],
)
def test_strip_shell_prompts(text, expected):
    assert strip_shell_prompts(text) == expected


def test_ambiguous_commands_are_known_commands():
    # a typo here would silently make a word neither ambiguous nor a command
    assert _AMBIGUOUS_COMMANDS <= _COMMAND_TOKENS
