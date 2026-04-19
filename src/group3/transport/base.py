"""Transport interface.

The :class:`Transport` protocol is intentionally tiny. The protocol layer owns ASCII
encode/decode and terminator logic — transports deal only in bytes. This keeps the
pyserial dependency at the edge and lets users plug in their own transport (e.g. a
TCP-to-serial bridge, a test harness, a logging wrapper) by duck-typing.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """A bidirectional byte stream to a single Group3 device or G3CL loop.

    Implementations must be safe to call sequentially from a single thread. The
    library does not lock transports internally — if a user wants concurrent access,
    they wrap the transport themselves.
    """

    def open(self) -> None:
        """Open the underlying resource. Idempotent."""

    def close(self) -> None:
        """Close the underlying resource. Idempotent."""

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        """Write ``payload`` and read a reply until a terminator or timeout.

        The transport returns the raw bytes received, *including* the device's
        string terminator. The protocol layer strips and validates the terminator.

        Args:
            payload: The full command bytes, terminator included.
            timeout: Seconds to wait for a reply. ``None`` means use the transport's
                configured default.

        Returns:
            The raw reply bytes.

        Raises:
            TransportError: Any underlying I/O failure.
            TimeoutError: No reply within ``timeout``.
        """

    def write_only(self, payload: bytes) -> None:
        """Write ``payload`` without reading a reply. Used for broadcast commands."""

    def __enter__(self) -> Transport:
        """Open on context entry. Returns self for ``with x.open() as t:`` idiom."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close on context exit."""
