import time

import pytest

from core.classifier import (
    _AMBIGUOUS_COMMANDS,
    _COMMAND_HINTS,
    _COMMAND_HINTS_MAX_LEN,
    _COMMAND_TOKENS,
    _EMAIL_RE,
    _RFC_5321_MAX_ADDRESS_LEN,
    ContentType,
    classify,
    is_runnable_path,
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
    pytest.param(
        "$ ls -la\ntotal 8\ndrwxr-xr-x  2 dev dev 4096 Oct 17 10:00 .\n"
        "-rw-r--r--  1 dev dev   12 Oct 17 10:00 notes.txt",
        id="terminal-session-with-output",
    ),
]


@pytest.mark.parametrize("text", COMMANDS)
def test_real_commands_stay_commands(text):
    assert classify(text) is ContentType.COMMAND


PROSE = [
    "go home",
    "file a bug please",
    "id like to go",
    "Fine; time will tell",
    "the results | more info below",
    "We spent most of the week on the migration and\n"
    "at the end of it the old cluster was finally gone,\n"
    "which meant the on-call rota could go back to normal.\n"
    "file handling is still slow though.",
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
    adversarial = "a@" + "b." * 16000 + "@"
    assert _elapsed_ms(_EMAIL_RE.match, adversarial) < 50
    assert _elapsed_ms(classify, adversarial) < 50


def test_email_still_matches_real_addresses():
    assert classify("first.last+tag@mail.example.co.uk") is ContentType.EMAIL
    assert classify("a@b..c") is not ContentType.EMAIL


def test_an_address_at_the_rfc_5321_limit_is_an_email():
    local = "x" * (_RFC_5321_MAX_ADDRESS_LEN - len("@example.com"))
    assert classify(local + "@example.com") is ContentType.EMAIL


def test_an_address_over_the_rfc_5321_limit_is_not():
    local = "x" * (_RFC_5321_MAX_ADDRESS_LEN - len("@example.com") + 1)
    assert classify(local + "@example.com") is not ContentType.EMAIL


@pytest.mark.parametrize(
    "adversarial",
    [
        pytest.param("x-" + "a-" * 16000 + "!", id="hyphenated-name"),
        pytest.param("x\n" + "\n" * 32000 + "X", id="blank-line-run"),
        pytest.param("-" + "a-" * 16000 + "!", id="flag-like"),
        pytest.param(".a{b:c}" * 4000 + "@media x{}", id="minified-css-with-one-at"),
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
    ],
)
def test_strip_shell_prompts(text, expected):
    assert strip_shell_prompts(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$ ls -la\ntotal 8\n$ cd /tmp", "ls -la\ncd /tmp"),
        (
            "$ sudo dnf install \\\n    foo bar\nLast metadata expiration",
            "sudo dnf install \\\n    foo bar",
        ),
        ("$ for x in a b; do\n> echo $x\n> done\na\nb", "for x in a b; do\necho $x\ndone"),
    ],
)
def test_a_session_paste_drops_output_lines_but_keeps_continuations(text, expected):
    assert strip_shell_prompts(text) == expected


@pytest.mark.parametrize(
    "script",
    [
        "# Install foo\nsudo apt install foo",
        "# make sure foo is installed\nmake install",
        "echo hi # comment",
    ],
)
def test_a_script_without_prompts_keeps_its_comments(script):
    assert strip_shell_prompts(script) == script


def test_ambiguous_commands_are_known_commands():
    """A typo in _AMBIGUOUS_COMMANDS would make a word neither ambiguous nor a command."""
    assert _AMBIGUOUS_COMMANDS <= _COMMAND_TOKENS


@pytest.mark.parametrize("text", ["go run errands", "go get lunch"])
def test_go_run_and_go_get_are_not_subcommands(text):
    assert classify(text) is ContentType.PLAIN_TEXT
    assert classify("go build ./...") is ContentType.COMMAND


def test_a_shell_operator_in_prose_is_enough_up_to_the_length_cap():
    def prose(length):
        tail = " && fox"
        return "x" * (length - len(tail)) + tail

    assert classify(prose(_COMMAND_HINTS_MAX_LEN - 1)) is ContentType.COMMAND
    assert classify(prose(_COMMAND_HINTS_MAX_LEN)) is ContentType.PLAIN_TEXT


def test_ping_counts_with_a_host():
    assert classify("ping example.com") is ContentType.COMMAND


def test_ping_without_a_host_is_prose():
    assert classify("ping me") is ContentType.PLAIN_TEXT


def test_kill_counts_with_a_pid():
    assert classify("kill 1234") is ContentType.COMMAND


def test_kill_without_a_number_is_prose():
    assert classify("kill time") is ContentType.PLAIN_TEXT


def test_a_listed_hyphenated_tool_counts_with_a_bare_argument():
    assert classify("ssh-keygen mykey") is ContentType.COMMAND


def test_an_unlisted_hyphenated_word_needs_evidence():
    assert classify("self-hosted apps") is ContentType.PLAIN_TEXT
    assert classify("self-hosted --flag") is ContentType.COMMAND


def test_markdown_emphasis_is_not_a_glob():
    assert classify("make *sure* you save") is ContentType.PLAIN_TEXT
    assert classify("cat *.txt") is ContentType.COMMAND


def test_an_unknown_tool_counts_with_a_flag_but_not_with_a_path():
    assert classify("obscuretool --foo") is ContentType.COMMAND
    assert classify("obscuretool /etc/foo") is ContentType.PLAIN_TEXT


def test_a_naked_url_needs_a_likely_tld():
    assert classify("github.com/torvalds/linux") is ContentType.URL
    assert classify("foo.bar") is ContentType.PLAIN_TEXT


def test_invisible_characters_from_a_rich_text_copy_are_ignored():
    assert classify("\u200bhttps://example.com\ufeff") is ContentType.URL


@pytest.mark.parametrize(
    ("path", "runnable"),
    [
        ("./build.sh", True),
        ("/opt/x.py", True),
        ("./configure", True),
        ("~/bin/foo", True),
        ("~/notes/foo", False),
        ("/usr/bin/env", False),
        ("build.sh", False),
        ("./a\n./b", False),
    ],
)
def test_runnable_paths(path, runnable):
    assert is_runnable_path(path) is runnable
