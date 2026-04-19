"""In-memory transport for tests and offline experiments.

:class:`FakeTransport` supports three usage patterns:

1. **Scripted** — supply an ordered list of ``(expected_payload, reply)`` pairs via
   :meth:`scripted`. Each :meth:`request` call pops the next pair and asserts that the
   actual payload matches the expected payload (otherwise raises ``AssertionError``).
   This is the preferred form for golden-transcript tests.
2. **Queued** — call :meth:`queue_reply` to enqueue replies without pinning the expected
   payload. Useful when you only want to test the reply direction.
3. **Queued error** — :meth:`queue_error` enqueues an exception that the next
   :meth:`request` will raise, for exercising transport/device error paths.

All sent payloads are appended to :attr:`sent` (a public list) for inspection.
"""

from __future__ import annotations

from collections import deque
from types import TracebackType

from group3.exceptions import TransportError

_QueueItem = bytes | BaseException


class FakeTransport:
    """Drop-in test transport with no I/O."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._replies: deque[_QueueItem] = deque()
        self._scripted: deque[tuple[bytes, _QueueItem]] = deque()
        self._is_open = False

    # ------------------------------------------------------------------
    # test setup helpers
    # ------------------------------------------------------------------

    def queue_reply(self, reply: bytes) -> None:
        """Enqueue a reply to be returned by the next :meth:`request`."""
        self._replies.append(reply)

    def queue_error(self, exc: BaseException) -> None:
        """Enqueue an exception to be raised by the next :meth:`request`."""
        self._replies.append(exc)

    def scripted(self, exchanges: list[tuple[bytes, bytes]]) -> None:
        """Pin an ordered sequence of expected-sent + reply bytes.

        Any deviation between the actual payload and the scripted expectation raises
        ``AssertionError`` from :meth:`request`. This is the right tool for golden
        transcript tests.
        """
        for expected, reply in exchanges:
            self._scripted.append((expected, reply))

    def scripted_error(self, exchanges: list[tuple[bytes, BaseException]]) -> None:
        """Pin an ordered sequence of expected-sent + exception-to-raise pairs."""
        for expected, err in exchanges:
            self._scripted.append((expected, err))

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._is_open = True

    def close(self) -> None:
        self._is_open = False

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        del timeout  # Fake transport does not simulate timeouts unless queued as an error.
        if not self._is_open:
            raise TransportError("FakeTransport is not open. Call open() first.")
        self.sent.append(payload)

        if self._scripted:
            expected, item = self._scripted.popleft()
            assert payload == expected, (
                f"FakeTransport scripted mismatch: expected {expected!r}, got {payload!r}"
            )
            return _unwrap(item)

        if self._replies:
            return _unwrap(self._replies.popleft())

        raise AssertionError(
            f"FakeTransport has no reply queued for payload {payload!r}. "
            f"Use queue_reply(), queue_error(), or scripted() in your test setup."
        )

    def write_only(self, payload: bytes) -> None:
        if not self._is_open:
            raise TransportError("FakeTransport is not open. Call open() first.")
        self.sent.append(payload)
        # If a scripted sequence is active, consume its next entry and assert the
        # expected payload matches — scripted is a golden transcript of every
        # byte on the wire, request or broadcast. The scripted reply is
        # discarded (broadcasts have no reply).
        if self._scripted:
            expected, _item = self._scripted.popleft()
            assert payload == expected, (
                f"FakeTransport scripted mismatch on write_only: "
                f"expected {expected!r}, got {payload!r}"
            )
        # The queued-reply deque is *not* touched — write_only has no read half.

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> FakeTransport:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _unwrap(item: _QueueItem) -> bytes:
    if isinstance(item, BaseException):
        raise item
    return item
