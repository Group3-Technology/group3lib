"""Transport layer — raw byte I/O to a Group3 device."""

from __future__ import annotations

from group3.transport.base import Transport
from group3.transport.fake import FakeTransport

__all__ = ["FakeTransport", "Transport"]
