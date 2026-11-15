"""vino_ai.idle: the unload timer the LLM, OCR and the voice share."""

from vino_ai import idle


def test_a_positive_timeout_arms_a_daemon_timer_just_past_the_deadline():
    timer = idle.rearm(None, 5.0, lambda: None)
    try:
        assert timer is not None and timer.daemon and timer.interval == 301
    finally:
        timer.cancel()


def test_zero_minutes_arms_no_timer():
    assert idle.rearm(None, 0.0, lambda: None) is None


def test_rearming_cancels_the_timer_it_replaces():
    old = idle.rearm(None, 5.0, lambda: None)
    new = idle.rearm(old, 5.0, lambda: None)
    try:
        assert old.finished.is_set() and new is not old
    finally:
        new.cancel()


def test_a_setting_that_cannot_be_read_falls_back_to_the_default(monkeypatch):
    import core.settings

    def broken():
        raise OSError("no config dir")

    monkeypatch.setattr(core.settings, "get_settings", broken)
    assert idle.float_setting("ai_idle_unload_minutes", 5.0) == 5.0
