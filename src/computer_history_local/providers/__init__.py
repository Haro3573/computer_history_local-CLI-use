"""Which Provider a name selects -- the one place `summarize` and `ask`
both go through in `__main__.py`, instead of each constructing
`FakeProvider()`/`ClaudeCliProvider()` inline (previously duplicated at
both call sites, along with the `choices=` tuple below)."""

from __future__ import annotations

from .claude_cli import CLAUDE_CLI_PROVIDER_ID, ClaudeCliProvider
from .fake import FAKE_PROVIDER_ID, FakeProvider

PROVIDER_CHOICES = (FAKE_PROVIDER_ID, CLAUDE_CLI_PROVIDER_ID)


def provider_for(name: str):
    return FakeProvider() if name == FAKE_PROVIDER_ID else ClaudeCliProvider()
