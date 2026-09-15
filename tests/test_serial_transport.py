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
import time
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
        self._timeout: float | None = kwargs.get("timeout")
        self.closed = False
        self.input_buffer_resets = 0
        self.bytes_read = 0
        self._in_waiting_polls = 0
        self._rx_available_after = 0
        # Every assignment to .timeout, as (value, bytes_written_so_far,
        # bytes_read_so_far). On Windows each assignment is a port
        # reconfiguration (pyserial's _reconfigure_port -> SetCommTimeouts),
        # and the hazard is specific to the window between a write and the
        # first reply byte - so both counts are needed to tell a safe
        # reconfiguration from a corrupting one.
        self.timeout_writes: list[tuple[float | None, int, int]] = []

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self.timeout_writes.append((value, len(self.written), self.bytes_read))
        self._timeout = value

    def queue_rx(self, data: bytes) -> None:
        self._rx_queue.extend(data)

    def reply_after_polls(self, polls: int) -> None:
        """Model a device that does not answer instantly.

        Without this the queued reply is readable the moment the transport
        looks, so ``_drain_leading_terminators`` consumes a byte before the
        first-byte wait even begins - and a guard checking "was the port
        reconfigured before any reply arrived" is satisfied vacuously. Real
        hardware takes milliseconds to answer, which is precisely the window
        the reconfiguration corrupts.
        """
        self._rx_available_after = polls

    @property
    def in_waiting(self) -> int:
        """Bytes readable without blocking, as ``serial.Serial`` reports them.

        The double did without this for a long time because the only caller,
        ``_drain_leading_terminators``, wrapped the access in a bare
        ``except`` and treated the failure as "nothing buffered" — so the
        fake silently exercised the error path in every test. ``read_optional``
        now polls it to wait for the first byte without reconfiguring the
        port, and a double that models the real API is what makes those tests
        mean anything.
        """
        self._in_waiting_polls += 1
        if self._in_waiting_polls <= self._rx_available_after:
            return 0
        return len(self._rx_queue)

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
        self.bytes_read += len(take)
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

    def test_failed_open_flush_does_not_leave_a_live_port(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """A port whose opening flush failed must not look open.

        The flush runs after construction, so without cleanup ``self._ser``
        keeps a live instance whose ``is_open`` is True — and the next
        ``open()`` returns straight away on that, handing back a port that
        was never flushed and whose failure was already reported.
        """
        failures = {"count": 1}

        def _flaky_reset(self: _FakeSerial) -> None:
            if failures["count"] > 0:
                failures["count"] -= 1
                raise fake_serial.SerialException("device disconnected")
            self.input_buffer_resets += 1

        monkey = _FakeSerial.reset_input_buffer
        _FakeSerial.reset_input_buffer = _flaky_reset  # type: ignore[method-assign]
        try:
            t = _transport()
            with pytest.raises(TransportError, match="device disconnected"):
                t.open()
            first = fake_serial.last_instance
            assert first is not None and first.closed

            # The retry must construct a fresh port and flush it, not return
            # the half-initialised one.
            t.open()
            assert fake_serial.last_instance is not None
            assert fake_serial.last_instance is not first
            assert fake_serial.last_instance.input_buffer_resets == 1
        finally:
            _FakeSerial.reset_input_buffer = monkey  # type: ignore[method-assign]

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


class TestReadReply:
    """SerialTransport.read_reply — blocking read used by streaming consumers."""

    def test_full_reply_returned(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 0.10T\r")
        assert t.read_reply(timeout=0.1) == b" 0.10T\r"

    def test_no_data_raises_timeout(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(timeout=0.05)
        t.open()
        with pytest.raises(TimeoutError, match="no reply"):
            t.read_reply(timeout=0.05)

    def test_partial_reply_raises_timeout(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport(timeout=0.05)
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b" 0.10")  # no terminator
        with pytest.raises(TimeoutError, match="partial reply"):
            t.read_reply(timeout=0.05)

    def test_rejects_negative_timeout(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        with pytest.raises(ValueError):
            t.read_reply(timeout=-0.1)


class TestReadOptional:
    """Cover the drain path used by Group3Protocol.send_setter()."""

    def test_write_only_waits_for_the_payload_to_clock_out(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """Regression: every setter failed 20-90% of the time on Windows.

        Callers that write and then read configure the read timeout *after*
        the write. On Windows that assignment reconfigures the port
        (pyserial's ``_reconfigure_port`` -> ``SetCommTimeouts``), and doing
        it while the payload is still in the adapter's transmit FIFO corrupts
        it: the instrument answers ``INVALID COMMAND ENTRY`` or ``PARITY
        ERROR`` to a command that left byte-perfect.

        ``flush()`` does not cover this - it returns once the driver has the
        bytes, not once the UART has shifted them out - which is why the
        flush that has always been here did not prevent it.

        Bench-measured over an FT4232H at 9600 7E2, ``R2`` sixty times:
        reconfiguring straight after the write scored 25/60 and 22/60
        failures; draining first scored 0/60 twice.

        Only the lower bound is asserted. Sleeping at least as long as the
        wire needs is the property; sleeping a little longer is the scheduler
        and not a defect.
        """
        t = _transport(timeout=1.0, baudrate=9600, bytesize=7, parity="E", stopbits=2)
        t.open()

        started = time.monotonic()
        t.write_only(b"R2\r")
        elapsed = time.monotonic() - started

        # 7E2 = 10 bits/char at 9600 baud; 3 payload chars + the margin.
        expected = (3 + 2) * (10 / 9600)
        assert elapsed >= expected * 0.9, (
            f"write_only returned after {elapsed * 1000:.2f} ms; the payload "
            f"needs {expected * 1000:.2f} ms to clock out. Reconfiguring the "
            "port this early corrupts the command."
        )

    def test_the_drain_scales_with_the_payload(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """A longer command takes proportionally longer to leave the UART."""
        t = _transport(timeout=1.0, baudrate=9600, bytesize=7, parity="E", stopbits=2)
        t.open()

        started = time.monotonic()
        t.write_only(b"A" * 40 + b"\r")
        long_write = time.monotonic() - started

        expected = (41 + 2) * (10 / 9600)
        assert long_write >= expected * 0.9, (
            f"a 41-character payload drained in {long_write * 1000:.2f} ms, "
            f"less than the {expected * 1000:.2f} ms it takes on the wire"
        )

    def test_the_drain_scales_with_baud_and_framing(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """Slower wires need longer; 8N1 is 10 bits, 7E2 is also 10.

        Pinning the computation rather than a constant, so a transport opened
        for a non-default DIP configuration still waits the right time.
        """
        slow = _transport(timeout=1.0, baudrate=1200, bytesize=7, parity="E", stopbits=2)
        slow.open()
        started = time.monotonic()
        slow.write_only(b"R2\r")
        elapsed = time.monotonic() - started

        expected = (3 + 2) * (10 / 1200)
        assert elapsed >= expected * 0.9, (
            f"at 1200 baud the drain took {elapsed * 1000:.2f} ms, less than "
            f"the {expected * 1000:.2f} ms the wire needs"
        )

    def test_request_may_reconfigure_because_it_does_so_before_writing(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """The invariant is about ordering, not about never touching timeout.

        ``request`` sets the timeout *before* its write, which is safe and is
        exactly why getters were unaffected while every setter failed on the
        same connection. Pinning it so a future change does not "fix" it into
        the broken order.
        """
        t = _transport(timeout=1.0)
        t.open()
        ser = t._ser
        ser.queue_rx(b" 2\n\r\n")
        ser.timeout_writes.clear()

        t.request(b"IR\r")

        assert ser.timeout_writes, "request is expected to set a timeout"
        first = ser.timeout_writes[0]
        assert first[1] == 0, (
            f"request reconfigured after writing {first[1]} bytes; it must do "
            "so before the write"
        )

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
    def test_open_resets_buffer(self, fake_serial: _FakeSerialModule) -> None:
        """A port opens onto bytes that belong to no exchange — drop them."""
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        assert fake_serial.last_instance.input_buffer_resets == 1

    def test_request_writes_payload_and_resets_buffer(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        resets_after_open = fake_serial.last_instance.input_buffer_resets
        fake_serial.last_instance.queue_rx(b" 0\r")
        t.request(b"IR\r")
        assert bytes(fake_serial.last_instance.written) == b"IR\r"
        assert fake_serial.last_instance.input_buffer_resets == resets_after_open + 1

    def test_write_only_does_not_read(self, fake_serial: _FakeSerialModule) -> None:
        t = _transport()
        t.open()
        assert fake_serial.last_instance is not None
        fake_serial.last_instance.queue_rx(b"lingering-data-should-not-be-read\r")
        t.write_only(b"V\r")
        assert bytes(fake_serial.last_instance.written) == b"V\r"
        # Queue is untouched.
        assert bytes(fake_serial.last_instance._rx_queue).startswith(b"lingering")
