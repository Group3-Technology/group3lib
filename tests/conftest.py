"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import pytest

from group3 import DTM151Serial, FakeTransport, Group3Protocol


@pytest.fixture
def fake() -> FakeTransport:
    """Return a fresh, opened FakeTransport for a test."""
    transport = FakeTransport()
    transport.open()
    return transport


@pytest.fixture
def protocol(fake: FakeTransport) -> Group3Protocol:
    """Return a Group3Protocol wrapping the FakeTransport fixture."""
    return Group3Protocol(fake)


@pytest.fixture
def dtm(protocol: Group3Protocol) -> DTM151Serial:
    """Return a DTM151Serial wrapping the protocol fixture."""
    return DTM151Serial(protocol)
