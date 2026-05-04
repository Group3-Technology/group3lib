"""End-to-end tests for DTM151Serial through FakeTransport.

Each public method gets a golden-transcript test: the exact bytes we send over the
wire are pinned as module-level constants so protocol regressions show up as diffs
in this file.
"""

from __future__ import annotations

import pytest

from group3 import (
    AcquisitionMode,
    CommandError,
    DeviceMetadataSnapshot,
    DTM151Serial,
    FakeTransport,
    FixedRangeProbeError,
    MeasurementMode,
    NoProbeError,
    ProtocolError,
    Unit,
)

# ---- Golden transcripts ----
SENT_F = b"F\r"
SENT_P = b"P\r"
SENT_Q = b"Q\r"
SENT_IR = b"IR\r"
SENT_R0 = b"R0\r"
SENT_R3 = b"R3\r"
SENT_Z = b"Z\r"
SENT_EZ = b"EZ\r"
SENT_EP = b"EP\r"
SENT_EO = b"EO\r"
SENT_EC = b"EC\r"
SENT_EL = b"EL\r"
SENT_IZ = b"IZ\r"
SENT_IC = b"IC\r"
SENT_IO = b"IO\r"
SENT_IL = b"IL\r"
SENT_D0 = b"D0\r"
SENT_D1 = b"D1\r"
SENT_SU0 = b"SU0\r"
SENT_SU1 = b"SU1\r"
SENT_ID = b"ID\r"
SENT_IJ = b"IJ\r"
SENT_IY = b"IY\r"
SENT_IG = b"IG\r"
SENT_IK = b"IK\r"
SENT_T = b"T\r"
SENT_GA = b"GA\r"
SENT_GD = b"GD\r"
SENT_GC = b"GC\r"
SENT_GV = b"GV\r"
SENT_V = b"V\r"
SENT_J41 = b"J41\r"
SENT_Y05 = b"Y0.5\r"
SENT_A5 = b"A5\r"
SENT_SC_2 = b"SC2\r"
SENT_O_MINUS_1 = b"O-1\r"


# --------------------------------------------------------------------------- #
# Field / peak reading
# --------------------------------------------------------------------------- #


