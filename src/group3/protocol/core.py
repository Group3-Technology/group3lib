"""Group3Protocol — ties transport + codec + parser together.

One ``Group3Protocol`` wraps a single :class:`~group3.transport.base.Transport` and
provides two public methods:

* :meth:`send` — for request/reply commands. Encodes, writes, reads until terminator,
  strips the terminator, checks for device-reported errors, returns the cleaned reply.
* :meth:`send_no_reply` — for broadcast commands like ``V`` on the G3CL where no
  reply is expected.

Callers must open the transport themselves (or use ``with protocol.transport: ...``).
Every ``send`` stores the last raw TX and RX bytes on the protocol instance so that
tests and users can inspect them for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass

from group3.exceptions import ProtocolError
from group3.protocol import codec
from group3.protocol.parser import check_error
from group3.transport.base import Transport


@dataclass
class _LastExchange:
    tx: bytes = b""
    rx: bytes = b""


# Default window to wait after a silent-success setter for a deferred error
# reply. At 9600 baud, the longest error strings (e.g. "POSITIVE NUMBER
# REQUIRED", 24 bytes + terminator) need ~26 ms to transmit. 50 ms gives
# comfortable headroom for processing latency without noticeably slowing a
# tight loop of setter calls.
DEFAULT_SETTER_ERROR_WINDOW: float = 0.05


class Group3Protocol:
    """Encoded, terminator-aware request/reply layer over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        terminator: bytes = codec.CR,
        timeout: float | None = None,
    ) -> None:
        """
        Args:
            transport: Underlying byte transport (serial, fake, etc.).
            terminator: Bytes to append to every outgoing command. The DTM-151-S
                requires CR on the host→device direction (manual section 4.5.2).
            timeout: Default per-request timeout in seconds. ``None`` means "use the
                transport's default".
        """
        self.transport = transport
        self.terminator = terminator
        self.timeout = timeout
        self._last = _LastExchange()
        # Accumulates write_only payloads (e.g., G3CL ``An`` prefixes) so that
        # ``last_raw_tx`` after a subsequent ``send`` reflects everything that
        # went on the wire since the previous reply, not just the final command.
        self._pending_tx = bytearray()

    # ------------------------------------------------------------------
    # send helpers
    # ------------------------------------------------------------------

    def send(self, command: str, timeout: float | None = None) -> str:
        """Send ``command`` and return the normalised reply string.

        The reply has its trailing CR/LF stripped (any of CR, LF, CR+LF, LF+CR is
        tolerated per manual section 3.6 DIP-switch options). Device-reported
        errors (manual section 4.5.3) are mapped to :mod:`group3.exceptions`
        classes and raised before returning.

        Args:
            command: The command string, *without* terminator.
            timeout: Per-call timeout override. ``None`` uses ``self.timeout``.

        Returns:
            The reply string, terminator stripped, leading space (if any) preserved.

        Raises:
            CommandError: ``command`` contains non-ASCII characters.
            TransportError: Underlying I/O failed.
            TimeoutError: No reply within the timeout.
            ProtocolError: Reply was not valid ASCII.
            DeviceError: The device reported a named error.
        """
        payload = codec.encode(command, self.terminator)
        effective_timeout = timeout if timeout is not None else self.timeout
        # Compose the full outgoing sequence up-front so debug state is correct
        # regardless of whether transport.request() succeeds, raises, or the
        # reply parses as an error.
        full_tx = bytes(self._pending_tx) + payload
        try:
            raw = self.transport.request(payload, timeout=effective_timeout)
        except BaseException:
            # Record the attempted TX so callers inspecting last_raw_tx after
            # catching the exception can see exactly what went on the wire.
            # Clear _pending_tx so a later successful exchange does not inherit
            # this failed attempt's bytes.
            self._last = _LastExchange(tx=full_tx, rx=b"")
            self._pending_tx.clear()
            raise
        self._pending_tx.clear()
        self._last = _LastExchange(tx=full_tx, rx=raw)
        reply = codec.strip_terminators(codec.decode(raw))
        check_error(reply)
        return reply

    def send_no_reply(self, command: str) -> None:
        """Send ``command`` via :meth:`Transport.write_only` with no read.

        Used for broadcast commands such as ``V`` (trigger) on the G3CL, and for
        the ``An`` address prefix in addressed exchanges. The payload is
        remembered in an internal buffer so that a subsequent :meth:`send`
        reports the full wire history via :attr:`last_raw_tx`.
        """
        payload = codec.encode(command, self.terminator)
        self.transport.write_only(payload)
        self._pending_tx.extend(payload)
        # For callers that only ever send_no_reply (e.g. a pure broadcast
        # trigger), expose the cumulative pending buffer as ``last_raw_tx``
        # with an empty rx until the next ``send()`` flushes it.
        self._last = _LastExchange(tx=bytes(self._pending_tx), rx=b"")

    def send_setter(
        self,
        command: str,
        error_window: float = DEFAULT_SETTER_ERROR_WINDOW,
    ) -> None:
        """Send a setter command that is silent on success (manual §4.5.2).

        DTM-151-S setters (``Z``, ``Rn``, ``GA``, ``GD``, ``Jn``, etc.) do not
        reply on success; they emit an error string from manual §4.5.3 only on
        failure. This method writes the command, then waits up to
        ``error_window`` seconds for a deferred reply. If bytes arrive they are
        treated as an error message and raised via :func:`check_error` — a
        non-error reply raises :class:`ProtocolError` because the protocol is
        now out of sync.

        Args:
            command: Setter command, without terminator.
            error_window: Seconds to wait for a deferred error reply. Default
                50 ms. Set to 0 to skip the drain (fastest, but defers error
                detection to the next read-producing command).

        Raises:
            CommandError: ``command`` is not ASCII.
            TransportError: Underlying I/O failed.
            DeviceError: A deferred error string arrived from the device.
            ProtocolError: A reply arrived that was not a recognised error.
        """
        payload = codec.encode(command, self.terminator)
        self.transport.write_only(payload)
        self._pending_tx.extend(payload)
        full_tx = bytes(self._pending_tx)

        raw = self.transport.read_optional(error_window) if error_window > 0 else b""

        if raw:
            # The device sent something — either a §4.5.3 error string or an
            # unexpected reply. Either way the transmission for this setter is
            # complete; clear pending_tx and record it.
            self._pending_tx.clear()
            self._last = _LastExchange(tx=full_tx, rx=raw)
            reply = codec.strip_terminators(codec.decode(raw))
            check_error(reply)
            # check_error didn't raise — this was an unrecognised reply to a
            # silent-success command, which means we're desynchronised.
            raise ProtocolError(
                f"Unexpected reply to silent setter {command!r}: {reply!r}",
                raw=raw,
            )

        # Silent success. Leave pending_tx populated so a subsequent send()
        # can include this setter's bytes in last_raw_tx, matching the debug
        # semantics of other write-only operations.
        self._last = _LastExchange(tx=full_tx, rx=b"")

    # ------------------------------------------------------------------
    # debug accessors
    # ------------------------------------------------------------------

    @property
    def last_raw_tx(self) -> bytes:
        """Raw bytes of the most recent outgoing sequence.

        For an addressed exchange (``An`` written via ``write_only`` followed by
        a normal ``send``), this returns the concatenation of both so multi-drop
        sessions can be audited end-to-end.
        """
        return self._last.tx

    @property
    def last_raw_rx(self) -> bytes:
        """Raw bytes of the most recent incoming reply, before terminator stripping."""
        return self._last.rx
