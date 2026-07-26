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
import warnings
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


def _echo_prefix(payload: bytes, terminator: bytes) -> bytes:
    """Return the bytes of ``payload`` that come back before the reply.

    Two unrelated mechanisms put the command back on the wire ahead of its
    reply (see :attr:`Group3Protocol.expect_command_returned`), and the
    prefix differs slightly between them:

    * **S2-4 echo.** Verified on bench unit FT572EW5 (2026-05-09): the
      firmware echoes the printable command body but suppresses the
      host-side CR terminator that triggered command processing.
    * **G3CL loop ripple.** The command is retransmitted around the loop
      byte-for-byte, terminator included (manual §4.5.1, page 4-7).

    Stripping the terminator covers both: the ripple's trailing terminator
    is then handled as ordinary residue by the callers.
    """
    if terminator and payload.endswith(terminator):
        return payload[: -len(terminator)]
    return payload


def _strip_returned_commands(raw: bytes, echo_prefix: bytes, terminator: bytes) -> bytes:
    """Strip every copy of the returned command from the front of ``raw``.

    A command can come back **twice**: manual §3.6 (page 3-11) states that
    with S2-4 echo ON *and* the G3CL ports in use, "the teslameter will
    transmit each input command twice, first the original command rippling
    through, then the echoed command". Stripping a single prefix leaves the
    second copy glued to the reply.

    Greedy stripping is safe because every genuine reply starts with a space
    (manual §4.5.2), so no reply can be mistaken for another returned copy.
    Each copy may or may not carry the terminator with it — the loop ripple
    is byte-for-byte and does, the S2-4 echo suppresses it — so a terminator
    between copies is consumed too.
    """
    if not echo_prefix:
        return raw
    while raw.startswith(echo_prefix):
        raw = raw[len(echo_prefix):]
        if terminator and raw.startswith(terminator):
            raw = raw[len(terminator):]
    return raw