class TestReadings:
    def test_read_field_sends_F_and_parses_reply(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1.2345T\r")
        r = dtm.read_field()
        assert fake.sent == [SENT_F]
        assert r.value == pytest.approx(1.2345)
        assert r.unit is Unit.TESLA
        assert r.raw == " 1.2345T"

    def test_read_field_without_unit_suffix(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" -0.00500\r")
        r = dtm.read_field()
        assert r.value == pytest.approx(-0.005)
        assert r.unit is Unit.UNKNOWN

    def test_read_peak_sends_P(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 2.9999T\r")
        r = dtm.read_peak()
        assert fake.sent == [SENT_P]
        assert r.value == pytest.approx(2.9999)

    def test_reset_peak_sends_Q(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.reset_peak()
        assert fake.sent == [SENT_Q]

    def test_read_temperature_sends_T_and_parses_reply(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" 23.5C\r")
        r = dtm.read_temperature()
        assert fake.sent == [SENT_T]
        assert r.value == pytest.approx(23.5)
        assert r.unit is Unit.CELSIUS


# --------------------------------------------------------------------------- #
# Range selection
# --------------------------------------------------------------------------- #


class TestRange:
    def test_get_range_parses_IR_reply(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 2\r")
        assert dtm.get_range() == 2
        assert fake.sent == [SENT_IR]

    def test_set_range_sends_Rn(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_range(0)
        assert fake.sent == [SENT_R0]

    def test_set_range_boundary_3(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_range(3)
        assert fake.sent == [SENT_R3]

    def test_set_range_rejects_negative(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_range(-1)

    def test_set_range_rejects_too_big(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_range(4)

    def test_set_range_rejects_bool(self, dtm: DTM151Serial) -> None:
        # bool is a subclass of int — explicitly reject to avoid True→1, False→0 surprises
        with pytest.raises(CommandError):
            dtm.set_range(True)

    def test_set_range_on_fixed_probe_raises(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Deferred error from silent-success setter: the drain catches it."""
        fake.queue_reply(b" FIXED RANGE PROBE\r")
        with pytest.raises(FixedRangeProbeError):
            dtm.set_range(2)


# --------------------------------------------------------------------------- #
# Deferred error detection on silent-success setters (manual §4.5.3)
# --------------------------------------------------------------------------- #


class TestSetterDeferredErrors:
    """Setters are silent on success but return §4.5.3 error strings on failure.

    Group3Protocol.send_setter() drains the line for a short window after each
    setter write and raises the matching DeviceError if an error string arrives.
    """

    def test_silent_success_does_not_raise(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        # No reply queued — drain returns b"" and the setter succeeds.
        dtm.zero()
        assert fake.sent == [SENT_Z]

    def test_no_probe_surfaces_from_zero(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" NO PROBE\r")
        with pytest.raises(NoProbeError):
            dtm.zero()

    def test_unexpected_reply_raises_protocol_error(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """A non-error reply to a silent-success setter means desync — raise."""
        fake.queue_reply(b" surprise\r")
        with pytest.raises(ProtocolError, match="Unexpected reply"):
            dtm.zero()

    def test_multiple_setters_in_sequence(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        """Back-to-back setters all succeed when the device stays silent."""
        dtm.set_dc_mode()
        dtm.set_continuous_mode()
        dtm.set_range(1)
        dtm.zero()
        assert fake.sent == [SENT_GD, SENT_GC, b"R1\r", SENT_Z]


# --------------------------------------------------------------------------- #
# Zero / erase
# --------------------------------------------------------------------------- #


class TestZeroAndErase:
    def test_zero_sends_Z(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.zero()
        assert fake.sent == [SENT_Z]

    def test_erase_zero_sends_EZ(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.erase_zero()
        assert fake.sent == [SENT_EZ]

    def test_erase_peak_sends_EP(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.erase_peak()
        assert fake.sent == [SENT_EP]

    def test_erase_offset_sends_EO(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.erase_offset()
        assert fake.sent == [SENT_EO]

    def test_get_zero_offset(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 0.00012\r")
        assert dtm.get_zero_offset() == pytest.approx(0.00012)
        assert fake.sent == [SENT_IZ]


# --------------------------------------------------------------------------- #
# Digital filter
# --------------------------------------------------------------------------- #


class TestFilter:
    def test_filter_on(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_filter_enabled(True)
        assert fake.sent == [SENT_D1]

    def test_filter_off(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_filter_enabled(False)
        assert fake.sent == [SENT_D0]

    def test_set_send_units_on(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        # Hardware empirically replies with a bare LF on SU1/SU0; treated as silent ack.
        fake.queue_reply(b"\n")
        dtm.set_send_units(True)
        assert fake.sent == [SENT_SU1]

    def test_set_send_units_off(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b"\n")
        dtm.set_send_units(False)
        assert fake.sent == [SENT_SU0]

    def test_get_filter_enabled(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1\r")
        assert dtm.get_filter_enabled() is True
        assert fake.sent == [SENT_ID]

    def test_set_filter_factor(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_filter_factor(41)
        assert fake.sent == [SENT_J41]

    def test_set_filter_factor_rejects_zero(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_factor(0)

    def test_set_filter_factor_rejects_huge(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_factor(70000)

    def test_get_filter_factor(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        # Real device returns IJ in decimal form (e.g. ' 41.0000') even though
        # the value is integer-valued. parse_int handles both forms.
        fake.queue_reply(b" 41.0000\r")
        assert dtm.get_filter_factor() == 41
        assert fake.sent == [SENT_IJ]

    def test_set_filter_window(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_filter_window(0.5)
        assert fake.sent == [SENT_Y05]

    def test_set_filter_window_rejects_non_positive(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_window(0)
        with pytest.raises(CommandError):
            dtm.set_filter_window(-1)

    def test_set_filter_window_rejects_infinite(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_window(float("inf"))


# --------------------------------------------------------------------------- #
# Mode (DC/AC, continuous/triggered)
# --------------------------------------------------------------------------- #


class TestMode:
    def test_ac(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_ac_mode()
        assert fake.sent == [SENT_GA]

    def test_dc(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_dc_mode()
        assert fake.sent == [SENT_GD]

    def test_continuous(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_continuous_mode()
        assert fake.sent == [SENT_GC]

    def test_triggered(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_triggered_mode()
        assert fake.sent == [SENT_GV]

    def test_get_status_dc_continuous(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" DC\r")
        s = dtm.get_status()
        assert s.measurement is MeasurementMode.DC
        assert s.acquisition is AcquisitionMode.CONTINUOUS
        assert fake.sent == [SENT_IG]


# --------------------------------------------------------------------------- #
# Trigger round-trip
# --------------------------------------------------------------------------- #


class TestTrigger:
    def test_trigger_sends_V_then_F(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 0.12345T\r")  # reply to F
        r = dtm.trigger(settle=0)  # skip the 175 ms sleep for the test
        assert fake.sent == [SENT_V, SENT_F]
        assert r.value == pytest.approx(0.12345)

    def test_trigger_rejects_negative_settle(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.trigger(settle=-1)


# --------------------------------------------------------------------------- #
# Addressing / calibration / offset
# --------------------------------------------------------------------------- #


class TestAddressingAndCalibration:
    def test_set_address_uses_write_only(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        dtm.set_address(5)
        assert fake.sent == [SENT_A5]

    def test_set_address_rejects_out_of_range(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_address(31)

    def test_set_calibration(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_calibration(2)
        assert fake.sent == [SENT_SC_2]

    def test_set_calibration_rejects_zero(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_calibration(0)

    def test_get_calibration(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1.00000E+00\r")
        assert dtm.get_calibration() == pytest.approx(1.0)
        assert fake.sent == [SENT_IC]

    def test_erase_calibration(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.erase_calibration()
        assert fake.sent == [SENT_EC]

    def test_set_offset_negative(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.set_offset(-1)
        assert fake.sent == [SENT_O_MINUS_1]

    def test_get_offset(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 0.0\r")
        assert dtm.get_offset() == pytest.approx(0.0)
        assert fake.sent == [SENT_IO]

    def test_erase_scale(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        dtm.erase_scale()
        assert fake.sent == [SENT_EL]

    def test_get_scale(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1.0\r")
        assert dtm.get_scale() == pytest.approx(1.0)
        assert fake.sent == [SENT_IL]


class TestMetadataSnapshot:
    def test_reads_snapshot_with_temperature(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" 22.0C\r")
        fake.queue_reply(b" 2\r")
        fake.queue_reply(b" 1\r")
        # Real device returns IK in decimal form (' 0.' for K=0).
        fake.queue_reply(b" 0.\r")
        fake.queue_reply(b" DC\r")
        snapshot = dtm.read_metadata_snapshot()
        assert isinstance(snapshot, DeviceMetadataSnapshot)
        assert snapshot.temperature is not None
        assert snapshot.temperature.value == pytest.approx(22.0)
        assert snapshot.range_index == 2
        assert snapshot.filter_enabled is True
        assert snapshot.sampling_interval == 0
        assert snapshot.status.measurement is MeasurementMode.DC
        assert fake.sent == [SENT_T, SENT_IR, SENT_ID, SENT_IK, SENT_IG]

    def test_snapshot_tolerates_missing_temperature_probe(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" NO TEMPERATURE PROBE\r")
        fake.queue_reply(b" 3\r")
        fake.queue_reply(b" 0\r")
        fake.queue_reply(b" 60.\r")
        fake.queue_reply(b" AV\r")
        snapshot = dtm.read_metadata_snapshot()
        assert snapshot.temperature is None
        assert snapshot.range_index == 3
        assert snapshot.filter_enabled is False
        assert snapshot.sampling_interval == 60
        assert snapshot.status.acquisition is AcquisitionMode.TRIGGERED


class TestScriptRunner:
    def test_run_script_executes_labview_style_concatenated_commands(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.scripted(
            [
                (SENT_SU1, b""),
                (SENT_IR, b" 2\r"),
                (SENT_ID, b" 1\r"),
            ]
        )
        results = dtm.run_script("SU1IRID")
        assert [result.command for result in results] == ["SU1", "IR", "ID"]
        assert [result.reply for result in results] == [None, " 2", " 1"]
        assert fake.sent == [SENT_SU1, SENT_IR, SENT_ID]

    def test_run_script_ignores_header_lines_and_whitespace(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.scripted(
            [
                (SENT_SU1, b""),
                (SENT_IR, b" 2\r"),
                (SENT_ID, b" 0\r"),
            ]
        )
        script = "STANDARD.TXT\n  SU1 IR ID  \n"
        results = dtm.run_script(script)
        assert [result.command for result in results] == ["SU1", "IR", "ID"]
        assert [result.reply for result in results] == [None, " 2", " 0"]

    def test_run_script_rejects_unparseable_text(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError, match="Could not parse"):
            dtm.run_script("HELLO")
