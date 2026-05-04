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

import time
from dataclasses import dataclass

from group3.exceptions import ProtocolError
from group3.exceptions import TimeoutError as Group3TimeoutError
from group3.protocol import codec
from group3.protocol.parser import check_error
from group3.transport.base import Transport


@dataclass
class _LastExchange:
    tx: bytes = b""
    rx: bytes = b""


# Default window to wait after a setter for either a bare-terminator ack
# (empirically what every probed setter sends — see examples/probe_setter_replies.py)
# or a §4.5.3 error string. At 9600 baud the longest error string ("POSITIVE
# NUMBER REQUIRED", 24 bytes + terminator) needs ~26 ms to transmit; 50 ms
# gives comfortable headroom for processing latency without noticeably slowing
# a tight loop of setter calls.
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
        # Single deadline shared by the initial request() and any residue-
        # draining read_reply() calls below, so a caller asking for
        # send(timeout=1.0) sees roughly 1 s of total wall-clock cost — not
        # 2 s when residue happens to be queued.
        deadline = (
            time.monotonic() + effective_timeout
            if effective_timeout is not None
            else None
        )

        def _remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        # Compose the full outgoing sequence up-front so debug state is correct
        # regardless of whether transport.request() succeeds, raises, or the
        # reply parses as an error.
        full_tx = bytes(self._pending_tx) + payload
        try:
            raw = self.transport.request(payload, timeout=_remaining())
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
        if reply == "":
            # Leading terminator residue (stale setter acks from prior
            # send_setter(error_window=0) calls, an undrained An-prefix ack,
            # or a multi-byte terminator that overflowed into a second frame).
            # Drain any number of terminator-only frames until we see real
            # data or the *shared* deadline expires.
            accumulated_rx = bytearray(raw)
            while reply == "":
                remaining = _remaining()
                if remaining is not None and remaining <= 0:
                    raise Group3TimeoutError(
                        f"Timed out after {effective_timeout}s with no real "
                        "reply (only terminator residue arrived)"
                    )
                # read_reply requires a concrete float; with no caller-supplied
                # timeout, fall back to 1 s per drain attempt (same behaviour
                # as the previous fallback constant).
                next_raw = self.transport.read_reply(
                    remaining if remaining is not None else 1.0
                )
                accumulated_rx.extend(next_raw)
                self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))
                reply = codec.strip_terminators(codec.decode(next_raw))
        check_error(reply)
        return reply

    def drain_setter_ack(self, window: float = DEFAULT_SETTER_ERROR_WINDOW) -> None:
        """Synchronously drain a single setter-style ack from the transport.

        Used by :class:`AddressedProtocol` after writing the ``An`` prefix
        via :meth:`send_no_reply`: the prefix command empirically acks with
        a bare terminator (like every other DTM-151-S setter), and if that
        ack is left on the wire it gets read as the start of the next
        command's reply — a desync that returns ``""`` instead of the real
        data.

        Differences from :meth:`send_setter`:

        * No write — ``send_no_reply`` already wrote the command.
        * ``_pending_tx`` is **not** cleared, so the caller's audit trail
          (``last_raw_tx``) still includes the prefix and the next command
          as one combined exchange.
        * The drained bytes update ``last_raw_rx`` for debugging.

        If the drained reply is a §4.5.3 error string the corresponding
        :class:`DeviceError` is raised — the addressed device rejected the
        prefix command. A non-empty, non-error reply raises
        :class:`ProtocolError` because the wire is now out of sync.
        """
        if window <= 0:
            return
        raw = self.transport.read_optional(window)
        if not raw:
            return
        # Update only the rx side; preserve _last.tx and _pending_tx so the
        # audit trail of the surrounding exchange (e.g. ``An\rF\r``) is intact.
        self._last = _LastExchange(tx=self._last.tx, rx=raw)
        reply = codec.strip_terminators(codec.decode(raw))
        if reply == "":
            return  # Bare-terminator ack — the desired path.
        check_error(reply)
        raise ProtocolError(
            f"Unexpected reply during ack drain: {reply!r}",
            raw=raw,
        )

    def drain_pending(self, window: float = 0.05) -> list[bytes]:
        """Drain any in-flight replies from the transport.

        Reads from the transport with a short per-read window until nothing
        arrives. Used around streaming transitions (start/stop/pause/resume)
        to absorb replies that may have been mid-transmission when a mode
        change was issued.

        Returns the raw bytes drained so callers (typically tests) can
        inspect them. In normal operation the return value is discarded.
        """
        drained: list[bytes] = []
        while True:
            chunk = self.transport.read_optional(window)
            if not chunk:
                break
            drained.append(chunk)
        return drained

    def read_next(self, timeout: float) -> str:
        """Read one unsolicited reply from the device (for streaming).

        Used by :class:`DTM151Serial.stream_field` to consume replies the
        device sends autonomously in ``SM1`` mode. This is a pure read — no
        command is written. The terminator is stripped, the reply is checked
        against the §4.5.3 error table, and the cleaned string is returned.

        Tolerates terminator-only frames: if a read returns just terminator
        bytes (e.g. a stale setter ack `\\n` left in the buffer when
        streaming begins), this method discards the empty frame and reads
        again. The total wait still respects ``timeout``.

        Args:
            timeout: Seconds to wait for the next reply. If nothing arrives,
                :class:`TimeoutError` is raised.

        Returns:
            The normalised reply string (terminators stripped, leading space
            from manual §4.5.2 preserved).

        Raises:
            TransportError: Underlying I/O failed.
            TimeoutError: No reply within ``timeout``.
            ProtocolError: Reply was not valid ASCII.
            DeviceError: The device sent a named error string.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Group3TimeoutError(
                    f"Timed out after {timeout}s with no real reply "
                    "(only terminator residue arrived)"
                )
            raw = self.transport.read_reply(remaining)
            # Streaming reads don't carry a "request" payload — _pending_tx
            # and _last.tx are preserved as-is so the last paired TX still
            # reflects the most recent command the caller issued.
            self._last = _LastExchange(tx=self._last.tx, rx=raw)
            reply = codec.strip_terminators(codec.decode(raw))
            if reply == "":
                # Terminator-only residue (e.g. stale setter ack). Skip and
                # read again until we get real data or the timeout expires.
                continue
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
        """Send a setter command that carries no payload on success (manual §4.5.2).

        DTM-151-S setters (``Z``, ``Rn``, ``GA``, ``GD``, ``Jn``, etc.) carry no
        payload on success; they emit an error string from manual §4.5.3 only
        on failure. The manual describes this as "silent on success", but
        empirically the bench device acks every setter with a bare terminator
        (e.g. ``b'\\n'``) — see ``examples/probe_setter_replies.py``. This
        method writes the command, waits up to ``error_window`` seconds for a
        deferred reply, and treats the three observed cases as:

        * No bytes, or a terminator-only frame → success.
        * A §4.5.3 error string → corresponding :class:`DeviceError` raised
          via :func:`check_error`.
        * Any other non-empty reply → :class:`ProtocolError` (desync).

        Args:
            command: Setter command, without terminator.
            error_window: Seconds to wait for a deferred error or ack reply.
                Default 50 ms. Set to 0 to skip the drain entirely — the
                bare-terminator ack is then left on the wire, which on real
                hardware is racy and on :class:`FakeTransport` is a guaranteed
                desync. The next read can return ``""`` (the stale ack) instead
                of real data; :meth:`send` defends against this by reading
                again on a terminator-only frame, but other consumers may not.
                Prefer the default unless you have measured the latency cost.

        Raises:
            CommandError: ``command`` is not ASCII.
            TransportError: Underlying I/O failed.
            DeviceError: A deferred error string arrived from the device.
            ProtocolError: A non-empty, non-error reply arrived.
        """
        payload = codec.encode(command, self.terminator)
        self.transport.write_only(payload)
        self._pending_tx.extend(payload)
        full_tx = bytes(self._pending_tx)

        raw = self.transport.read_optional(error_window) if error_window > 0 else b""

        if raw:
            # The device sent something — either a bare terminator ack
            # (empirically observed on SU1 and likely other setters), a
            # §4.5.3 error string, or an unexpected reply. Either way the
            # transmission for this setter is complete; clear pending_tx
            # and record it.
            self._pending_tx.clear()
            self._last = _LastExchange(tx=full_tx, rx=raw)
            reply = codec.strip_terminators(codec.decode(raw))
            if reply == "":
                # Terminator-only frame carries no payload — manual §4.5.2
                # treats setters as silent on success, so accept this as success.
                return
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
