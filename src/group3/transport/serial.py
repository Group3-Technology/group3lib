"""pyserial-backed transport.

``pyserial`` is an *optional* dependency (``pip install group3lib[serial]``). We import
it lazily inside methods so that ``from group3 import DTM151Serial`` does not fail on a
fresh install. If ``pyserial`` is missing and :class:`SerialTransport` is constructed,
we raise a clear ``ImportError`` pointing the user at the extras group.

The DTM-151-S accepts configurable serial parameters set by internal DIP switches
(manual section 3.6). ``SerialTransport`` defaults to **9600 baud, 7E2,
no flow control** — the factory default shipped by Group3 Technology
(verified against a bench unit). Override ``bytesize``, ``parity``,
``stopbits`` for non-default DIP-switch settings.

Reply-terminator handling follows manual §3.6: the final terminator is either CR
(S2-2 ON) or LF (S2-2 OFF), optionally preceded by the other character as a
pre-terminator (S2-3 ON). Rather than require the caller to know which DIP-switch
combination their device uses, the default behaviour is to stop at the first CR or
LF seen and then briefly peek for a paired byte — this covers all four documented
configurations (CR, LF, CR+LF, LF+CR) without configuration.
"""

from __future__ import annotations

import contextlib
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from group3.exceptions import TimeoutError as Group3TimeoutError
from group3.exceptions import TransportError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import serial as _serial_types

_TERMINATOR_BYTES = frozenset((b"\r"[0], b"\n"[0]))
_DEFAULT_PAIR_PEEK_SECONDS = 0.01

#: How long :meth:`SerialTransport._wait_for_first_byte` sleeps between
#: polls. Half a character time at the slowest supported rate (a 7E2
#: character is ~1.15 ms at 9600 baud), so waiting costs at most a fraction
#: of a character of latency while keeping the poll off the CPU.
_FIRST_BYTE_POLL_SECONDS = 0.0005


