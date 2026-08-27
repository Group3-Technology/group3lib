"""Tests for FakeTransport + Group3Protocol wiring.

These tests focus on the boundary between the codec/parser and the transport — they
ensure the exact bytes we send over the wire match Table 9 of the DTM-151-S manual.
"""

from __future__ import annotations

import pytest

from group3 import (
    DeviceOverflowError,
    FakeTransport,
    Group3Protocol,
    NoProbeError,
    ProtocolError,
    TimeoutError,
    TransportError,
)

# --------------------------------------------------------------------------- #
# Golden transcripts — bytes we expect on the wire for the most common commands.
# --------------------------------------------------------------------------- #

SENT_F = b"F\r"
SENT_SU1 = b"SU1\r"
SENT_P = b"P\r"
SENT_IR = b"IR\r"
SENT_R2 = b"R2\r"
SENT_Z = b"Z\r"
SENT_EZ = b"EZ\r"
SENT_GA = b"GA\r"
SENT_GD = b"GD\r"
SENT_GC = b"GC\r"
SENT_GV = b"GV\r"
SENT_V = b"V\r"
SENT_D0 = b"D0\r"
SENT_D1 = b"D1\r"


class TestSendAndReceive:
    def test_send_returns_stripped_reply(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" 1.2345T\r")
        p = Group3Protocol(fake)
        assert p.send("F") == " 1.2345T"
        assert fake.sent == [SENT_F]

    def test_send_stores_raw_bytes(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" 0.12345T\r\n")
        p = Group3Protocol(fake)
        p.send("F")
        assert p.last_raw_tx == SENT_F
        assert p.last_raw_rx == b" 0.12345T\r\n"

    def test_send_no_reply_does_not_dequeue(self) -> None:
        """send_no_reply writes bytes but does not consume from the reply queue."""
        fake = FakeTransport()
        fake.open()
        # Pre-queue a reply intended for a later send; send_no_reply must leave it alone.
        fake.queue_reply(b" 1.2T\r")
        p = Group3Protocol(fake)
        p.send_no_reply("V")
        assert fake.sent == [SENT_V]
        # The reply we queued before the broadcast is still there for the next send.
        assert p.send("F") == " 1.2T"

    def test_scripted_exchange(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.scripted(
            [
                (SENT_R2, b" \r"),
                (SENT_F, b" 1.2345T\r"),
            ]
        )
        p = Group3Protocol(fake)
        p.send("R2")
        assert p.send("F") == " 1.2345T"

    def test_scripted_mismatch_asserts(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.scripted([(SENT_F, b" 0\r")])
        p = Group3Protocol(fake)
        with pytest.raises(AssertionError):
            p.send("P")  # would send P\r, scripted expected F\r


class TestErrorReplies:
    """Verify device-level error strings are raised, not silently returned."""

    def test_no_probe_raises(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" NO PROBE\r")
        p = Group3Protocol(fake)
        with pytest.raises(NoProbeError):
            p.send("F")

    def test_overflow_raises(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" OVERFLOW\r")
        p = Group3Protocol(fake)
        with pytest.raises(DeviceOverflowError):
            p.send("F")


# Left on the wire by an earlier exchange: a late reply, an undrained ack, or
# an S2-1 stream that was running before we connected. Issue #6.
STALE_FRAME = b" INVALID COMMAND ENTRY\n\r"
ACK_BARE_LF = b"\n"


class TestStaleInputBeforeWrite:
    """A frame buffered before the write belongs to no exchange (issue #6).

    ``request`` has always flushed before writing, so getters self-healed
    while setters — which write via ``write_only`` and then read — inherited
    whatever was pending and read it as their own reply. Reported against
    v0.3.0 over an FTDI FT4232H at 9600 7E2, where it failed ``SU1`` on most
    connections while ``IR`` and ``F`` were unaffected on the same link.
    """

    def test_setter_discards_stale_frame(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_stale(STALE_FRAME)
        fake.queue_reply(ACK_BARE_LF)
        p = Group3Protocol(fake)
        p.send_setter("SU1")
        assert fake.sent == [SENT_SU1]
        assert p.last_raw_rx == ACK_BARE_LF

    def test_getter_discards_stale_frame(self) -> None:
        """The half that already worked — pinned so the fix stays symmetric."""
        fake = FakeTransport()
        fake.open()
        fake.queue_stale(STALE_FRAME)
        fake.queue_reply(b" 3\r")
        p = Group3Protocol(fake)
        assert p.send("IR") == " 3"
        assert fake.sent == [SENT_IR]

    def test_stale_frame_does_not_mask_a_real_setter_error(self) -> None:
        """Flushing must not swallow the error the setter itself provokes."""
        fake = FakeTransport()
        fake.open()
        fake.queue_stale(STALE_FRAME)
        fake.queue_reply(b" NO PROBE\r")
        p = Group3Protocol(fake)
        with pytest.raises(NoProbeError):
            p.send_setter("Z")

    def test_send_unvalidated_discards_stale_frame(self) -> None:
        """The repair path runs when the link is dirty — it must flush too."""
        fake = FakeTransport()
        fake.open()
        fake.queue_stale(STALE_FRAME)
        fake.queue_reply(ACK_BARE_LF)
        p = Group3Protocol(fake)
        assert p.send_unvalidated("SE0") == ACK_BARE_LF

    def test_write_only_preserves_the_reply_stream(self) -> None:
        """``send_no_reply`` has no read half, so it must not flush.

        ``FieldStream.paused`` sends ``SM0`` this way precisely so that an
        in-flight reading survives to be drained deliberately.
        """
        fake = FakeTransport()
        fake.open()
        fake.queue_stale(b" 1.2345T\n\r")
        p = Group3Protocol(fake)
        p.send_no_reply("SM0")
        assert p.drain_pending() == [b" 1.2345T\n\r"]


class TestSendSetter:
    """send_setter covers the 'silent on success, error string on failure' contract."""

    def test_silent_success_does_not_raise(self) -> None:
        fake = FakeTransport()
        fake.open()
        p = Group3Protocol(fake)
        p.send_setter("Z")
        assert fake.sent == [SENT_Z]
        # No data was read, no error raised.

    def test_deferred_error_surfaces(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" NO PROBE\r")
        p = Group3Protocol(fake)
        with pytest.raises(NoProbeError):
            p.send_setter("Z")

    def test_unexpected_reply_raises_protocol_error(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" surprise\r")
        p = Group3Protocol(fake)
        with pytest.raises(ProtocolError, match="Unexpected reply"):
            p.send_setter("Z")

    @pytest.mark.parametrize("terminator", [b"\r", b"\n", b"\r\n", b"\n\r"])
    def test_bare_terminator_treated_as_success(self, terminator: bytes) -> None:
        # Hardware (e.g. SU1) may ack a setter with just a terminator.
        # check_error has nothing to chew on, and the protocol layer must
        # accept this rather than raising ProtocolError.
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(terminator)
        p = Group3Protocol(fake)
        p.send_setter("Z")
        assert fake.sent == [SENT_Z]
        assert p.last_raw_rx == terminator

    def test_error_window_zero_skips_drain(self) -> None:
        fake = FakeTransport()
        fake.open()
        # Queue an error — but with error_window=0 it won't be read.
        fake.queue_reply(b" NO PROBE\r")
        p = Group3Protocol(fake)
        p.send_setter("Z", error_window=0)
        assert fake.sent == [SENT_Z]
        # The error reply is still in the queue; caller could pick it up later.
        assert list(fake._replies) == [b" NO PROBE\r"]

    def test_error_window_zero_then_send_recovers_from_stale_ack(self) -> None:
        """Regression: send_setter(error_window=0) leaves the bare-LF ack on
        the wire. The next send() must read past that residue and return the
        real reply rather than ``""``.
        """
        fake = FakeTransport()
        fake.open()
        # Stale ack from the prior setter, then F's actual reply.
        fake.queue_reply(b"\n")
        fake.queue_reply(b" 1.234T\r")
        p = Group3Protocol(fake)
        p.send_setter("Z", error_window=0)
        # Without the leading-residue tolerance in send(), this returned ""
        # because the stale ack was consumed as F's reply.
        assert p.send("F") == " 1.234T"

    def test_send_skips_multiple_terminator_only_frames(self) -> None:
        """Regression: send() must drain *any number* of stale terminator
        frames before returning. The original tolerance only skipped one,
        so two queued ``b"\\n"`` acks before the real reply still produced
        ``""``.
        """
        fake = FakeTransport()
        fake.open()
        # Two stale acks from prior setters, then the real F reply.
        fake.queue_reply(b"\n")
        fake.queue_reply(b"\n")
        fake.queue_reply(b" 1.234T\r")
        p = Group3Protocol(fake)
        p.send_setter("Z", error_window=0)
        p.send_setter("EZ", error_window=0)
        assert p.send("F") == " 1.234T"

    def test_send_residue_drain_shares_deadline_with_request(self) -> None:
        """Regression: send(timeout=T) must total ~T wall-clock time, not 2T.

        Before the shared-deadline fix, the residue-draining read_reply()
        loop started a fresh ``deadline = now + effective_timeout`` after
        request() had already consumed up to the full budget — so a stale
        ack followed by a slow real reply could blow past the caller's
        timeout by ~100%.

        Each transport call sleeps long enough that, *if* the budget were
        being shared, the next call would visibly receive less time.
        With the bug, every call sees a fresh ``timeout`` near the full
        budget; with the fix, the recorded timeouts strictly decrease.
        """
        import time as _time

        from group3.exceptions import TransportError

        sleep_per_call = 0.05  # 50 ms — large enough to dwarf scheduling jitter
        budget = 1.0

        class _SleepingTransport:
            """Returns frames after a small sleep, recording each timeout."""

            def __init__(self, frames: list[bytes]) -> None:
                self._frames = list(frames)
                self.timeouts: list[float | None] = []

            def open(self) -> None: ...
            def close(self) -> None: ...

            def request(
                self, payload: bytes, timeout: float | None = None
            ) -> bytes:
                self.timeouts.append(timeout)
                _time.sleep(sleep_per_call)
                if not self._frames:
                    raise TransportError("no frames queued")
                return self._frames.pop(0)

            def write_only(self, payload: bytes) -> None:
                raise NotImplementedError

            def read_reply(self, timeout: float) -> bytes:
                self.timeouts.append(timeout)
                _time.sleep(sleep_per_call)
                if not self._frames:
                    raise TransportError("no frames queued")
                return self._frames.pop(0)

            def read_optional(self, timeout: float) -> bytes:
                return b""

            def __enter__(self) -> _SleepingTransport:
                return self

            def __exit__(self, *exc: object) -> None: ...

        rec = _SleepingTransport([b"\n", b"\n", b" 1.234T\r"])
        p = Group3Protocol(rec)  # type: ignore[arg-type]
        wall_start = _time.monotonic()
        assert p.send("F", timeout=budget) == " 1.234T"
        wall_elapsed = _time.monotonic() - wall_start
        assert len(rec.timeouts) == 3, rec.timeouts

        # Each successive call must receive at least sleep_per_call less
        # budget than the previous — i.e. the deadline is shared, not reset.
        for i in range(1, len(rec.timeouts)):
            prev, nxt = rec.timeouts[i - 1], rec.timeouts[i]
            assert prev is not None and nxt is not None, rec.timeouts
            assert nxt < prev - sleep_per_call / 2, (
                f"timeout did not shrink between calls (deadline reset?): "
                f"{rec.timeouts!r}"
            )
        # Total wall-clock cost is ~3 * sleep_per_call = 0.15 s, comfortably
        # under the 1 s budget — the budget would only matter if the bug
        # caused us to wait longer than necessary.
        assert wall_elapsed < budget


class TestReadReply:
    """FakeTransport.read_reply: pops queued reply or raises TimeoutError."""

    def test_pops_queued_reply(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" 1.2T\r")
        assert fake.read_reply(timeout=0.1) == b" 1.2T\r"

    def test_empty_queue_raises_timeout(self) -> None:
        fake = FakeTransport()
        fake.open()
        with pytest.raises(TimeoutError, match="no reply queued"):
            fake.read_reply(timeout=0.1)

    def test_closed_transport_raises_transport_error(self) -> None:
        fake = FakeTransport()
        with pytest.raises(TransportError):
            fake.read_reply(timeout=0.1)


class TestReadOptional:
    def test_empty_queue_returns_empty_bytes(self) -> None:
        fake = FakeTransport()
        fake.open()
        assert fake.read_optional(0.01) == b""

    def test_pops_queued_reply(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" data\r")
        assert fake.read_optional(0.01) == b" data\r"
        assert fake.read_optional(0.01) == b""

    def test_raises_queued_error(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_error(TransportError("boom"))
        with pytest.raises(TransportError):
            fake.read_optional(0.01)


class TestTransportErrors:
    def test_transport_timeout_surfaces(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_error(TimeoutError("boom"))
        p = Group3Protocol(fake)
        with pytest.raises(TimeoutError):
            p.send("F")

    def test_closed_transport_refuses_send(self) -> None:
        fake = FakeTransport()  # not opened
        p = Group3Protocol(fake)
        with pytest.raises(TransportError):
            p.send("F")

    def test_context_manager_opens_and_closes(self) -> None:
        with FakeTransport() as fake:
            fake.queue_reply(b" 0\r")
            p = Group3Protocol(fake)
            p.send("IR")
        # after close, further sends should fail
        with pytest.raises(TransportError):
            p.send("IR")
