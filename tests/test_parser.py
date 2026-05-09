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
    parse_baud_code,
    parse_bool_flag,
    parse_dip_switches,
    parse_float,
    parse_int,
    parse_reading,
    parse_status,
)
from group3.types import AcquisitionMode, BaudCode, MeasurementMode, Unit


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


class TestParseDipSwitches:
    """Coverage for the Ctrl-D 16-bit DIP-switch reply parser.

    Bit-ordering assumption is documented as TODO(bench) in the parser; these
    tests pin the *current* assumption (LSB-first per bank: bit 0 = S1-1, bit
    8 = S2-1) so any change shows up as a failing test rather than a silent
    behavioural shift.
    """

    def test_all_off_factory_defaults(self) -> None:
        # 7E2 (S1-6/7/8 OFF), address 0, S2 all OFF.
        dip = parse_dip_switches(" 0000000000000000")
        assert dip.raw_bits == 0
        assert dip.address == 0
        assert dip.data_format.data_bits == 7
        assert dip.data_format.parity == "E"
        assert dip.data_format.stop_bits == 2
        assert dip.echo_enabled is False
        assert dip.terminator_cr is False  # S2-2 OFF -> LF
        assert dip.transmit_every_reading is False
        assert dip.units_gauss is False

    def test_bench_unit_reply_format_with_nibble_spaces(self) -> None:
        # Verbatim reply from FT572EW5 on 2026-05-09: four space-separated
        # 4-bit nibbles. Cross-checked bits: S2-2 (CR), S2-3 (double term),
        # S2-5 (gauss), S2-7 (filter ON) — all ON; everything else OFF.
        dip = parse_dip_switches(" 0101 0110 0000 0000 ")
        assert dip.address == 0
        assert dip.terminator_cr is True
        assert dip.double_terminator is True
        assert dip.units_gauss is True
        assert dip.filter_enabled is True
        assert dip.echo_enabled is False
        assert dip.transmit_every_reading is False
        assert dip.data_format.data_bits == 7
        assert dip.data_format.parity == "E"
        assert dip.data_format.stop_bits == 2

    def test_address_5_via_s1_1_and_s1_3(self) -> None:
        # Bit 0 (S1-1, +1) ON, bit 2 (S1-3, +4) ON; address = 5.
        dip = parse_dip_switches(" 0000000000000101")
        assert dip.address == 5

    def test_address_30_max(self) -> None:
        # All S1-1..S1-5 ON: 1+2+4+8+16 = 31. The DTM address is documented as
        # 0..30 — values above 30 are an out-of-spec switch setting; the
        # parser still returns the raw sum so downstream layers can decide.
        dip = parse_dip_switches(" 0000000000011111")
        assert dip.address == 31

    def test_8n1_data_format(self) -> None:
        # S1-8 ON, S1-7 OFF, S1-6 ON  -> 8N1 per Table 5.
        # Bits 5/6/7 = S1-6/7/8. Set bit 5 and bit 7.
        dip = parse_dip_switches(" 0000000010100000")
        assert dip.data_format.data_bits == 8
        assert dip.data_format.parity == "N"
        assert dip.data_format.stop_bits == 1

    def test_echo_bit_decoded(self) -> None:
        # Bit 11 (S2-4) on -> echo enabled per the working assumption.
        dip = parse_dip_switches(" 0000100000000000")
        assert dip.echo_enabled is True

    def test_terminator_cr_bit_decoded(self) -> None:
        # Bit 9 (S2-2) on -> CR terminator (factory default).
        dip = parse_dip_switches(" 0000001000000000")
        assert dip.terminator_cr is True

    def test_rejects_short_reply(self) -> None:
        with pytest.raises(ProtocolError):
            parse_dip_switches(" 010101")

    def test_rejects_non_binary_chars(self) -> None:
        with pytest.raises(ProtocolError):
            parse_dip_switches(" 0001020304050607")  # contains 2..7

    def test_rejects_empty(self) -> None:
        with pytest.raises(ProtocolError):
            parse_dip_switches("")


class TestParseBaudCode:
    """Coverage for the Ctrl-B hex-character reply parser."""

    @pytest.mark.parametrize(
        "reply, expected",
        [
            (" 0", BaudCode.POS_0),
            (" 5", BaudCode.POS_5),
            (" 9", BaudCode.POS_9),
            (" A", BaudCode.POS_A),
            (" E", BaudCode.POS_E),  # factory preferred 9600
            (" F", BaudCode.POS_F),  # 19200
        ],
    )
    def test_full_hex_range_accepted(self, reply: str, expected: BaudCode) -> None:
        assert parse_baud_code(reply) is expected

    def test_lowercase_hex_accepted(self) -> None:
        assert parse_baud_code(" e") is BaudCode.POS_E

    def test_no_leading_space(self) -> None:
        assert parse_baud_code("E") is BaudCode.POS_E

    def test_rejects_two_chars(self) -> None:
        with pytest.raises(ProtocolError):
            parse_baud_code(" EE")

    def test_rejects_non_hex_char(self) -> None:
        with pytest.raises(ProtocolError):
            parse_baud_code(" G")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ProtocolError):
            parse_baud_code("")