class SerialTransport:
    """Transport that talks to a DTM-151-S over RS-232 or fiber-optic-to-RS-232.

    Args:
        port: Serial port identifier (e.g., ``/dev/cu.usbserial-1``, ``COM3``).
        baudrate: Baud rate. Must match the bit-rate switch (manual §3.7).
        bytesize, parity, stopbits: Framing parameters. Default to 7E2 to
            match the DTM-151-S factory shipping configuration. Override for
            units with non-default S2 DIP-switch settings.
        timeout: Per-read timeout in seconds. Applied to :meth:`request` unless
            overridden on the call.
        rtscts, xonxoff: Flow-control toggles. DTM-151-S does not use flow
            control by default.
        terminators: Iterable of single bytes that end a reply. Default
            ``(b"\\r", b"\\n")`` accepts any of the four configurations documented
            in manual §3.6 (CR, LF, CR+LF, LF+CR).
        pair_peek_timeout: After seeing a terminator byte, wait this long for a
            possible paired byte (covers CR+LF and LF+CR). Default 10 ms. Set to
            0 to disable peeking (treat only a single byte as the terminator).
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 7,
        parity: str = "E",
        stopbits: float = 2,
        timeout: float = 1.0,
        rtscts: bool = False,
        xonxoff: bool = False,
        terminators: tuple[bytes, ...] = (b"\r", b"\n"),
        pair_peek_timeout: float = _DEFAULT_PAIR_PEEK_SECONDS,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout = timeout
        self._rtscts = rtscts
        self._xonxoff = xonxoff
        term_bytes: set[int] = set()
        for t in terminators:
            if len(t) != 1 or t[0] not in _TERMINATOR_BYTES:
                raise ValueError(
                    f"terminator entries must be single-byte CR (b'\\r') or LF "
                    f"(b'\\n'); got {t!r}"
                )
            term_bytes.add(t[0])
        if not term_bytes:
            raise ValueError("terminators must not be empty")
        self._terminator_bytes = frozenset(term_bytes)
        if pair_peek_timeout < 0:
            raise ValueError("pair_peek_timeout must be >= 0")
        self._pair_peek_timeout = pair_peek_timeout
        self._ser: Any = None
        # One-byte pushback buffer used by _drain_*_terminators when they
        # accidentally consume a non-terminator byte while draining residue.
        # Pyserial has no native pushback; we re-serve from _pushback before
        # touching the OS buffer.
        self._pushback: bytes = b""

    # ------------------------------------------------------------------
    # open / close
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._ser is not None and getattr(self._ser, "is_open", False):
            return
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - exercised by users without the extra
            raise ImportError(
                "pyserial is required for SerialTransport. "
                "Install the extra: pip install 'group3lib[serial]'"
            ) from exc

        try:
            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._bytesize,
                parity=self._parity,
                stopbits=self._stopbits,
                timeout=self._timeout,
                rtscts=self._rtscts,
                xonxoff=self._xonxoff,
            )
            # A port opens onto whatever the device emitted while nobody was
            # listening — power-on banners, an S2-1 free-running stream, the
            # tail of a previous session. Those bytes belong to no exchange.
            self._ser.reset_input_buffer()
        except serial.SerialException as exc:
            # The flush runs after construction, so self._ser may hold a live
            # port whose is_open is True. Left there, the next open() returns
            # on it immediately and hands back a port that was never flushed.
            if self._ser is not None:
                with contextlib.suppress(Exception):
                    self._ser.close()
                self._ser = None
            raise TransportError(f"Failed to open serial port {self._port!r}: {exc}") from exc

    def close(self) -> None:
        if self._ser is None:
            return
        try:
            self._ser.close()
        except Exception as exc:  # pragma: no cover - defensive
            raise TransportError(f"Error closing serial port: {exc}") from exc
        finally:
            self._ser = None
            self._pushback = b""

    # ------------------------------------------------------------------
    # read / write
    # ------------------------------------------------------------------

    def _require_open(self) -> _serial_types.Serial:
        if self._ser is None:
            raise TransportError("Serial port is not open. Call open() first.")
        return self._ser

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        ser = self._require_open()
        original_timeout = ser.timeout
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            ser.timeout = effective_timeout
            try:
                self.reset_input()
                ser.write(payload)
                ser.flush()
            except Exception as exc:
                raise TransportError(f"Failed to write to serial port: {exc}") from exc

            buf = bytearray()
            deadline = time.monotonic() + effective_timeout
            term_bytes = self._terminator_bytes
            while time.monotonic() < deadline:
                chunk = self._read_byte(ser)
                if not chunk:
                    # read returned empty because of the per-byte timeout; loop
                    # to the deadline so callers get a consistent TimeoutError.
                    continue
                buf.extend(chunk)
                if chunk[0] in term_bytes:
                    self._drain_trailing_terminators(ser, buf)
                    return bytes(buf)
            if buf:
                raise Group3TimeoutError(
                    f"Timed out after {effective_timeout}s waiting for terminator "
                    f"(got partial reply: {bytes(buf)!r})"
                )
            raise Group3TimeoutError(
                f"Timed out after {effective_timeout}s with no reply"
            )
        finally:
            ser.timeout = original_timeout

    def _read_byte(self, ser: _serial_types.Serial) -> bytes:
        """Read one byte, draining ``_pushback`` first.

        Pyserial has no push-back primitive, so the drain helpers stash any
        accidentally-consumed non-terminator byte in ``self._pushback`` and
        re-serve it here before touching the OS buffer.
        """
        if self._pushback:
            byte, self._pushback = self._pushback[:1], self._pushback[1:]
            return byte
        result: bytes = ser.read(1)
        return result

    def _drain_trailing_terminators(
        self, ser: _serial_types.Serial, buf: bytearray
    ) -> None:
        """After the first terminator byte, drain ALL consecutive terminator bytes.

        Manual §3.6 documents 1- or 2-byte terminator combinations (CR, LF,
        CR+LF, LF+CR), but real DTM-151-S firmware on some DIP-switch
        configurations emits 3 bytes (e.g. ``\\n\\r\\n``) for request-reply
        responses while sending only ``\\n`` for setter acks and ``\\n\\r``
        for streaming readings. Rather than encode each combination, drain
        all consecutive CR/LF bytes within the peek window. A non-terminator
        byte stops the drain and is push-back'd for the next read.

        Assigning ``ser.timeout`` here is safe, unlike in the paths that wait
        for a *first* byte — see :meth:`_wait_for_first_byte`. This runs only
        after a terminator has already been received, and a reply proves the
        device took the command, so no bytes of ours remain in the transmit
        FIFO for a reconfiguration to corrupt.
        """
        if self._pair_peek_timeout <= 0:
            return
        saved = ser.timeout
        ser.timeout = self._pair_peek_timeout
        try:
            while True:
                peek = self._read_byte(ser)
                if not peek:
                    return
                if peek[0] in self._terminator_bytes:
                    buf.extend(peek)
                    continue
                # Non-terminator byte — out of the terminator zone. Restore
                # via pushback so the next read sees it.
                self._pushback = peek + self._pushback
                return
        finally:
            ser.timeout = saved

    def _wait_for_first_byte(self, ser: _serial_types.Serial, timeout: float) -> bool:
        """Wait up to ``timeout`` for a byte to be readable. No reconfiguration.

        This exists to hold one invariant, which every read path in this
        class depends on:

            **The port is never reconfigured between a write and the first
            reply byte.**

        The obvious implementation — ``ser.timeout = timeout`` then a
        one-byte read — breaks it. On Windows, assigning ``ser.timeout`` is a
        port reconfiguration (pyserial's ``_reconfigure_port`` →
        ``SetCommTimeouts``), and the read paths are entered immediately
        after a write, while the command is still in the adapter's transmit
        FIFO. The reconfiguration corrupts the bytes going out, so the
        instrument answers ``INVALID COMMAND ENTRY`` or ``PARITY ERROR`` to a
        command that left byte-perfect.

        Reconfiguring *after* the first reply byte is safe, and the invariant
        is worded that way deliberately: a reply proves the device received
        the command, so nothing of ours is still in flight. That is why
        :meth:`request` may set the timeout before its write, and why
        :meth:`_drain_trailing_terminators` may set it once a terminator has
        already arrived.

        Polling costs a little CPU for the length of the wait and touches
        nothing on the port. The sleep is 0.5 ms — under half the ~1.15 ms a
        7E2 character takes to clock out at 9600 baud, so the poll cannot
        miss the arrival window by more than a fraction of a character — and
        it is clamped to the remaining time so a short ``timeout`` is not
        overrun by the sleep itself.
        """
        if self._pushback:
            return True
        deadline = time.monotonic() + timeout
        while True:
            try:
                if ser.in_waiting:
                    return True
            except Exception as exc:
                raise TransportError(f"Failed to read from serial port: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_FIRST_BYTE_POLL_SECONDS, remaining))

    def _drain_leading_terminators(self, ser: _serial_types.Serial) -> None:
        """Discard any leading terminator bytes from pushback + OS buffer.

        Cleans up residue left behind by a prior request whose terminator
        was longer than this transport happened to consume. Only drains
        bytes that are *already* buffered (no blocking) — does not wait for
        bytes that might or might not arrive.
        """
        # Drop terminator bytes from the front of the pushback buffer.
        while self._pushback and self._pushback[0] in self._terminator_bytes:
            self._pushback = self._pushback[1:]
        if self._pushback:
            return  # First pushback byte is data — leave it for the read.
        # Drain only what's already in the OS buffer. ser.in_waiting tells
        # us how many bytes are buffered without blocking.
        try:
            available = int(ser.in_waiting)
        except Exception:  # pragma: no cover - defensive
            return
        while available > 0:
            byte = ser.read(1)
            available -= 1
            if not byte:
                return
            if byte[0] not in self._terminator_bytes:
                # Restore non-terminator for the read to consume.
                self._pushback = byte
                return

    def reset_input(self) -> None:
        """Discard buffered input, including our own push-back byte.

        ``reset_input_buffer`` wipes the OS buffer only; the push-back byte
        lives in this object and would otherwise survive as the first byte
        of the next read.
        """
        ser = self._require_open()
        try:
            ser.reset_input_buffer()
        except Exception as exc:
            raise TransportError(f"Failed to reset serial input buffer: {exc}") from exc
        self._pushback = b""

    def write_only(self, payload: bytes) -> None:
        ser = self._require_open()
        try:
            ser.write(payload)
            ser.flush()
        except Exception as exc:
            raise TransportError(f"Failed to write to serial port: {exc}") from exc

    def read_reply(self, timeout: float) -> bytes:
        """Block up to ``timeout`` s for a full reply; raise if none arrives.

        Semantics match the read-half of :meth:`request`: reads bytes until
        the device sends its terminator. If the timeout elapses before the
        first byte (no data at all), we raise :class:`TimeoutError`
        immediately. If bytes have started arriving but no terminator is
        seen within the window, we also raise — returning a partial reply
        to a streaming consumer would desync the stream.

        Drains any leading terminator residue from the buffer first, so a
        prior request whose terminator sequence was longer than expected
        does not corrupt this read.

        Waits for the first byte by polling rather than by assigning
        ``ser.timeout`` — see :meth:`_wait_for_first_byte` for why. This path
        is entered directly after a write on the streaming start
        (``_start_auto_transmit`` sends ``SM1`` write-only, then the first
        streaming read lands here) and on the G3CL ``An`` address prefix, so
        it sits inside the window where a reconfiguration corrupts the
        command that has just gone out.
        """
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        ser = self._require_open()
        self._drain_leading_terminators(ser)
        if not self._wait_for_first_byte(ser, timeout):
            raise Group3TimeoutError(
                f"Timed out after {timeout}s with no reply"
            )
        buf = bytearray()
        deadline = time.monotonic() + timeout
        term_bytes = self._terminator_bytes
        while time.monotonic() < deadline:
            try:
                chunk = self._read_byte(ser)
            except Exception as exc:
                raise TransportError(
                    f"Failed to read from serial port: {exc}"
                ) from exc
            if not chunk:
                continue
            buf.extend(chunk)
            if chunk[0] in term_bytes:
                self._drain_trailing_terminators(ser, buf)
                return bytes(buf)
        if buf:
            raise Group3TimeoutError(
                f"Timed out after {timeout}s waiting for terminator "
                f"(got partial reply: {bytes(buf)!r})"
            )
        raise Group3TimeoutError(
            f"Timed out after {timeout}s with no reply"
        )

    def read_optional(self, timeout: float) -> bytes:
        """Read a reply if one arrives within ``timeout`` seconds.

        Returns ``b""`` immediately if the first byte does not arrive within
        ``timeout``. Once the first byte is received, reads continue to the
        usual terminator-detection logic using the transport's default timeout
        (so partial replies don't get truncated just because the error window
        was short).

        Drains any leading terminator residue from the buffer first, so a
        prior reply whose terminator was longer than expected does not get
        served back here as an empty frame.

        **Never reconfigures the port before the first reply byte.** On
        Windows, assigning ``ser.timeout`` is a port reconfiguration
        (pyserial's ``_reconfigure_port`` → ``SetCommTimeouts``), and this
        method is called immediately after ``write_only`` on the setter path.
        Reconfiguring while the command is still in the adapter's transmit
        FIFO corrupts it on the wire: the instrument receives garbage and
        answers ``INVALID COMMAND ENTRY`` or ``PARITY ERROR``.

        Once a reply byte has arrived the window is closed — the device
        answered, so nothing of ours is still going out — which is why
        ``_drain_trailing_terminators`` may still set the timeout for its
        pair-peek further down this method.

        Bench-measured on Windows against a DTM-351-S over an FT4232H,
        ``R2`` 60 times per configuration:

        * write, settle, read ....................... 0/60 failed
        * write, ``ser.timeout = x``, settle, read .. 10/60 and 17/60 failed

        That ordering is the only reason setters failed where getters did
        not: :meth:`request` assigns ``ser.timeout`` *before* its write,
        this path assigned it after. It made ``SU1``, ``Ufc`` and ``Rn``
        fail 20-90% of the time on Windows while every getter stayed clean,
        and macOS was unaffected because termios does not reconfigure this
        way.

        The first-byte wait is therefore done by polling ``in_waiting``
        against a deadline. Subsequent reads use the port's standing
        timeout, which :meth:`open` already set to ``self._timeout``.
        """
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        ser = self._require_open()
        self._drain_leading_terminators(ser)
        if not self._wait_for_first_byte(ser, timeout):
            return b""
        try:
            first = self._read_byte(ser)
        except Exception as exc:
            raise TransportError(f"Failed to read from serial port: {exc}") from exc
        if not first:
            return b""
        buf = bytearray(first)
        if first[0] in self._terminator_bytes:
            self._drain_trailing_terminators(ser, buf)
            return bytes(buf)
        deadline = time.monotonic() + self._timeout
        term_bytes = self._terminator_bytes
        while time.monotonic() < deadline:
            try:
                chunk = self._read_byte(ser)
            except Exception as exc:
                raise TransportError(
                    f"Failed to read from serial port: {exc}"
                ) from exc
            if not chunk:
                continue
            buf.extend(chunk)
            if chunk[0] in term_bytes:
                self._drain_trailing_terminators(ser, buf)
                return bytes(buf)
        # Partial reply but no terminator — return what we have. The
        # protocol layer will likely raise ProtocolError.
        return bytes(buf)

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> SerialTransport:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
