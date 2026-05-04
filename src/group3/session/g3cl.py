"""G3CL (Group3 Communication Loop) session.

A G3CL is a daisy-chained serial bus with up to 31 Group3 devices addressed 0..30
(manual section 4.5.1). The host sends one character stream into the loop; each
device retransmits the message to the next, so the host can confirm every device
heard the command when the message returns.

Addressed commands are sent as ``An<cr>`` followed by the actual command. Once a
device has been addressed, it remains the active listener until a different address
is issued. We model this at the :class:`AddressedProtocol` level: every call
pre-sends ``A<n>`` and then the requested command. This is conservative — it wastes
a few bytes in tight loops but eliminates a whole class of "who's listening now?"
bugs.

The ``V`` (trigger) command is explicitly **not** prefixed with an address: manual
section 4.7 states that V is the only simultaneously-obeyed command, and all devices
previously placed in triggered mode respond. :meth:`G3CLSession.broadcast_trigger`
implements this.
"""

from __future__ import annotations

from types import TracebackType

from group3.exceptions import CommandError
from group3.protocol import commands
from group3.protocol.core import Group3Protocol


class AddressedProtocol:
    """A thin wrapper around :class:`Group3Protocol` that prefixes every command with ``An``.

    Instances are produced by :meth:`G3CLSession.select` — users should not
    construct them directly.
    """

    def __init__(self, inner: Group3Protocol, address: int) -> None:
        self._inner = inner
        if not 0 <= address <= 30:
            raise CommandError(f"address must be 0..30, got {address}")
        self._address = address

    @property
    def address(self) -> int:
        """Return the G3CL device address this protocol addresses."""
        return self._address

    def send(self, command: str, timeout: float | None = None) -> str:
        """Address the device, then send ``command`` and return the reply.

        Per manual §4.5.2, ``An`` is described as silent. We send it via
        ``write_only`` and then send the real command which does return a
        reply. (Note: every probed setter on the bench DTM-151-S actually acks
        with a bare ``\\n`` despite the manual's "silent" wording, so ``An``
        may behave the same — this code path has not yet been verified
        against real multi-drop hardware.)
        """
        self._inner.send_no_reply(commands.a_set_address(self._address))
        return self._inner.send(command, timeout=timeout)

    def send_setter(self, command: str, error_window: float | None = None) -> None:
        """Address the device, then send a silent-success setter.

        Forwards to :meth:`Group3Protocol.send_setter`, which drains the line
        for deferred error replies (manual §4.5.3).
        """
        self._inner.send_no_reply(commands.a_set_address(self._address))
        if error_window is None:
            self._inner.send_setter(command)
        else:
            self._inner.send_setter(command, error_window=error_window)

    def send_no_reply(self, command: str) -> None:
        """Address the device, then send ``command`` without reading a reply."""
        self._inner.send_no_reply(commands.a_set_address(self._address))
        self._inner.send_no_reply(command)

    # Deliberately no ``read_next`` / ``drain_pending`` here.
    #
    # Streaming (``SM1``) is not supported on the G3CL addressed loop: the
    # device sends replies without an address tag, so a consumer listening on
    # one :class:`AddressedProtocol` would indiscriminately absorb readings
    # from any other device on the loop that also has ``SM1`` enabled. The
    # resulting ambiguity is worse than the feature is worth — use point-to-
    # point (a single :class:`Group3Protocol` wired to one device) for
    # streaming workflows. :meth:`DTM151Serial.stream_field` enforces this
    # with a runtime ``isinstance`` check.

    @property
    def last_raw_tx(self) -> bytes:
        return self._inner.last_raw_tx

    @property
    def last_raw_rx(self) -> bytes:
        return self._inner.last_raw_rx


class G3CLSession:
    """A handle to a Group3 Communication Loop.

    A session owns a single underlying :class:`Group3Protocol`. Call
    :meth:`select` to get an :class:`AddressedProtocol` for a specific device, or
    :meth:`broadcast_trigger` to fire the unaddressed ``V`` across every device
    that has been put in triggered mode.
    """

    def __init__(self, protocol: Group3Protocol) -> None:
        self._protocol = protocol

    @property
    def protocol(self) -> Group3Protocol:
        """Access the underlying protocol (e.g., to inspect ``last_raw_tx``)."""
        return self._protocol

    def select(self, address: int) -> AddressedProtocol:
        """Return an :class:`AddressedProtocol` bound to ``address``.

        Args:
            address: Device address, 0..30.

        Raises:
            CommandError: ``address`` is outside the valid range.
        """
        return AddressedProtocol(self._protocol, address)

    def broadcast_trigger(self) -> None:
        """Send the unaddressed ``V`` command to trigger all triggered-mode devices.

        Per manual section 4.7: ``V`` is the only command simultaneously obeyed by
        more than one device on the loop, and must *not* be preceded by an address.
        No reply is read — each device that responds will do so after its own
        measurement delay (up to 175 ms).
        """
        self._protocol.send_no_reply(commands.V)

    def device(self, address: int) -> DTM151Serial:
        """Convenience — return a :class:`DTM151Serial` bound to ``address``.

        Equivalent to ``DTM151Serial(session.select(address))``.
        """
        return DTM151Serial(self.select(address))

    # ------------------------------------------------------------------
    # context manager (no-op; transport open/close is the caller's concern)
    # ------------------------------------------------------------------

    def __enter__(self) -> G3CLSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# Imported at the bottom to avoid a circular import (models.dtm151 -> session types).
from group3.models.dtm151 import DTM151Serial  # noqa: E402
