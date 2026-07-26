"""Tests for streaming mode (SM1/Kn), temperature reads (T), and read_reply."""

from __future__ import annotations

import pytest

from group3 import (
    CommandError,
    DTM151Serial,
    FakeTransport,
    FieldStream,
    G3CLSession,
    Group3Protocol,
    NoProbeError,
    NoTemperatureProbeError,
    TimeoutError,
    Unit,
)

# ---- Golden transcripts ----
SENT_T = b"T\r"
SENT_SM0 = b"SM0\r"
SENT_SM1 = b"SM1\r"
SENT_K0 = b"K0\r"
SENT_K1 = b"K1\r"
SENT_K60 = b"K60\r"
SENT_IK = b"IK\r"


# --------------------------------------------------------------------------- #
# Temperature (T command)
# --------------------------------------------------------------------------- #


class TestReadTemperature:
    def test_sends_T_and_parses_celsius(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" 23.5C\r")
        r = dtm.read_temperature()
        assert fake.sent == [SENT_T]
        assert r.value == pytest.approx(23.5)
        assert r.unit is Unit.CELSIUS
        assert r.raw == " 23.5C"

    def test_reply_without_unit_suffix(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        # When SU0 (or S2-6 OFF), the device omits the C suffix.
        fake.queue_reply(b" 23.5\r")
        r = dtm.read_temperature()
        assert r.value == pytest.approx(23.5)
        assert r.unit is Unit.UNKNOWN

    def test_no_temperature_probe_error(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" NO TEMPERATURE PROBE\r")
        with pytest.raises(NoTemperatureProbeError):
            dtm.read_temperature()


# --------------------------------------------------------------------------- #
# Send mode (SM0/SM1) and sampling interval (Kn/IK)
# --------------------------------------------------------------------------- #


class TestSendModeAndSamplingInterval:
    def test_set_auto_transmit_on(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_auto_transmit(True)
        assert fake.sent == [SENT_SM1]

    def test_set_auto_transmit_off(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_auto_transmit(False)
        assert fake.sent == [SENT_SM0]

    def test_set_sampling_interval_zero(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """K0 selects the internal max rate (10 Hz)."""
        dtm.set_sampling_interval(0)
        assert fake.sent == [SENT_K0]

    def test_set_sampling_interval_1(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_sampling_interval(1)
        assert fake.sent == [SENT_K1]

    def test_set_sampling_interval_60(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_sampling_interval(60)
        assert fake.sent == [SENT_K60]

    def test_set_sampling_interval_rejects_negative(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_sampling_interval(-1)

    def test_set_sampling_interval_rejects_too_big(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_sampling_interval(65535)

    def test_get_sampling_interval(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        # Real device returns IK in decimal form (' 1.' for K=1).
        fake.queue_reply(b" 1.\r")
        assert dtm.get_sampling_interval() == 1
        assert fake.sent == [SENT_IK]


# --------------------------------------------------------------------------- #
# FieldStream context manager
# --------------------------------------------------------------------------- #


class TestStreamField:
    def test_enters_and_exits_sends_SM1_and_SM0(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Entering the stream writes SM1; exiting writes SM0."""
        with dtm.stream_field() as stream:
            assert isinstance(stream, FieldStream)
        # Expect SM1 on enter, SM0 on exit.
        assert fake.sent == [SENT_SM1, SENT_SM0]

    def test_iterates_queued_readings(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" 0.10T\r")
        fake.queue_reply(b" 0.20T\r")
        fake.queue_reply(b" 0.30T\r")
        values: list[float] = []
        with dtm.stream_field() as stream:
            for reading in stream:
                values.append(reading.value)
                if len(values) == 3:
                    break
        assert values == [pytest.approx(0.10), pytest.approx(0.20), pytest.approx(0.30)]

    def test_loop_link_absorbs_returned_SM1_before_reading(
        self, dtm: DTM151Serial, fake: FakeTransport, protocol: Group3Protocol
    ) -> None:
        """On a returning link the SM1 ripple must not be read as a reading.

        Bench-observed on an FTR fiber-optic link (2026-07-26): SM1 goes out
        write-only, the loop ripples ``SM1\\r`` back with a bare-LF ack, and
        the first streaming read parsed it — ProtocolError "Expected numeric
        field reply, got 'SM1'".
        """
        protocol.expect_command_returned = True
        fake.queue_reply(b"SM1\r\n")  # ripple (terminator included) + ack
        fake.queue_reply(b" 0.10T\r")
        fake.queue_reply(b" 0.20T\r")
        values: list[float] = []
        with dtm.stream_field() as stream:
            for reading in stream:
                values.append(reading.value)
                if len(values) == 2:
                    break
        assert values == [pytest.approx(0.10), pytest.approx(0.20)]
        assert fake.sent == [SENT_SM1, SENT_SM0]

    def test_loop_link_absorbs_returned_SM1_on_resume(
        self, dtm: DTM151Serial, fake: FakeTransport, protocol: Group3Protocol
    ) -> None:
        """Same for the SM1 re-issued when a paused() block exits."""
        protocol.expect_command_returned = True
        fake.queue_reply(b"SM1\r\n")  # ripple + ack for the opening SM1
        fake.queue_reply(b" 0.10T\r")
        values: list[float] = []
        with dtm.stream_field() as stream:
            for reading in stream:
                values.append(reading.value)
                with stream.paused():
                    fake.queue_reply(b"IR 2\r")  # echoed IR + range reply
                    assert dtm.get_range() == 2
                    fake.queue_reply(b"SM1\r\n")  # ripple + ack on resume
                    fake.queue_reply(b" 0.20T\r")
                break
            for reading in stream:
                values.append(reading.value)
                break
        assert values == [pytest.approx(0.10), pytest.approx(0.20)]

    def test_interval_seconds_sets_and_restores_Kn(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Passing interval_seconds snapshots current Kn via IK and restores on exit."""
        # 1. IK on entry to snapshot current Kn → returns "0" (max rate).
        # 2. K1 to set 1 Hz.
        # 3. SM1 to enable streaming.
        # 4. ...iterate...
        # 5. SM0 on exit.
        # 6. K0 to restore original Kn.
        fake.queue_reply(b" 0.\r")  # reply to IK
        with dtm.stream_field(interval_seconds=1):
            pass
        assert fake.sent == [SENT_IK, SENT_K1, SENT_SM1, SENT_SM0, SENT_K0]

    def test_interval_seconds_zero_selects_max_rate(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """interval_seconds=0 sets K0 = internal max rate (10 Hz)."""
        fake.queue_reply(b" 1.\r")  # reply to IK (original was K1)
        with dtm.stream_field(interval_seconds=0):
            pass
        assert fake.sent == [SENT_IK, SENT_K0, SENT_SM1, SENT_SM0, SENT_K1]

    def test_interval_seconds_rejects_negative(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Validation is up-front — no IK round-trip on bad input."""
        with pytest.raises(CommandError), dtm.stream_field(interval_seconds=-1):
            pass
        assert fake.sent == []

    def test_interval_seconds_rejects_too_big(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        with pytest.raises(CommandError), dtm.stream_field(interval_seconds=65535):
            pass
        assert fake.sent == []

    def test_stream_timeout_surfaces(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """If no reading arrives, the iterator raises TimeoutError."""
        with dtm.stream_field() as stream, pytest.raises(TimeoutError):
            next(iter(stream))

    def test_device_error_during_stream_raises(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """An error string arriving mid-stream surfaces as a DeviceError."""
        fake.queue_reply(b" 0.10T\r")
        fake.queue_reply(b" NO PROBE\r")
        values: list[float] = []
        with dtm.stream_field() as stream:
            it = iter(stream)
            values.append(next(it).value)
            with pytest.raises(NoProbeError):
                next(it)
        assert values == [pytest.approx(0.10)]

    def test_drain_absorbs_inflight_replies_on_exit(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """On exit, any reading mid-transmission when we sent SM0 is drained."""
        fake.queue_reply(b" 0.10T\r")
        fake.queue_reply(b" 0.20T\r")  # this one arrives after SM0
        with dtm.stream_field() as stream:
            next(iter(stream))  # consume 0.10
            # leave the loop — exit sends SM0 then drains the queued 0.20T
        # The 0.20T reading must not linger in the reply queue.
        assert not list(fake._replies)


class TestStreamingOnG3CLIsRejected:
    """Streaming on an AddressedProtocol must fail loudly.

    SM1 replies carry no address tag, so a consumer bound to one address
    would absorb readings from any other device on the loop that also has
    SM1 enabled. We refuse to allow the misuse.
    """

    def test_addressed_device_rejects_stream_field(self) -> None:
        fake = FakeTransport()
        fake.open()
        protocol = Group3Protocol(fake)
        session = G3CLSession(protocol)
        dtm = session.device(address=5)
        with pytest.raises(NotImplementedError, match="G3CL"), dtm.stream_field():
            pass
        # No bytes should have left the host — the fence fires before
        # any SM1 / IK / Kn traffic.
        assert fake.sent == []


class TestStreamPause:
    def test_paused_sends_SM0_and_SM1_around_block(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        with dtm.stream_field() as stream, stream.paused():
            pass
        # Enter SM1, pause SM0, resume SM1, exit SM0.
        assert fake.sent == [SENT_SM1, SENT_SM0, SENT_SM1, SENT_SM0]

    def test_paused_allows_interleaved_query(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Inside a paused block, the user can issue request/reply commands.

        Uses ``scripted`` rather than ``queue_reply`` so the T-response is
        only accessible via ``request`` (not the ``read_optional`` used in
        the drain step). This mirrors the real device: the reply to T is
        truly the response to sending T, not a drain-visible pending byte.
        """
        fake.scripted(
            [
                (SENT_SM1, b""),              # enter stream_field
                (SENT_SM0, b""),              # enter paused()
                (SENT_T, b" 23.5C\r"),        # read_temperature
                (SENT_SM1, b""),              # exit paused()
                (SENT_SM0, b""),              # exit stream_field
            ]
        )
        with dtm.stream_field() as stream, stream.paused():
            temp = dtm.read_temperature()
        assert temp.value == pytest.approx(23.5)
        assert fake.sent == [SENT_SM1, SENT_SM0, SENT_T, SENT_SM1, SENT_SM0]

    def test_iterating_while_paused_raises(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        with dtm.stream_field() as stream:
            it = iter(stream)
            with stream.paused(), pytest.raises(RuntimeError, match="paused"):
                next(it)

    def test_drain_on_pause_absorbs_inflight(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """A reading arriving between SM0 and the host's next query is drained.

        The in-flight streaming reading lives in the ``_replies`` queue (seen
        by ``read_optional`` during drain). The T-reply lives in scripted
        (only accessible via ``request``). Together they simulate: device
        sends one last reading after we issue SM0; we drain it; then the
        T query gets its own reply.
        """
        fake.queue_reply(b" 0.33T\r")  # in-flight reading, consumed by drain
        fake.scripted(
            [
                (SENT_SM1, b""),
                (SENT_SM0, b""),
                (SENT_T, b" 20.0C\r"),
                (SENT_SM1, b""),
                (SENT_SM0, b""),
            ]
        )
        with dtm.stream_field() as stream, stream.paused():
            temp = dtm.read_temperature()
        assert temp.value == pytest.approx(20.0)
        # The in-flight 0.33T should have been absorbed by drain_pending.
        assert not list(fake._replies)
