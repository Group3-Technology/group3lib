"""G3CL addressing and broadcast-trigger tests."""

from __future__ import annotations

import pytest

from group3 import (
    CommandError,
    DTM151Serial,
    FakeTransport,
    G3CLSession,
    Group3Protocol,
)

SENT_A0 = b"A0\r"
SENT_A5 = b"A5\r"
SENT_A30 = b"A30\r"
SENT_F = b"F\r"
SENT_V = b"V\r"
SENT_R2 = b"R2\r"


class TestAddressedProtocol:
    def test_addressed_send_prefixes_address(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" 1.2345T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        addr = session.select(5)
        reply = addr.send("F")
        assert reply == " 1.2345T"
        assert fake.sent == [SENT_A5, SENT_F]

    def test_addressed_send_validates_address_at_select(self) -> None:
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        with pytest.raises(CommandError):
            session.select(31)
        with pytest.raises(CommandError):
            session.select(-1)

    def test_device_helper_returns_addressed_dtm151(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(b" 0.1T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        dtm = session.device(address=5)
        assert isinstance(dtm, DTM151Serial)
        dtm.read_field()
        assert fake.sent == [SENT_A5, SENT_F]

    def test_address_boundaries(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.scripted(
            [
                (SENT_A0, b" \r"),
                (SENT_R2, b" \r"),
                (SENT_A30, b" \r"),
                (SENT_R2, b" \r"),
            ]
        )
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.select(0).send("R2")
        session.select(30).send("R2")


class TestBroadcastTrigger:
    def test_broadcast_trigger_sends_V_without_address(self) -> None:
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.broadcast_trigger()
        assert fake.sent == [SENT_V]

    def test_broadcast_trigger_does_not_read_reply(self) -> None:
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.broadcast_trigger()
        # If we had read a reply, we'd have asserted on the empty queue.
        assert fake.sent == [SENT_V]

    def test_mixed_broadcast_then_addressed_read(self) -> None:
        """Typical multi-drop flow: trigger all, then read each addressed device."""
        fake = FakeTransport()
        fake.open()
        fake.scripted(
            [
                # The broadcast V is write-only (no reply) — empty reply bytes.
                (SENT_V, b""),
                # Then we read device 0 then device 5.
                (SENT_A0, b""),
                (SENT_F, b" 0.10T\r"),
                (SENT_A5, b""),
                (SENT_F, b" 0.25T\r"),
            ]
        )
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.broadcast_trigger()
        r0 = DTM151Serial(session.select(0)).read_field()
        r1 = DTM151Serial(session.select(5)).read_field()
        assert r0.value == pytest.approx(0.10)
        assert r1.value == pytest.approx(0.25)
        assert fake.sent == [SENT_V, SENT_A0, SENT_F, SENT_A5, SENT_F]
