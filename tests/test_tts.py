"""Read aloud (#45): the cached vinoSpeak voice in vino_ai/tts.py and the
Speak plugin, with vino_speak faked. No OpenVINO, model or audio needed."""

from __future__ import annotations

import sys
import types

import pytest

from vino_ai import tts

# The real one, uncached; the fixture stubs tts.player
_pick_player = tts.player.__wrapped__


class _Speech:
    def __init__(self, text):
        self.text = text
        self.stopped = False
        self.finished = False

    @property
    def done(self):  # a property in vino_speak.Speech
        return self.stopped or self.finished

    def stop(self):
        self.stopped = True

    def wait(self, timeout=None):
        return True


class _Synth:
    made: list[str] = []
    refuse: set[str] = set()

    def __init__(self, device):
        _Synth.made.append(device)
        if device in _Synth.refuse:
            raise RuntimeError(f"{device} can't compile")
        self.device = device
        self.spoken: list[_Speech] = []

    def stream(self, text):
        return text  # what the fake Speech "plays"


@pytest.fixture(autouse=True)
def fake_vinospeak(monkeypatch):
    mod = types.ModuleType("vino_speak")
    mod.Synthesizer = _Synth
    mod.Speech = lambda audio, player: _Speech(audio)
    monkeypatch.setitem(sys.modules, "vino_speak", mod)
    monkeypatch.setattr(tts, "player", lambda: "/usr/bin/paplay")
    _Synth.made, _Synth.refuse = [], set()
    monkeypatch.setattr(tts.devices, "candidates", lambda order: list(order))
    monkeypatch.setattr(tts, "_idle_minutes", lambda: 5.0)
    monkeypatch.setattr(tts, "_schedule_unload", lambda: None)
    monkeypatch.setattr(tts, "_synth", None)
    monkeypatch.setattr(tts, "_speech", None)
    yield


def test_loads_once_on_the_npu_and_reuses_it():
    first = tts.speak("Hello there.")
    second = tts.speak("Again.")
    assert _Synth.made == ["NPU"]
    assert tts.device() == "NPU"
    # A new read cuts off the one still playing
    assert first.stopped and not second.stopped


def test_falls_back_when_the_npu_fails_to_compile():
    _Synth.refuse = {"NPU"}
    tts.speak("Hello.")
    assert _Synth.made == ["NPU", "GPU"]
    assert tts.device() == "GPU"


def test_no_device_compiles():
    _Synth.refuse = {"NPU", "GPU", "CPU"}
    with pytest.raises(RuntimeError, match="CPU can't compile"):
        tts.speak("Hello.")
    assert not tts.loaded()


def test_text_is_tidied_and_capped():
    speech = tts.speak("  one\n\ntwo\t three " + "x" * (tts.MAX_CHARS * 2))
    assert speech.text.startswith("one two three x")
    assert len(speech.text) == tts.MAX_CHARS
    with pytest.raises(ValueError):
        tts.speak(" \n ")


def test_stop_says_whether_anything_was_playing():
    assert not tts.stop()
    speech = tts.speak("Hello.")
    assert tts.speaking()
    assert tts.stop() and speech.stopped
    assert not tts.speaking() and not tts.stop()
    finished = tts.speak("Hello.")
    finished.finished = True
    assert not tts.stop()


def test_idle_unload_waits_for_the_speech_to_end(monkeypatch):
    now = [1000.0]  # whole seconds, so the 300 s boundary is exact
    monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])
    speech = tts.speak("A long passage.")
    now[0] += 301
    assert not tts.unload_if_idle()  # still talking
    speech.finished = True
    assert not tts.unload_if_idle()
    assert tts._last_used == now[0]
    now[0] += 299
    assert not tts.unload_if_idle()
    now[0] += 1
    assert tts.unload_if_idle()
    assert not tts.loaded()


def test_idle_unload_off(monkeypatch):
    tts.speak("Hello.").finished = True
    monkeypatch.setattr(tts, "_last_used", 0.0)
    monkeypatch.setattr(tts, "_idle_minutes", lambda: 0.0)
    assert not tts.unload_if_idle()


def test_an_old_pw_play_is_skipped(monkeypatch):
    playback = types.ModuleType("vino_speak.playback")
    playback.PLAYERS = ("pw-play", "paplay", "aplay")
    monkeypatch.setitem(sys.modules, "vino_speak.playback", playback)
    monkeypatch.setattr(tts.shutil, "which", lambda name: f"/usr/bin/{name}")
    pick = _pick_player
    helps = {"new": "  --raw  raw mode\n", "old": "  --format  Sample format\n"}
    for version, expected in (("new", "/usr/bin/pw-play"), ("old", "/usr/bin/paplay")):
        monkeypatch.setattr(
            tts.subprocess,
            "run",
            lambda *_a, v=version, **_k: types.SimpleNamespace(stdout=helps[v], stderr=""),
        )
        assert pick() == expected, version


def test_unavailable_without_vinospeak(monkeypatch):
    monkeypatch.setattr(tts.importlib.util, "find_spec", lambda _name: None)
    ok, reason = tts.available()
    assert not ok and "requirements-ai.txt" in reason


# ---- the plugin ---------------------------------------------------------------


@pytest.fixture
def plugin(monkeypatch):
    import speak

    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        speak.actions, "notify", lambda title, body, *_a, **_k: notes.append((title, body))
    )
    monkeypatch.setattr(speak, "_esc_watch", lambda _stop: lambda: None)
    monkeypatch.setattr(tts, "available", lambda: (True, ""))
    return speak, notes


def test_plugin_registers_read_aloud(plugin):
    speak, _ = plugin
    registered = []
    speak.register(registered.append)
    assert [(p.name, p.icon, p.tooltip) for p in registered] == [
        ("speak", "audio-speakers-symbolic", "Read aloud")
    ]


def test_plugin_reads_and_says_it_is_loading(plugin):
    speak, notes = plugin
    speak._read("Hello there.")
    assert tts._speech.text == "Hello there."
    # First use: the compile can take a while, so say something's happening
    assert notes == [("Read aloud", "Loading the voice…")]
    tts._speech.finished = True
    notes.clear()
    speak._read("Hello again.")
    assert notes == []


def test_a_second_click_stops_it(plugin, monkeypatch):
    speak, _ = plugin
    playing = tts.speak("Hello there.")
    # wait() returns at once in the fake, so the read stays "playing"
    speak._read("Something else.")
    assert playing.stopped
    assert tts._speech is None  # stopped, not replaced


def test_plugin_explains_a_missing_install(plugin, monkeypatch):
    speak, notes = plugin
    monkeypatch.setattr(tts, "available", lambda: (False, "vinoSpeak isn't installed"))
    speak._read("Hello.")
    assert notes == [("Read aloud", "vinoSpeak isn't installed")]
    assert not tts.loaded()


def test_plugin_reports_a_failed_load(plugin):
    speak, notes = plugin
    _Synth.refuse = {"NPU", "GPU", "CPU"}
    speak._read("Hello.")
    assert notes[-1][1].startswith("Couldn't read it aloud")


def test_one_load_at_a_time(plugin):
    speak, _ = plugin
    # A click while another is still loading the voice is dropped
    speak._busy.acquire()
    try:
        speak._read("Hello.")
    finally:
        speak._busy.release()
    assert not tts.loaded()
