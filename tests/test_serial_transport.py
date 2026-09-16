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

import math
import sys
import threading
import time
import types
from typing import Any

import pytest

from group3 import SerialTransport, TimeoutError, TransportError
from group3.transport import serial as serial_transport


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
        # Every assignment to .timeout, with how many bytes had been
        # written at the time. request() sets it *before* writing, which is
        # safe and is why getters were unaffected while every setter failed
        # on the same connection.
        self.timeout_writes: list[tuple[float | None, int]] = []

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self.timeout_writes.append((value, len(self.written)))
        self._timeout = value

    def queue_rx(self, data: bytes) -> None:
        self._rx_queue.extend(data)

    @property
    def in_waiting(self) -> int:
        """Bytes readable without blocking, as ``serial.Serial`` reports them.

        The double did without this for a long time because its only caller,
        ``_drain_leading_terminators``, wraps the access in a bare ``except``
        and treats the failure as "nothing buffered" — so every test using
        the fake silently exercised that error path.
        """
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

    # Character times from first principles: one start bit, the data bits, a
    # parity bit unless parity is disabled, then the stop bits. Written out
    # per framing rather than recomputed from the implementation's formula -
    # a test that mirrors the code it checks cannot catch a wrong formula,
    # and getting this 9% low is enough to bring the corruption back.
    # time.sleep() never returns early, so the measured drain can only be
    # >= the computed one. The floor is 0.99 purely for clock granularity,
    # not slack: an earlier 0.9 was wide enough to swallow the exact defect
    # these tests exist to catch, since dropping 11 bits/char to 10 leaves
    # 90.9% of the correct drain.
    _SLEEP_FLOOR = 0.99

    FRAMINGS = [
        (dict(bytesize=7, parity="E", stopbits=2), 11),  # DTM-151-S default
        (dict(bytesize=8, parity="N", stopbits=1), 10),
        (dict(bytesize=8, parity="N", stopbits=2), 11),
        (dict(bytesize=7, parity="N", stopbits=1), 9),
    ]

    @staticmethod
    def _elapsed_write(t: SerialTransport, payload: bytes) -> float:
        """Time a write with ``perf_counter``, not ``monotonic``.

        These drains are single-digit milliseconds. On Windows under Python
        3.10-3.12 ``monotonic`` is ``GetTickCount64`` at 15.6 ms resolution
        (CPython moved it to ``QueryPerformanceCounter`` only in 3.13,
        gh-88494), so a 5.67 ms drain can measure as 0.0 ms and fail a test
        that is working perfectly. CI is Linux, so it would be invisible
        there and would only bite on the Windows bench these tests exist
        for.
        """
        started = time.perf_counter()
        t.write_only(payload)
        return time.perf_counter() - started

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
        wire needs is the property; sleeping longer is the scheduler.
        """
        t = _transport(timeout=1.0, baudrate=9600, bytesize=7, parity="E", stopbits=2)
        t.open()

        elapsed = self._elapsed_write(t, b"R2\r")

        expected = (3 + 2) * 11 / 9600
        assert elapsed >= expected * self._SLEEP_FLOOR, (
            f"write_only returned after {elapsed * 1000:.2f} ms; the payload "
            f"needs {expected * 1000:.2f} ms to clock out at 7E2/9600. "
            "Reconfiguring the port this early corrupts the command."
        )

    def test_the_drain_scales_with_the_payload(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """A longer command takes proportionally longer to leave the UART."""
        t = _transport(timeout=1.0, baudrate=9600, bytesize=7, parity="E", stopbits=2)
        t.open()

        elapsed = self._elapsed_write(t, b"A" * 40 + b"\r")

        expected = (41 + 2) * 11 / 9600
        assert elapsed >= expected * self._SLEEP_FLOOR, (
            f"a 41-character payload drained in {elapsed * 1000:.2f} ms, less "
            f"than the {expected * 1000:.2f} ms it takes on the wire"
        )

    def test_the_drain_scales_with_baud(self, fake_serial: _FakeSerialModule) -> None:
        """A slower wire needs proportionally longer."""
        t = _transport(timeout=1.0, baudrate=1200, bytesize=7, parity="E", stopbits=2)
        t.open()

        elapsed = self._elapsed_write(t, b"R2\r")

        expected = (3 + 2) * 11 / 1200
        assert elapsed >= expected * self._SLEEP_FLOOR, (
            f"at 1200 baud the drain took {elapsed * 1000:.2f} ms, less than "
            f"the {expected * 1000:.2f} ms the wire needs"
        )

    @pytest.mark.parametrize("framing,bits", FRAMINGS)
    def test_the_drain_matches_the_framings_character_time(
        self,
        fake_serial: _FakeSerialModule,
        framing: dict[str, Any],
        bits: int,
    ) -> None:
        """Each S2 DIP framing has its own character time, and the drain has
        to track it.

        7E2 is 11 bits and 8N1 is 10, so a drain that assumes one framing
        under-waits the other by 9% - measured on the bench, an *exact* drain
        was already scoring 1/60, so 9% short brings the corruption back.
        """
        t = _transport(timeout=1.0, baudrate=1200, **framing)
        t.open()

        elapsed = self._elapsed_write(t, b"R2\r")

        expected = (3 + 2) * bits / 1200
        assert elapsed >= expected * self._SLEEP_FLOOR, (
            f"{framing} is {bits} bits/char; the drain took "
            f"{elapsed * 1000:.2f} ms against the {expected * 1000:.2f} ms "
            "the wire needs"
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


class TestTrailingTerminatorPeek:
    """The peek after a reply's first terminator byte.

    It has to hold two things at once: pick up a paired terminator that is
    still in flight, and not spend a fixed slab of wall-clock doing so. The
    second is why this drain polls ``in_waiting`` instead of handing the wait
    to ``ser.timeout`` - on Windows a comm timeout is rounded up to the
    system tick (15.625 ms), so a 10 ms window cost 15.9 ms on *every*
    request and more than doubled a 22 ms exchange.
    """

    def test_captures_a_terminator_still_in_flight(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """A byte that has not arrived yet must still be drained.

        The guard this replaces could not fail: the fake returns an empty
        read the instant its queue runs dry, so no test ever made the peek
        *wait* for anything, and an implementation that gave up at once
        passed the whole suite.
        """
        t = _transport(pair_peek_timeout=0.2)
        t.open()
        ser = fake_serial.last_instance
        assert ser is not None
        ser.queue_rx(b" 1.03G\n")

        # The rest of the terminator lands 5 ms later, as it would at 9600
        # baud behind an FTDI latency timer.
        threading.Timer(0.005, lambda: ser.queue_rx(b"\r\n")).start()

        assert t.read_optional(timeout=0.5) == b" 1.03G\n\r\n"

    def test_budget_is_per_byte_not_for_the_whole_drain(
        self, fake_serial: _FakeSerialModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each terminator byte gets a fresh window, as it did before.

        The implementation this replaces re-entered a blocking read with a
        fresh ``pair_peek_timeout`` per byte. Collapsing that to one deadline
        for the whole drain looks equivalent and is not: a three-byte
        terminator delivered in separate FTDI batches, each gap inside the
        window but the sum outside it, gets truncated and strands a byte that
        the next exchange reads as an empty reply.

        Driven by a controlled clock rather than real time. The obvious
        version of this test used ``threading.Timer`` and was flaky 4 runs in
        5 - ``Timer`` waits on ``Event.wait``, which on Windows is rounded up
        to the 15.625 ms system tick, so a 20 ms timer fires anywhere up to
        35 ms and lands outside the window it was meant to sit inside. That
        is the same OS behaviour this whole change is about.
        """
        window = 0.030
        clock = {"t": 0.0}
        reveal = {0.020: b"\r", 0.040: b"\n"}

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return clock["t"]

            @staticmethod
            def sleep(seconds: float) -> None:
                clock["t"] += max(seconds, 0.001)
                for at, data in sorted(reveal.items()):
                    if clock["t"] >= at and data is not None:
                        ser.queue_rx(data)
                        reveal[at] = None  # type: ignore[assignment]

        monkeypatch.setattr(serial_transport, "time", _Clock)

        t = _transport(pair_peek_timeout=window)
        t.open()
        ser = fake_serial.last_instance
        assert ser is not None
        ser.queue_rx(b" 1.03G\n")

        # Gaps of 20 ms: each inside a 30 ms window, together outside it.
        assert t.read_optional(timeout=0.5) == b" 1.03G\n\r\n"

    def test_gives_up_after_the_window_not_a_multiple_of_it(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """A terminator that never arrives costs one window, not one tick.

        The fake is made to honour ``ser.timeout`` the way a Windows comm
        timeout does — rounded up to the 15.625 ms system tick — because
        otherwise this test's upper bound is decorative: the plain fake never
        blocks, so a revert to OS-timeout waiting would trip the *lower*
        bound and the upper one could not fail for the regression it names.
        """
        window = 0.05
        tick = 0.015625
        t = _transport(pair_peek_timeout=window)
        t.open()
        ser = fake_serial.last_instance
        assert ser is not None

        real_read = ser.read

        def windows_read(n: int = 1) -> bytes:
            if not ser.in_waiting and ser.timeout:
                # What WaitForSingleObject does to a sub-tick comm timeout.
                time.sleep(math.ceil(ser.timeout / tick) * tick)
            return real_read(n)

        ser.read = windows_read  # type: ignore[method-assign]
        ser.queue_rx(b" 1.03G\n")

        start = time.perf_counter()
        assert t.read_optional(timeout=0.5) == b" 1.03G\n"
        elapsed = time.perf_counter() - start

        # Lower bound: it really did wait, rather than returning at once.
        assert elapsed >= window * 0.9, f"returned after {elapsed * 1000:.1f} ms"
        # Upper bound, stated as the thing it has to beat rather than as a
        # round multiple: blocking on a comm timeout costs the window rounded
        # up to a whole tick, 62.5 ms for a 50 ms window. A ratio picked by
        # eye (1.6x = 80 ms) sits *above* that and so could not fail for the
        # regression it names.
        tick_rounded = math.ceil(window / tick) * tick
        assert elapsed < tick_rounded * 0.95, (
            f"took {elapsed * 1000:.1f} ms; tick-rounded waiting would cost "
            f"{tick_rounded * 1000:.1f} ms"
        )

    def test_never_reconfigures_the_port_mid_exchange(
        self, fake_serial: _FakeSerialModule
    ) -> None:
        """The drain must not assign ``ser.timeout``.

        Two reasons, either sufficient. It is what makes the peek cost
        sub-tick on Windows. And every assignment is a ``SetCommTimeouts``
        on a port with a reply in flight, which is the exact hazard 0.4.2
        exists to remove - see ``_await_drain``.
        """
        t = _transport(pair_peek_timeout=0.05)
        t.open()
        ser = fake_serial.last_instance
        assert ser is not None
        ser.queue_rx(b" 1.03G\n\r\n")

        before = len(ser.timeout_writes)
        assert t.request(b"F\r", timeout=0.5) == b" 1.03G\n\r\n"

        # request() itself legitimately sets the timeout before writing and
        # restores it afterwards. What must not appear is a third and fourth
        # assignment from inside the drain.
        assert len(ser.timeout_writes) - before <= 2, (
            f"drain reconfigured the port: {ser.timeout_writes[before:]}"
        )


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
