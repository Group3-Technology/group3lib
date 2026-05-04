"""pyserial-backed transport.

``pyserial`` is an *optional* dependency (``pip install group3lib[serial]``). We import
it lazily inside methods so that ``from group3 import DTM151Serial`` does not fail on a
fresh install. If ``pyserial`` is missing and :class:`SerialTransport` is constructed,
we raise a clear ``ImportError`` pointing the user at the extras group.

The DTM-151-S accepts configurable serial parameters set by internal DIP switches
(manual section 3.6). ``SerialTransport`` defaults to **9600 baud, 7E2,
no flow control** — the factory default shipped by Group3 Technology
(verified against an Antala bench unit). Override ``bytesize``, ``parity``,
``stopbits`` for non-default DIP-switch settings.

Reply-terminator handling follows manual §3.6: the final terminator is either CR
(S2-2 ON) or LF (S2-2 OFF), optionally preceded by the other character as a
pre-terminator (S2-3 ON). Rather than require the caller to know which DIP-switch
combination their device uses, the default behaviour is to stop at the first CR or
LF seen and then briefly peek for a paired byte — this covers all four documented
configurations (CR, LF, CR+LF, LF+CR) without configuration.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from group3.exceptions import TimeoutError as Group3TimeoutError
from group3.exceptions import TransportError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import serial as _serial_types

_TERMINATOR_BYTES = frozenset((b"\r"[0], b"\n"[0]))
_DEFAULT_PAIR_PEEK_SECONDS = 0.01


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
        except serial.SerialException as exc:
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
                ser.reset_input_buffer()
                # reset_input_buffer wipes the OS buffer; any residue we'd
                # stashed for push-back is now stale and must be dropped too.
                self._pushback = b""
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
