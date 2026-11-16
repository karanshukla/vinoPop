"""When a Plugin's button shows: content types, then the predicate."""

from core.classifier import ContentType
from plugin_system.plugin_base import Plugin


def _plugin(**kwargs) -> Plugin:
    return Plugin(name="p", icon="i", tooltip="t", handler=lambda _text: None, **kwargs)


def test_no_content_types_means_every_type():
    plugin = _plugin()
    assert all(plugin.handles(content_type) for content_type in ContentType)


def test_listed_content_types_limit_the_button():
    plugin = _plugin(content_types=(ContentType.URL,))
    assert plugin.handles(ContentType.URL)
    assert not plugin.handles(ContentType.PLAIN_TEXT)


def test_a_predicate_that_accepts_shows_the_button():
    assert _plugin(predicate=lambda text: text == "yes").matches("yes")


def test_a_predicate_that_raises_hides_the_button():
    def broken(_text: str) -> bool:
        raise ValueError("bad input")

    assert not _plugin(predicate=broken).matches("anything")