def _command_returned_error(command: str, raw: bytes) -> ProtocolError:
    """Build the diagnostic raised when a reply is just the command, returned.

    This is the signature failure of talking to a device whose command comes
    back before its reply while the protocol is not expecting it. Without a
    named diagnostic the symptom surfaces two layers away, in the parser, as
    an opaque "expected numeric field reply, got 'F'".
    """
    return ProtocolError(
        f"reply is {command!r} — the command just sent, returned verbatim. "
        f"Either the device has echo ON (DIP S2-4, manual §3.6 page 3-11) or "
        f"the host sits on a G3CL loop, where every command ripples back "
        f"before its reply regardless of S2-4 (manual §4.5.1 page 4-7 — this "
        f"is the normal case for a fiber-optic/FTR link). Construct "
        f"Group3Protocol(..., expect_command_returned=True), or call "
        f"DTM151Serial.identify() / detect_command_echo() to probe for it.",
        raw=raw,
    )


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
        expect_command_returned: bool = False,
        echo_enabled: bool | None = None,
    ) -> None:
        """
        Args:
            transport: Underlying byte transport (serial, fake, etc.).
            terminator: Bytes to append to every outgoing command. The DTM-151-S
                requires CR on the host→device direction (manual section 4.5.2).
            timeout: Default per-request timeout in seconds. ``None`` means "use the
                transport's default".
            expect_command_returned: ``True`` if each command comes back on the
                wire before its reply — see
                :attr:`expect_command_returned`. The normal flow is to leave
                this ``False`` and call :meth:`group3.DTM151Serial.identify`
                after connect, which probes for it.
            echo_enabled: Deprecated alias for ``expect_command_returned``. The
                old name described only one of the two causes.
        """
        self.transport = transport
        self.terminator = terminator
        self.timeout = timeout
        if echo_enabled is not None:
            warnings.warn(
                "Group3Protocol(echo_enabled=...) is deprecated; use "
                "expect_command_returned=... — a G3CL loop returns commands "
                "even with echo (S2-4) off.",
                DeprecationWarning,
                stacklevel=2,
            )
            expect_command_returned = echo_enabled
        #: ``True`` when the command sent is returned ahead of its reply. Two
        #: unrelated causes produce this, and the wire looks the same either
        #: way, so one flag covers both:
        #:
        #: * the device echoes commands — DIP S2-4 ON or the ``SE1`` command
        #:   (manual §3.6, page 3-11). Clearable with ``SE0``.
        #: * the host is on a Group3 Communication Loop, where each device
        #:   retransmits the message on around the loop until it arrives back
        #:   at the host (manual §4.5.1, page 4-7). This is topology, **not** a
        #:   setting: ``SE0`` cannot switch it off, and with S2-4 also ON the
        #:   command comes back twice.
        self.expect_command_returned = expect_command_returned
        self._last = _LastExchange()
        # Accumulates write_only payloads (e.g., G3CL ``An`` prefixes) so that
        # ``last_raw_tx`` after a subsequent ``send`` reflects everything that
        # went on the wire since the previous reply, not just the final command.
        self._pending_tx = bytearray()

    @property
    def echo_enabled(self) -> bool:
        """Deprecated alias for :attr:`expect_command_returned`."""
        warnings.warn(
            "Group3Protocol.echo_enabled is deprecated; use "
            "expect_command_returned — a G3CL loop returns commands even with "
            "echo (S2-4) off.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.expect_command_returned

    @echo_enabled.setter
    def echo_enabled(self, value: bool) -> None:
        warnings.warn(
            "Group3Protocol.echo_enabled is deprecated; use "
            "expect_command_returned — a G3CL loop returns commands even with "
            "echo (S2-4) off.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.expect_command_returned = value

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
        accumulated_rx = bytearray(raw)
        self._last = _LastExchange(tx=full_tx, rx=raw)

        def _drain_residue(current: bytes) -> bytes:
            """Discard terminator-only frames until real data arrives.

            Stale terminator residue can come from prior
            ``send_setter(error_window=0)`` calls, an undrained An-prefix
            ack, or a multi-byte terminator that overflowed into a second
            frame. Draining must happen *before* echo-prefix validation —
            otherwise a leftover CR/LF makes the startswith() check raise
            ProtocolError instead of being silently absorbed.
            """
            while codec.strip_terminators(codec.decode(current)) == "":
                remaining = _remaining()
                if remaining is not None and remaining <= 0:
                    raise Group3TimeoutError(
                        f"Timed out after {effective_timeout}s with no real "
                        "reply (only terminator residue arrived)"
                    )
                # read_reply requires a concrete float; with no caller-supplied
                # timeout, fall back to 1 s per drain attempt.
                next_frame = self.transport.read_reply(
                    remaining if remaining is not None else 1.0
                )
                accumulated_rx.extend(next_frame)
                self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))
                current = next_frame
            return current

        raw = _drain_residue(raw)

        if self.expect_command_returned:
            # The command comes back ahead of its reply — echoed by the device
            # (printable body only, terminator suppressed; verified on bench
            # unit FT572EW5 on 2026-05-09) or rippled around the G3CL loop
            # (byte-for-byte, terminator included).
            echo_prefix = _echo_prefix(payload, self.terminator)
            if not raw.startswith(echo_prefix):
                raise ProtocolError(
                    f"command echo expected (expect_command_returned=True) but "
                    f"reply for {command!r} does not start with the echoed payload",
                    raw=bytes(accumulated_rx),
                )
            raw = _strip_returned_commands(raw, echo_prefix, self.terminator)
            while not raw or codec.strip_terminators(codec.decode(raw)) == "":
                # Only returned copies (and their terminators) so far — read
                # on for the reply. A second copy may lead the next frame,
                # since the transport breaks frames at the terminator the
                # loop ripple carries.
                remaining = _remaining()
                raw = self.transport.read_reply(
                    remaining if remaining is not None else 1.0
                )
                accumulated_rx.extend(raw)
                self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))
                raw = _strip_returned_commands(raw, echo_prefix, self.terminator)

        reply = codec.strip_terminators(codec.decode(raw))
        if not self.expect_command_returned and reply == command:
            # Every genuine reply starts with a space (manual §4.5.2), so a
            # reply identical to the command can only be the command coming
            # back at us. Name the cause here rather than letting the parser
            # fail on it downstream.
            raise _command_returned_error(command, bytes(accumulated_rx))
        check_error(reply)
        return reply

    def drain_setter_ack(
        self,
        window: float = DEFAULT_SETTER_ERROR_WINDOW,
        *,
        echo_command: str | None = None,
    ) -> None:
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

        Args:
            window: Seconds to wait for a deferred ack/error reply.
            echo_command: When echo is enabled this is the command string
                whose echo is expected to lead the drained reply (typically
                the ``An`` prefix the caller just sent via
                :meth:`send_no_reply`). Without this, an echoed prefix
                would be misinterpreted as an unexpected reply and raise
                :class:`ProtocolError`.

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
        accumulated = bytearray(raw)
        # Update only the rx side; preserve _last.tx and _pending_tx so the
        # audit trail of the surrounding exchange (e.g. ``An\rF\r``) is intact.
        self._last = _LastExchange(tx=self._last.tx, rx=bytes(accumulated))

        # Strip leading terminator residue from a prior exchange before
        # further validation. A stale CR/LF here would otherwise be misread
        # as the bare-terminator setter ack of *this* drain, masking a
        # genuine echo or error string that arrived right behind it.
        leading = 0
        while leading < len(raw) and raw[leading : leading + 1] in (codec.CR, codec.LF):
            leading += 1
        raw = raw[leading:]

        if self.expect_command_returned and echo_command is not None and raw:
            # The caller just wrote ``echo_command`` via send_no_reply; the
            # device echoed its printable body (terminator suppressed —
            # verified on bench unit FT572EW5). Strip that echo before
            # checking for an error or unexpected reply.
            payload = codec.encode(echo_command, self.terminator)
            echo_prefix = _echo_prefix(payload, self.terminator)
            if not raw.startswith(echo_prefix):
                raise ProtocolError(
                    f"echo expected for {echo_command!r} during ack drain "
                    "but drained bytes do not start with the echoed payload",
                    raw=bytes(accumulated),
                )
            raw = _strip_returned_commands(raw, echo_prefix, self.terminator)
            while not raw:
                # Only returned copies so far — read on for the actual ack
                # within the window. A second copy (S2-4 echo behind the
                # loop ripple) may lead the next frame.
                tail = self.transport.read_optional(window)
                if not tail:
                    break
                accumulated.extend(tail)
                self._last = _LastExchange(tx=self._last.tx, rx=bytes(accumulated))
                raw = _strip_returned_commands(tail, echo_prefix, self.terminator)

        reply = codec.strip_terminators(codec.decode(raw))
        if reply == "":
            return  # Bare-terminator ack — the desired path.
        check_error(reply)
        raise ProtocolError(
            f"Unexpected reply during ack drain: {reply!r}",
            raw=bytes(accumulated),
        )

    def send_unvalidated(
        self, command: str, window: float = DEFAULT_SETTER_ERROR_WINDOW
    ) -> bytes:
        """Write ``command``, absorb everything it produces, and return those bytes.

        A deliberate escape hatch from the usual guarantees, for **repair**
        paths only: no echo-prefix validation, no error checking, no reply
        parsing. It exists because those checks assume a healthy link, and a
        repair runs precisely when the link is not healthy — bench-observed
        on an FTR loop with echo ON (2026-07-26), where the device's echoed
        copy of ``SE0`` came back corrupted as ``b'S\\x05`\\x000'`` and a
        strict check would have aborted the one command that fixes the link.

        The caller decides what the drained bytes mean — typically running
        :func:`~group3.protocol.parser.check_error` over a lenient decode to
        catch an outright rejection while tolerating corruption.

        Unlike :meth:`send_no_reply`, this finalises the exchange: the
        pending-TX buffer is cleared so the command does not reappear in a
        later :attr:`last_raw_tx`.

        Args:
            command: Command string, without terminator.
            window: Per-read wait while draining. Draining stops at the first
                empty read.

        Returns:
            Every byte drained, concatenated, exactly as it arrived.
        """
        payload = codec.encode(command, self.terminator)
        full_tx = bytes(self._pending_tx) + payload
        self.transport.write_only(payload)
        # This *is* the whole exchange — nothing later should inherit it.
        self._pending_tx.clear()
        drained = bytearray()
        while True:
            chunk = self.transport.read_optional(window)
            if not chunk:
                break
            drained.extend(chunk)
        self._last = _LastExchange(tx=full_tx, rx=bytes(drained))
        return bytes(drained)

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

        if error_window <= 0:
            self._last = _LastExchange(tx=full_tx, rx=b"")
            return

        accumulated_rx = bytearray()
        raw = self.transport.read_optional(error_window)
        accumulated_rx.extend(raw)

        # Strip leading terminator residue (stale CR/LF from a prior
        # exchange) so it doesn't fail the echo-prefix check below or get
        # silently consumed as this setter's bare-terminator ack — masking
        # a genuine echo or error string queued behind it.
        leading = 0
        while leading < len(raw) and raw[leading : leading + 1] in (codec.CR, codec.LF):
            leading += 1
        raw = raw[leading:]

        if self.expect_command_returned and raw:
            # The command comes back ahead of the ack (device echo, or G3CL
            # ripple — see _echo_prefix). Strip it, then read again for the
            # actual ack/error if it didn't arrive in the same buffer.
            echo_prefix = _echo_prefix(payload, self.terminator)
            if not raw.startswith(echo_prefix):
                self._pending_tx.clear()
                self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))
                raise ProtocolError(
                    f"command echo expected for setter {command!r} but reply "
                    f"does not start with the echoed payload",
                    raw=bytes(accumulated_rx),
                )
            raw = _strip_returned_commands(raw, echo_prefix, self.terminator)
            while not raw:
                tail = self.transport.read_optional(error_window)
                if not tail:
                    break
                accumulated_rx.extend(tail)
                raw = _strip_returned_commands(tail, echo_prefix, self.terminator)

        if raw:
            # The device sent something — either a bare terminator ack
            # (empirically observed on SU1 and likely other setters), a
            # §4.5.3 error string, or an unexpected reply. Either way the
            # transmission for this setter is complete; clear pending_tx
            # and record it.
            self._pending_tx.clear()
            self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))
            reply = codec.strip_terminators(codec.decode(raw))
            if reply == "":
                # Terminator-only frame carries no payload — manual §4.5.2
                # treats setters as silent on success, so accept this as success.
                return
            if not self.expect_command_returned and reply == command:
                raise _command_returned_error(command, bytes(accumulated_rx))
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
        self._last = _LastExchange(tx=full_tx, rx=bytes(accumulated_rx))

    def send_control(
        self,
        payload: bytes,
        *,
        expect_echo: bool | None = None,
        timeout: float | None = None,
    ) -> str:
        """Send a single control-byte command (e.g. ``Ctrl-D``) and return the reply.

        The vendor command reference lists four control-character
        commands (``\\x02``/``\\x04``/``\\x15``/``\\x18`` in
        :mod:`group3.protocol.commands`) that are sent as raw bytes **without** a
        terminator. The reply still ends with the device's configured terminator.

        Args:
            payload: The control bytes (typically a single byte). Sent verbatim.
            expect_echo: ``True`` to require an echoed prefix, ``False`` to require
                no echo, or ``None`` (default) to auto-detect from the reply prefix.
                When ``None`` the protocol's :attr:`expect_command_returned` flag
                is updated from the detection result — useful as the very first
                probe at connect time, when it is not yet known whether the wire
                returns commands.
            timeout: Per-call timeout. ``None`` uses :attr:`timeout`.

        Returns:
            The reply string, terminator stripped, echo prefix stripped if present.

        Raises:
            ProtocolError: ``expect_echo=True`` but the reply does not start with
                ``payload``; or the reply is not valid ASCII.
            DeviceError: The device returned a §4.5.3 named-error string.
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        deadline = (
            time.monotonic() + effective_timeout
            if effective_timeout is not None
            else None
        )

        def _remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        try:
            raw = self.transport.request(payload, timeout=_remaining())
        except BaseException:
            self._last = _LastExchange(tx=payload, rx=b"")
            raise
        accumulated_rx = bytearray(raw)
        self._last = _LastExchange(tx=payload, rx=raw)

        # Detect or enforce echoed prefix. Control-byte commands have no
        # terminator, so the echo prefix is the full payload.
        echo_prefix = _echo_prefix(payload, self.terminator)
        if expect_echo is True:
            echo_present = raw.startswith(echo_prefix)
        elif expect_echo is False:
            echo_present = False
        else:
            echo_present = raw.startswith(echo_prefix)
            self.expect_command_returned = echo_present

        if expect_echo is True and not echo_present:
            raise ProtocolError(
                f"echo expected for control payload {payload!r} but reply "
                f"does not start with the echoed payload",
                raw=bytes(accumulated_rx),
            )

        if echo_present:
            # A control byte has no terminator, so a double return (S2-4 echo
            # on top of the G3CL ripple — manual §3.6, page 3-11) arrives as
            # two adjacent copies in the same frame. Strip every copy, then
            # read on if that consumed the whole frame.
            raw = _strip_returned_commands(raw, echo_prefix, self.terminator)
            while not raw:
                remaining = _remaining()
                next_raw = self.transport.read_reply(
                    remaining if remaining is not None else 1.0
                )
                accumulated_rx.extend(next_raw)
                self._last = _LastExchange(tx=payload, rx=bytes(accumulated_rx))
                raw = _strip_returned_commands(next_raw, echo_prefix, self.terminator)

        reply = codec.strip_terminators(codec.decode(raw))
        check_error(reply)
        return reply

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
