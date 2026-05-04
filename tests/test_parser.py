"""Tests for group3.protocol.parser."""

from __future__ import annotations

import pytest

from group3.exceptions import (
    DeviceOverflowError,
    FixedRangeProbeError,
    InvalidCommandError,
    NoProbeError,
    OverRangeError,
    ProtocolError,
    ResetError,
)
from group3.protocol.parser import (
    check_error,
    parse_bool_flag,
    parse_float,
    parse_int,
    parse_reading,
    parse_status,
)
from group3.types import AcquisitionMode, MeasurementMode, Unit


class TestParseFloat:
    def test_plain_decimal(self) -> None:
        assert parse_float(" 1.2345") == pytest.approx(1.2345)

    def test_negative(self) -> None:
        assert parse_float(" -0.5") == pytest.approx(-0.5)

    def test_exponential(self) -> None:
        assert parse_float(" 3.4E-02") == pytest.approx(3.4e-2)
        assert parse_float(" 1.0e5") == pytest.approx(1.0e5)

    def test_integer_without_point(self) -> None:
        assert parse_float(" 41") == pytest.approx(41.0)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ProtocolError):
            parse_float("hello")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ProtocolError):
            parse_float("")


class TestParseInt:
    def test_zero(self) -> None:
        assert parse_int(" 0") == 0

    def test_positive(self) -> None:
        assert parse_int(" 2") == 2

    def test_decimal_zero_from_ik(self) -> None:
        # Real device IK reply with sampling interval 0 (max rate).
        assert parse_int(" 0.") == 0

    def test_decimal_integer_from_ij(self) -> None:
        # Real device IJ reply with filter factor 15.
        assert parse_int(" 15.0000") == 15

    def test_decimal_with_zero_fractional(self) -> None:
        assert parse_int(" 41.0") == 41

    def test_rejects_float(self) -> None:
        with pytest.raises(ProtocolError):
            parse_int(" 1.5")


class TestParseBoolFlag:
    def test_zero_is_false(self) -> None:
        assert parse_bool_flag(" 0") is False

    def test_one_is_true(self) -> None:
        assert parse_bool_flag(" 1") is True

    def test_rejects_other(self) -> None:
        with pytest.raises(ProtocolError):
            parse_bool_flag(" 2")

    def test_rejects_text(self) -> None:
        with pytest.raises(ProtocolError):
            parse_bool_flag(" on")


class TestParseReading:
    def test_no_unit_suffix(self) -> None:
        r = parse_reading(" 1.2345")
        assert r.value == pytest.approx(1.2345)
        assert r.unit is Unit.UNKNOWN
        assert r.raw == " 1.2345"

    def test_tesla_suffix(self) -> None:
        r = parse_reading(" 1.2345T")
        assert r.value == pytest.approx(1.2345)
        assert r.unit is Unit.TESLA

    def test_gauss_suffix(self) -> None:
        r = parse_reading(" 12345.0G")
        assert r.unit is Unit.GAUSS

    def test_exponential(self) -> None:
        r = parse_reading(" -3.4E-02T")
        assert r.value == pytest.approx(-3.4e-2)
        assert r.unit is Unit.TESLA

    def test_celsius_suffix(self) -> None:
        """Temperature replies use a C suffix (DTM-151 Commands v7.1, T row)."""
        r = parse_reading(" 23.5C")
        assert r.value == pytest.approx(23.5)
        assert r.unit is Unit.CELSIUS

    def test_celsius_negative(self) -> None:
        r = parse_reading(" -5.0C")
        assert r.value == pytest.approx(-5.0)
        assert r.unit is Unit.CELSIUS

    def test_rejects_malformed(self) -> None:
        with pytest.raises(ProtocolError):
            parse_reading(" garbage")


class TestParseStatus:
    def test_dc_continuous(self) -> None:
        s = parse_status(" DC")
        assert s.measurement is MeasurementMode.DC
        assert s.acquisition is AcquisitionMode.CONTINUOUS

    def test_ac_triggered(self) -> None:
        s = parse_status(" AV")
        assert s.measurement is MeasurementMode.AC
        assert s.acquisition is AcquisitionMode.TRIGGERED

    def test_rejects_short(self) -> None:
        with pytest.raises(ProtocolError):
            parse_status(" D")

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(ProtocolError):
            parse_status(" XC")

    def test_rejects_unknown_acquisition(self) -> None:
        with pytest.raises(ProtocolError):
            parse_status(" DX")


class TestCheckError:
    """Verify every named error string from manual §4.5.3 maps to the right exception."""

    def test_no_probe(self) -> None:
        with pytest.raises(NoProbeError):
            check_error(" NO PROBE")

    def test_overflow(self) -> None:
        with pytest.raises(DeviceOverflowError):
            check_error(" OVERFLOW")

    def test_fixed_range_probe(self) -> None:
        with pytest.raises(FixedRangeProbeError):
            check_error(" FIXED RANGE PROBE")

    def test_over_range_distinct_from_overflow(self) -> None:
        with pytest.raises(OverRangeError):
            check_error(" OVER RANGE")

    def test_invalid_command(self) -> None:
        with pytest.raises(InvalidCommandError):
            check_error(" INVALID COMMAND ENTRY")

    def test_reset(self) -> None:
        with pytest.raises(ResetError):
            check_error(" RESET")

    def test_normal_reply_not_errored(self) -> None:
        check_error(" 1.2345T")  # must not raise

    def test_empty_reply_not_errored(self) -> None:
        check_error("")  # must not raise — protocol layer handles empties
