"""Provide deterministic token counting fixtures for offline tests."""

import pytest


class CharCounter:
    """One token per character: deterministic, without a tokenizer."""

    def count(self, text: str) -> int:
        return len(text)


@pytest.fixture
def char_counter():
    return CharCounter()
