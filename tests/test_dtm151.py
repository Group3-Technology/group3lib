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
    DTM151Serial,
    FakeTransport,
    FixedRangeProbeError,
    MeasurementMode,
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
SENT_ID = b"ID\r"
SENT_IJ = b"IJ\r"
SENT_IY = b"IY\r"
SENT_IG = b"IG\r"
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
        fake.queue_reply(b" \r")
        dtm.reset_peak()
        assert fake.sent == [SENT_Q]


# --------------------------------------------------------------------------- #
# Range selection
# --------------------------------------------------------------------------- #


class TestRange:
    def test_get_range_parses_IR_reply(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 2\r")
        assert dtm.get_range() == 2
        assert fake.sent == [SENT_IR]

    def test_set_range_sends_Rn(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_range(0)
        assert fake.sent == [SENT_R0]

    def test_set_range_boundary_3(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
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
            dtm.set_range(True)  # type: ignore[arg-type]

    def test_set_range_on_fixed_probe_raises(
        self, dtm: DTM151Serial, fake: FakeTransport
    ) -> None:
        fake.queue_reply(b" FIXED RANGE PROBE\r")
        with pytest.raises(FixedRangeProbeError):
            dtm.set_range(2)


# --------------------------------------------------------------------------- #
# Zero / erase
# --------------------------------------------------------------------------- #


class TestZeroAndErase:
    def test_zero_sends_Z(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.zero()
        assert fake.sent == [SENT_Z]

    def test_erase_zero_sends_EZ(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.erase_zero()
        assert fake.sent == [SENT_EZ]

    def test_erase_peak_sends_EP(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.erase_peak()
        assert fake.sent == [SENT_EP]

    def test_erase_offset_sends_EO(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
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
        fake.queue_reply(b" \r")
        dtm.set_filter_enabled(True)
        assert fake.sent == [SENT_D1]

    def test_filter_off(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_filter_enabled(False)
        assert fake.sent == [SENT_D0]

    def test_get_filter_enabled(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1\r")
        assert dtm.get_filter_enabled() is True
        assert fake.sent == [SENT_ID]

    def test_set_filter_factor(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_filter_factor(41)
        assert fake.sent == [SENT_J41]

    def test_set_filter_factor_rejects_zero(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_factor(0)

    def test_set_filter_factor_rejects_huge(self, dtm: DTM151Serial) -> None:
        with pytest.raises(CommandError):
            dtm.set_filter_factor(70000)

    def test_get_filter_factor(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 41\r")
        assert dtm.get_filter_factor() == 41
        assert fake.sent == [SENT_IJ]

    def test_set_filter_window(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
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
        fake.queue_reply(b" \r")
        dtm.set_ac_mode()
        assert fake.sent == [SENT_GA]

    def test_dc(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_dc_mode()
        assert fake.sent == [SENT_GD]

    def test_continuous(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_continuous_mode()
        assert fake.sent == [SENT_GC]

    def test_triggered(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
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
        fake.queue_reply(b" \r")
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
        fake.queue_reply(b" \r")
        dtm.erase_calibration()
        assert fake.sent == [SENT_EC]

    def test_set_offset_negative(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.set_offset(-1)
        assert fake.sent == [SENT_O_MINUS_1]

    def test_get_offset(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 0.0\r")
        assert dtm.get_offset() == pytest.approx(0.0)
        assert fake.sent == [SENT_IO]

    def test_erase_scale(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" \r")
        dtm.erase_scale()
        assert fake.sent == [SENT_EL]

    def test_get_scale(self, dtm: DTM151Serial, fake: FakeTransport) -> None:
        fake.queue_reply(b" 1.0\r")
        assert dtm.get_scale() == pytest.approx(1.0)
        assert fake.sent == [SENT_IL]
