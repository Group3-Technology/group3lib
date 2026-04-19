"""pyserial-backed transport.

``pyserial`` is an *optional* dependency (``pip install group3lib[serial]``). We import
it lazily inside methods so that ``from group3 import DTM151Serial`` does not fail on a
fresh install. If ``pyserial`` is missing and :class:`SerialTransport` is constructed,
we raise a clear ``ImportError`` pointing the user at the extras group.

The DTM-151-S accepts configurable serial parameters set by internal DIP switches
(manual section 3.6). Defaults of **9600 baud, 8N1, no flow control** match the
factory default shipped by Group3 Technology, but callers should confirm against the
physical switch positions on their unit.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from group3.exceptions import TimeoutError as Group3TimeoutError
from group3.exceptions import TransportError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import serial as _serial_types


class SerialTransport:
    """Transport that talks to a DTM-151-S over RS-232 or fiber-optic-to-RS-232.

    Terminator handling is delegated to :class:`~group3.protocol.core.Group3Protocol`:
    this transport's :meth:`request` reads bytes until it either (a) sees the
    configured ``read_terminator`` byte sequence, or (b) the read times out.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        timeout: float = 1.0,
        rtscts: bool = False,
        xonxoff: bool = False,
        read_terminator: bytes = b"\r",
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout = timeout
        self._rtscts = rtscts
        self._xonxoff = xonxoff
        self._read_terminator = read_terminator
        self._ser: Any = None

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
        try:
            if timeout is not None:
                ser.timeout = timeout
            try:
                ser.reset_input_buffer()
                ser.write(payload)
                ser.flush()
            except Exception as exc:
                raise TransportError(f"Failed to write to serial port: {exc}") from exc

            buf = bytearray()
            deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
            term = self._read_terminator
            while time.monotonic() < deadline:
                chunk = ser.read(1)
                if not chunk:
                    # read returned empty because of the per-byte timeout; loop to
                    # deadline so callers get a consistent TimeoutError.
                    continue
                buf.extend(chunk)
                if buf.endswith(term):
                    return bytes(buf)
            if buf:
                raise Group3TimeoutError(
                    f"Timed out after {timeout or self._timeout}s waiting for terminator "
                    f"(got partial reply: {bytes(buf)!r})"
                )
            raise Group3TimeoutError(
                f"Timed out after {timeout or self._timeout}s with no reply"
            )
        finally:
            ser.timeout = original_timeout

    def write_only(self, payload: bytes) -> None:
        ser = self._require_open()
        try:
            ser.write(payload)
            ser.flush()
        except Exception as exc:
            raise TransportError(f"Failed to write to serial port: {exc}") from exc

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
