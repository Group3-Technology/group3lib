"""Tests for group3.protocol.codec."""

from __future__ import annotations

import pytest

from group3.exceptions import CommandError, ProtocolError
from group3.protocol import codec


class TestEncode:
    def test_basic_ascii_with_cr(self) -> None:
        assert codec.encode("F") == b"F\r"

    def test_appends_configurable_terminator(self) -> None:
        assert codec.encode("F", codec.LF) == b"F\n"
        assert codec.encode("F", codec.CRLF) == b"F\r\n"
        assert codec.encode("F", codec.LFCR) == b"F\n\r"

    def test_multichar_command(self) -> None:
        assert codec.encode("R2") == b"R2\r"
        assert codec.encode("GV") == b"GV\r"

    def test_empty_command_still_emits_terminator(self) -> None:
        # Edge case — not user-facing, but codec should not silently alter input.
        assert codec.encode("") == b"\r"

    def test_rejects_non_ascii(self) -> None:
        with pytest.raises(CommandError, match="ASCII"):
            codec.encode("Réad")


class TestDecode:
    def test_basic_ascii(self) -> None:
        assert codec.decode(b" 1.2345\r") == " 1.2345\r"

    def test_rejects_non_ascii(self) -> None:
        with pytest.raises(ProtocolError, match="ASCII"):
            codec.decode(b"\xff\xfe")


class TestStripTerminators:
    def test_strips_cr(self) -> None:
        assert codec.strip_terminators(" 1.23\r") == " 1.23"

    def test_strips_lf(self) -> None:
        assert codec.strip_terminators(" 1.23\n") == " 1.23"

    def test_strips_crlf(self) -> None:
        assert codec.strip_terminators(" 1.23\r\n") == " 1.23"

    def test_strips_lfcr(self) -> None:
        assert codec.strip_terminators(" 1.23\n\r") == " 1.23"

    def test_preserves_leading_space_per_manual(self) -> None:
        # Section 4.5.2: "All responses from the teslameter start with a space character."
        assert codec.strip_terminators(" 1.23\r") == " 1.23"

    def test_handles_no_terminator(self) -> None:
        assert codec.strip_terminators(" 1.23") == " 1.23"

    def test_round_trip_all_terminators(self) -> None:
        for term in codec.TERMINATORS.values():
            encoded = codec.encode("R2", term)
            decoded = codec.decode(encoded)
            stripped = codec.strip_terminators(decoded)
            assert stripped == "R2"
