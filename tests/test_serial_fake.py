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
    TimeoutError,
    TransportError,
)

# --------------------------------------------------------------------------- #
# Golden transcripts — bytes we expect on the wire for the most common commands.
# --------------------------------------------------------------------------- #

SENT_F = b"F\r"
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
