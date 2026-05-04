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

# Real DTM-151-S setters (including the ``An`` prefix) ack with a bare LF.
# AddressedProtocol drains this synchronously after every An so the next
# read isn't desynced — tests must queue it to mirror hardware.
AN_ACK = b"\n"


class TestAddressedProtocol:
    def test_addressed_send_prefixes_address(self) -> None:
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(AN_ACK)
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
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 0.1T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        dtm = session.device(address=5)
        assert isinstance(dtm, DTM151Serial)
        dtm.read_field()
        assert fake.sent == [SENT_A5, SENT_F]

    def test_last_raw_tx_includes_address_prefix(self) -> None:
        """Addressed send must expose A<n> + command in last_raw_tx for debugging."""
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 1.2T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.select(5).send("F")
        assert protocol.last_raw_tx == b"A5\rF\r"
        assert protocol.last_raw_rx == b" 1.2T\r"

    def test_last_raw_tx_accumulates_broadcast_then_addressed(self) -> None:
        """last_raw_tx reports every byte sent since the last reply was read.

        A broadcast produces no reply, so its bytes carry forward to the next
        send() — that's honest about what went on the wire, not a bug.
        """
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.broadcast_trigger()
        assert protocol.last_raw_tx == b"V\r"
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 0.1T\r")
        session.select(0).send("F")
        # The addressed send flushes everything since the last reply: V, A0, F.
        assert protocol.last_raw_tx == b"V\rA0\rF\r"
        assert protocol.last_raw_rx == b" 0.1T\r"

    def test_pending_tx_clears_after_transport_failure(self) -> None:
        """A failed addressed send must not poison the next successful exchange."""
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        # Arrange: A5 ack succeeds (drained), F read raises a TimeoutError.
        from group3 import TimeoutError as G3TimeoutError
        fake.queue_reply(AN_ACK)
        fake.queue_error(G3TimeoutError("simulated"))
        with pytest.raises(G3TimeoutError):
            session.select(5).send("F")
        # The failed attempt's bytes are captured for inspection.
        assert protocol.last_raw_tx == b"A5\rF\r"
        # A later successful exchange must NOT include any leaked A5\rF\r.
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 0.5T\r")
        session.select(10).send("F")
        assert protocol.last_raw_tx == b"A10\rF\r"

    def test_last_raw_tx_resets_after_each_reply(self) -> None:
        """Pending bytes clear after every send() that produces a reply."""
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 0.1T\r")
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 0.2T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.select(5).send("F")
        assert protocol.last_raw_tx == b"A5\rF\r"
        session.select(10).send("F")
        # Previous A5/F is gone — only the latest addressed exchange remains.
        assert protocol.last_raw_tx == b"A10\rF\r"

    def test_address_boundaries(self) -> None:
        fake = FakeTransport()
        fake.open()
        # An ack arrives between the address byte and the R2 reply, mirroring
        # the bare-LF setter ack the device sends for every command.
        fake.scripted(
            [
                (SENT_A0, AN_ACK),
                (SENT_R2, b" \r"),
                (SENT_A30, AN_ACK),
                (SENT_R2, b" \r"),
            ]
        )
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        session.select(0).send("R2")
        session.select(30).send("R2")

    def test_addressed_send_does_not_consume_command_reply_as_address_ack(
        self,
    ) -> None:
        """Regression: An's bare-LF ack must be drained synchronously so that
        the next command's reply isn't returned as an empty string.

        Before the drain_setter_ack fix, AddressedProtocol.send issued An via
        send_no_reply and then immediately read the next byte as the F reply
        — which was actually the A5 ack ``\\n``, returning ``""`` and leaving
        the real F reply queued for the next command to consume.
        """
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(AN_ACK)
        fake.queue_reply(b" 1.2T\r")
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        # If An's ack leaks into F's read, this returns "" and asserts on the
        # next exchange would see a stale ` 1.2T` reply.
        assert session.select(5).send("F") == " 1.2T"

    def test_addressed_send_setter_does_not_mask_setter_error(self) -> None:
        """Regression: An's ack must be drained before send_setter so that a
        setter's deferred error string isn't masked by the An ack arriving
        first.

        Pre-fix: send_setter would consume the An ack as ``b'\\n'``, decide
        the setter succeeded silently, and leave the real ``NO PROBE`` error
        on the wire to confuse the next command.
        """
        from group3 import NoProbeError
        fake = FakeTransport()
        fake.open()
        fake.queue_reply(AN_ACK)            # An prefix ack — drained
        fake.queue_reply(b" NO PROBE\r")    # Z setter's error reply
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        with pytest.raises(NoProbeError):
            session.select(5).send_setter("Z")


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
                # Each addressed read: An prefix acks with bare LF, then F reply.
                (SENT_A0, AN_ACK),
                (SENT_F, b" 0.10T\r"),
                (SENT_A5, AN_ACK),
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
