"""Base class for vinoPop plugins.

A plugin exposes one action: a button in the popup. It declares which
content types it cares about, which icon and label to render, and what
to do when the user clicks it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from core.classifier import ContentType


@dataclass
class Plugin:
    """One popup button.

    No `content_types` means every content type. `predicate`, when set,
    must also accept the selection text; it runs on every popup show, so
    keep it cheap, and one that raises hides the button (pinned by
    `tests/test_plugins_base.py::test_no_content_types_means_every_type`,
    `tests/test_plugins_base.py::test_listed_content_types_limit_the_button`,
    `tests/test_plugins_base.py::test_a_predicate_that_raises_hides_the_button`).
    `requires_editable` hides the button when the focused widget isn't
    editable, for actions such as Cut or Bold that would do nothing there.
    """

    name: str
    icon: str
    tooltip: str
    handler: Callable[[str], None]
    content_types: Iterable[ContentType] = field(default_factory=tuple)
    priority: int = 100
    predicate: Callable[[str], bool] | None = None
    requires_editable: bool = False

    def handles(self, content_type: ContentType) -> bool:
        if not self.content_types:
            return True
        return content_type in self.content_types

    def matches(self, text: str) -> bool:
        """Whether the button shows for this text, after handles()."""
        if self.predicate is None:
            return True
        try:
            return bool(self.predicate(text))
        except Exception:
            return False

    def execute(self, text: str) -> None:
        self.handler(text)
