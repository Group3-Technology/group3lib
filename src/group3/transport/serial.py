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

#: Extra character times added to a write's computed drain, in
#: :meth:`SerialTransport._await_drain`. An exact drain sat on the edge on
#: the bench — 1/60 failures on one run, 0/60 on the next — so two
#: characters (~2 ms at 9600 baud) buys margin for the UART shift register
#: and driver jitter at negligible cost.
_DRAIN_MARGIN_CHARS = 2

#: How often ``_drain_trailing_terminators`` re-checks ``in_waiting`` while
#: waiting out its peek window. Well under one character time at 9600 baud
#: (1.146 ms at 7E2), so a byte in flight is picked up promptly, and short
#: enough that the wait is bounded by the window rather than by this. Uses
#: ``time.sleep``, which on Windows has used a high-resolution waitable timer
#: since Python 3.11 and so is *not* subject to the system tick that makes a
#: blocking read round up.
_PEEK_POLL_SECONDS = 0.0005


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

        The peek window is honoured by polling ``in_waiting`` rather than by
        setting ``ser.timeout`` and letting ``read`` block. On Windows a comm
        timeout is enforced at the system timer tick — 15.625 ms by default —
        so the OS rounds *any* sub-tick timeout up to a full tick. Measured on
        a DTM-351-S at 9600 7E2, that turned the 10 ms window into 15.9 ms of
        dead waiting **on every request**, more than doubling a 22 ms
        exchange. Polling costs the same on POSIX, where the timeout was
        already honoured precisely, and removes two ``ser.timeout``
        assignments per request — each one a ``SetCommTimeouts`` call that
        reconfigures the port mid-exchange, which is the hazard 0.4.2 exists
        to avoid.

        The window itself is deliberately unchanged. It is sized against the
        FTDI latency timer, not the baud rate: with the 16 ms Windows default
        the trailing bytes of a reply can be delivered a full latency period
        after the first, and a window shorter than that would end the drain
        early and leave terminator bytes to corrupt the next reply.
        """
        if self._pair_peek_timeout <= 0:
            return
        deadline = time.monotonic() + self._pair_peek_timeout
        while True:
            if not self._pushback and not self._buffered(ser):
                # Nothing to read yet. Wait for it ourselves rather than
                # handing the wait to the OS — see _wait_for_byte.
                if time.monotonic() >= deadline:
                    return
                time.sleep(_PEEK_POLL_SECONDS)
                continue
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

    def _buffered(self, ser: _serial_types.Serial) -> int:
        """Bytes already in the OS buffer, or 0 if that cannot be asked.

        Defensive in the same way as ``_drain_leading_terminators``: a
        transport that cannot report ``in_waiting`` degrades to "nothing
        buffered", which makes the peek time out rather than misbehave.
        """
        try:
            return int(ser.in_waiting)
        except Exception:  # pragma: no cover - defensive
            return 0

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
        self._await_drain(len(payload))

    def _await_drain(self, char_count: int) -> None:
        """Wait for a just-written payload to finish clocking out of the UART.

        Callers that write and then read — every setter, and the streaming
        start — configure the read timeout *after* the write. On Windows that
        assignment is a port reconfiguration (pyserial's
        ``_reconfigure_port`` → ``SetCommTimeouts``), and performing one while
        the payload is still in the adapter's transmit FIFO corrupts it on the
        wire: the instrument receives garbage and answers ``INVALID COMMAND
        ENTRY`` or ``PARITY ERROR`` to a command that left byte-perfect.

        ``flush()`` is not enough. It returns once the OS buffer is handed to
        the driver, which is not the same as the bytes having been shifted
        out of an FTDI adapter's own FIFO — which is why this was not caught
        by the flush that has always been here.

        Waiting here rather than in the read paths is deliberate. The window
        belongs to the write, so closing it at the write leaves ``request``,
        ``read_reply`` and ``read_optional`` exactly as they were — including
        their timeout accounting and their blocking reads, which a polling
        approach in those paths would have cost.

        Bench-measured on Windows over an FT4232H at 9600 7E2, ``R2`` sixty
        times per configuration:

        * reconfigure straight after the write .... 25/60 and 22/60 failed
        * drain first, then reconfigure ........... 0/60 and 0/60

        The margin is two character times. An exact drain measured 1/60 on
        one run and 0/60 on another, i.e. right on the edge; two characters
        costs ~2 ms at 9600 baud and takes it off the edge.
        """
        seconds = (char_count + _DRAIN_MARGIN_CHARS) * self._seconds_per_char()
        if seconds > 0:
            time.sleep(seconds)

    def _seconds_per_char(self) -> float:
        """Wire time for one character at the configured framing.

        One start bit, the data bits, a parity bit when parity is enabled,
        and the stop bits. For the DTM-151-S default of 7E2 that is
        ``1 + 7 + 1 + 2 = 11`` bits, or ~1.15 ms at 9600 baud.

        Computed rather than assumed, because the S2 DIP switches can select
        other framings and each has its own character time: 8N1 is 10 bits,
        8N2 is 11, 7N1 is 9. Under-drain a slower framing and the corruption
        this exists to prevent comes back.
        """
        bits = 1 + self._bytesize + (0 if self._parity == "N" else 1) + self._stopbits
        return bits / float(self._baudrate)

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
        """
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        ser = self._require_open()
        original_timeout = ser.timeout
        try:
            self._drain_leading_terminators(ser)
            ser.timeout = timeout
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
        finally:
            ser.timeout = original_timeout

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
        """
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        ser = self._require_open()
        original_timeout = ser.timeout
        try:
            self._drain_leading_terminators(ser)
            ser.timeout = timeout
            try:
                first = self._read_byte(ser)
            except Exception as exc:
                raise TransportError(f"Failed to read from serial port: {exc}") from exc
            if not first:
                return b""
            # Give the device room to finish sending — use the transport's
            # default timeout for the remainder of the message.
            ser.timeout = self._timeout
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
        finally:
            ser.timeout = original_timeout

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
