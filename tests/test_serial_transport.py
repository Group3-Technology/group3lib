"""SerialTransport tests using a mock ``serial.Serial``.

These exercise the request/reply loop, terminator handling for all four
DIP-switch-documented configurations (CR, LF, CR+LF, LF+CR), timeout behaviour,
and error wrapping — without opening a real port.

We inject a fake serial.Serial class into ``sys.modules`` so ``import serial``
inside SerialTransport.open() finds it. This is coupled to the import
structure of ``group3.transport.serial``, but it's the only way to test the
pyserial path without pyserial semantics leaking into the tests.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from group3 import SerialTransport, TimeoutError, TransportError


class _FakeSerial:
    """Minimal stand-in for ``serial.Serial`` covering the methods we call."""

    is_open = True

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        # Bytes the transport has written to us.
        self.written: bytearray = bytearray()
        # Bytes we'll hand back, one byte per read(1).
        self._rx_queue: bytearray = bytearray()
        self.timeout: float | None = kwargs.get("timeout")
        self.closed = False
        self.input_buffer_resets = 0

    def queue_rx(self, data: bytes) -> None:
        self._rx_queue.extend(data)

    def reset_input_buffer(self) -> None:
        # Real pyserial clears pending OS-buffered bytes. In tests we queue the
        # device's *future* response, so this counter records the call for
        # assertions but we don't actually drain the queue.
        self.input_buffer_resets += 1

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        if not self._rx_queue:
            return b""
        take = self._rx_queue[:n]
        del self._rx_queue[:n]
        return bytes(take)

    def close(self) -> None:
        self.closed = True
        self.is_open = False


class _FakeSerialModule(types.ModuleType):
    """A fake ``serial`` module with the subset SerialTransport uses."""

    SerialException: type[Exception]
    Serial: Any

    def __init__(self) -> None:
        super().__init__("serial")
        self.SerialException = type("SerialException", (Exception,), {})
        self.last_instance: _FakeSerial | None = None

        def _factory(**kwargs: Any) -> _FakeSerial:
            inst = _FakeSerial(**kwargs)
            self.last_instance = inst
            return inst

        self.Serial = _factory


@pytest.fixture
def fake_serial(monkeypatch: pytest.MonkeyPatch) -> _FakeSerialModule:
    """Install a fake ``serial`` module for the duration of the test."""
    mod = _FakeSerialModule()
    monkeypatch.setitem(sys.modules, "serial", mod)
    return mod


def _transport(**kwargs: Any) -> SerialTransport:
    kwargs.setdefault("port", "COM_FAKE")
    kwargs.setdefault("timeout", 1.0)
    return SerialTransport(**kwargs)


class TestOpenClose:
    def test_open_passes_parameters_to_pyserial(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(baudrate=19200, parity="E", stopbits=2, rtscts=True)
        t.open()
        assert fake_serial.last_instance is not None
        assert fake_serial.last_instance.kwargs["baudrate"] == 19200
        assert fake_serial.last_instance.kwargs["parity"] == "E"
        assert fake_serial.last_instance.kwargs["stopbits"] == 2
        assert fake_serial.last_instance.kwargs["rtscts"] is True
        t.close()
        assert fake_serial.last_instance.closed

    def test_open_wraps_serial_exception_in_transport_error(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        def _raise(**_kwargs: Any) -> _FakeSerial:
            raise fake_serial.SerialException("could not open port")

        fake_serial.Serial = _raise
        t = _transport()
        with pytest.raises(TransportError, match="could not open port"):
            t.open()

    def test_context_manager(self, fake_serial: _FakeSerialModule) -> None:
        with _transport():
            assert fake_serial.last_instance is not None
            assert not fake_serial.last_instance.closed
        assert fake_serial.last_instance is not None
        assert fake_serial.last_instance.closed

    def test_request_without_open_raises(self, fake_serial: _FakeSerialModule) -> None:
        # fake_serial fixture still installed, but we never open.
        t = _transport()
        with pytest.raises(TransportError, match="not open"):
            t.request(b"F\r")


class TestTerminatorHandling:
    """Exercise manual §3.6 DIP-switch options: CR, LF, CR+LF, LF+CR."""

    def test_cr_terminator(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 1.2345T\r")
        reply = t.request(b"F\r")
        assert reply == b" 1.2345T\r"

    def test_lf_terminator(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 1.2345T\n")
        reply = t.request(b"F\r")
        assert reply == b" 1.2345T\n"

    def test_crlf_terminator(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(pair_peek_timeout=0.05)
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 1.2345T\r\n")
        reply = t.request(b"F\r")
        assert reply == b" 1.2345T\r\n"

    def test_lfcr_terminator(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(pair_peek_timeout=0.05)
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 1.2345T\n\r")
        reply = t.request(b"F\r")
        assert reply == b" 1.2345T\n\r"

    def test_pair_peek_disabled_returns_single_terminator(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        t = _transport(pair_peek_timeout=0.0)
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 1.2T\r\n")
        reply = t.request(b"F\r")
        # With peeking disabled we stop at CR; LF is left for the next request
        # (which will reset_input_buffer).
        assert reply == b" 1.2T\r"

    def test_rejects_invalid_terminator(self) -> None:
        with pytest.raises(ValueError, match="CR.*LF"):
            SerialTransport(port="X", terminators=(b"X",))

    def test_rejects_multi_byte_terminator(self) -> None:
        with pytest.raises(ValueError, match="single-byte"):
            SerialTransport(port="X", terminators=(b"\r\n",))

    def test_rejects_empty_terminator_tuple(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            SerialTransport(port="X", terminators=())


class TestTimeout:
    def test_timeout_with_no_reply_raises(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(timeout=0.05)
        t.open()
        # No bytes queued — read() always returns b"".
        with pytest.raises(TimeoutError, match="no reply"):
            t.request(b"F\r")

    def test_timeout_with_partial_reply_preserves_it(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        t = _transport(timeout=0.05)
        t.open()
        assert fake_serial.last_instance is not None
        # Queue bytes without a terminator so we never complete.
        fake_serial.last_instance.queue_rx(b" 1.23")
        with pytest.raises(TimeoutError, match="partial reply.*1\\.23"):
            t.request(b"F\r")


class TestReadOptional:
    """Cover the drain path used by Group3Protocol.send_setter()."""

    def test_no_bytes_returns_empty_within_timeout(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        t = _transport(timeout=1.0)
        t.open()
        # Nothing queued — first-byte read returns b"" within the drain window.
        assert t.read_optional(timeout=0.01) == b""

    def test_full_reply_returned(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" NO PROBE\r")
        assert t.read_optional(timeout=0.1) == b" NO PROBE\r"

    def test_crlf_reply_captured_with_peek(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(pair_peek_timeout=0.05)
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" OVERFLOW\r\n")
        assert t.read_optional(timeout=0.1) == b" OVERFLOW\r\n"

    def test_rejects_negative_timeout(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        with pytest.raises(ValueError):
            t.read_optional(timeout=-0.1)


class TestRequestMechanics:
    def test_request_writes_payload_and_resets_buffer(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 0\r")
        t.request(b"IR\r")
        assert bytes(fake_serial.last_instance.written) == b"IR\r"
        assert fake_serial.last_instance.input_buffer_resets == 1

    def test_write_only_does_not_read(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b"lingering-data-should-not-be-read\r")
        t.write_only(b"V\r")
        assert bytes(fake_serial.last_instance.written) == b"V\r"
        # Queue is untouched.
        assert bytes(fake_serial.last_instance._rx_queue).startswith(b"lingering")
