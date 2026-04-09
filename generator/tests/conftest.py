"""Shared fixtures and configuration for tests."""

from __future__ import annotations

import pytest_regtest


def _normalize_non_latin1(output: str) -> str:
    """Replace non-Latin-1 characters so pytest-regtest's binary check passes.

    pytest-regtest treats characters above U+00FF as "unprintable".
    """
    return output.encode("latin-1", "replace").decode("latin-1")


pytest_regtest.register_converter_pre(_normalize_non_latin1)
