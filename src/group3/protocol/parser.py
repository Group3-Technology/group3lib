"""Reply parsing and device-error detection.

This module is the **only** place that turns an ASCII reply from the teslameter into
a Python value or a device exception. If you need to interpret a new reply shape, add
a function here — not in the model layer.

Reply rules (manual section 4.5.2):

* All replies start with a space character.
* Numeric replies include a decimal point; some are in exponential format.
* A trailing unit character (``T`` or ``G``) may be present depending on the output
  format DIP switch.

Error replies (manual section 4.5.3) are transmitted as plain text strings such as
``OVERFLOW`` or ``NO PROBE`` — :func:`check_error` matches these and raises the
corresponding :mod:`group3.exceptions` class.
"""

from __future__ import annotations

import re
from typing import Final

from group3.exceptions import (
    BadTemperatureReadingError,
    DataCarrierError,
    DeviceError,
    DeviceOverflowError,
    DivideByZeroError,
    FixedRangeProbeError,
    FramingError,
    InvalidCommandError,
    NoProbeError,
    NoTemperatureProbeError,
    NumberTooBigError,
    OverRangeError,
    OverrunError,
    ParityError,
    PositiveNumberRequiredError,
    ProtocolError,
    ResetError,
)
from group3.types import (
    AcquisitionMode,
    BaudCode,
    DeviceStatus,
    DipSwitches,
    MeasurementMode,
    Reading,
    SerialDataFormat,
    Unit,
)

# Order matters slightly: put longer specific strings before shorter generic ones so
# that ``OVER RANGE`` is not masked by a hypothetical ``OVER`` substring match.
_ERROR_TABLE: Final[tuple[tuple[str, type[DeviceError]], ...]] = (
    ("INVALID COMMAND ENTRY", InvalidCommandError),
    ("NUMBER TOO BIG", NumberTooBigError),
    ("POSITIVE NUMBER REQUIRED", PositiveNumberRequiredError),
    ("DIVIDE BY ZERO", DivideByZeroError),
    ("NO TEMPERATURE PROBE", NoTemperatureProbeError),
    ("BAD TEMPERATURE READING", BadTemperatureReadingError),
    ("FRAMING ERROR", FramingError),
    ("OVERRUN ERROR", OverrunError),
    ("PARITY ERROR", ParityError),
    ("DATA CARRIER NOT PRESENT", DataCarrierError),
    ("FIXED RANGE PROBE", FixedRangeProbeError),
    ("NO PROBE", NoProbeError),
    ("OVER RANGE", OverRangeError),
    ("OVERFLOW", DeviceOverflowError),
    ("RESET", ResetError),
)

# ``-?\d+(\.\d*)?([eE][+-]?\d+)?`` matches both plain decimals and exponential
# floats. The optional unit suffix accepts ``T`` / ``G`` / ``kG`` for field
# replies and ``C`` for temperature replies from the ``T`` command.
_NUMBER_RE: Final = re.compile(
    r"""^\s*
        (?P<value>[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
        \s*
        (?P<unit>kG|[TGCtgc])?
        \s*$
    """,
    re.VERBOSE,
)


def check_error(reply: str) -> None:
    """Raise the appropriate :class:`DeviceError` subclass if ``reply`` is an error string.

    Replies start with a space per manual section 4.5.2; we match the substring
    without anchoring.

    Args:
        reply: The normalised (terminator-stripped) reply string.

    Raises:
        DeviceError: A specific subclass matching the named error token.
    """
    upper = reply.upper()
    for needle, exc_class in _ERROR_TABLE:
        if needle in upper:
            raise exc_class(needle, raw=reply)


def parse_float(reply: str) -> float:
    """Parse a numeric reply into a ``float``.

    Args:
        reply: The normalised reply string (leading space from section 4.5.2 is
            tolerated; trailing unit letter is ignored).

    Raises:
        ProtocolError: If ``reply`` does not parse as a number.
    """
    match = _NUMBER_RE.match(reply)
    if match is None:
        raise ProtocolError(
            f"Expected numeric reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    try:
        return float(match.group("value"))
    except ValueError as exc:  # pragma: no cover - regex guarantees parseability
        raise ProtocolError(f"Could not convert {reply!r} to float: {exc}") from exc


def parse_int(reply: str) -> int:
    """Parse an integer-valued reply (e.g. ``IR``, ``IK``, ``IJ``).

    Accepts both the plain-integer form (``' 3'`` from ``IR``, a status
    index) and the decimal-point form (``' 0.'`` from ``IK``, ``' 15.0000'``
    from ``IJ``). Manual §4.5.2 documents that all *numeric* replies include
    a decimal point — status-index replies like ``IR`` are the exception.
    Rejects non-integer values (``' 1.5'`` raises).

    Args:
        reply: The normalised (terminator-stripped) reply string.

    Raises:
        ProtocolError: ``reply`` is not numeric or has a non-zero
            fractional part.
    """
    match = _NUMBER_RE.match(reply)
    if match is None:
        raise ProtocolError(
            f"Expected integer reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    value = float(match.group("value"))
    if value != int(value):
        raise ProtocolError(
            f"Expected integer-valued reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    return int(value)


def parse_bool_flag(reply: str) -> bool:
    """Parse a ``0`` / ``1`` status reply (e.g., ``ID``).

    Raises:
        ProtocolError: Reply is neither ``0`` nor ``1`` after stripping whitespace.
    """
    stripped = reply.strip()
    if stripped == "0":
        return False
    if stripped == "1":
        return True
    raise ProtocolError(
        f"Expected '0' or '1' status reply, got {reply!r}",
        raw=reply.encode("ascii", errors="replace"),
    )


def parse_reading(reply: str) -> Reading:
    """Parse a field/peak reply into a :class:`~group3.types.Reading`.

    The reply is typically ``" 1.2345T\\r"`` or ``" -3.4E-02\\r"``. Unit suffix is
    optional (controlled by a DIP switch). If no unit is present, the ``unit`` field
    is set to :attr:`group3.types.Unit.UNKNOWN`.
    """
    match = _NUMBER_RE.match(reply)
    if match is None:
        raise ProtocolError(
            f"Expected numeric field reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    value = float(match.group("value"))
    unit_text = (match.group("unit") or "").strip()
    unit = _unit_from_suffix(unit_text)
    return Reading(value=value, unit=unit, raw=reply)


def parse_status(reply: str) -> DeviceStatus:
    """Parse the ``IG`` reply.

    Per manual section 4.5.4, ``IG`` returns two characters after the leading space:
    ``D`` or ``A`` (DC or AC mode) followed by ``C`` or ``V`` (continuous or
    triggered/V-awaiting).
    """
    body = reply.strip()
    if len(body) < 2:
        raise ProtocolError(
            f"Expected 2-character status reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    mode_char = body[0].upper()
    acq_char = body[1].upper()
    if mode_char == "D":
        measurement = MeasurementMode.DC
    elif mode_char == "A":
        measurement = MeasurementMode.AC
    else:
        raise ProtocolError(
            f"Unexpected mode character {mode_char!r} in status reply {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    if acq_char == "C":
        acquisition = AcquisitionMode.CONTINUOUS
    elif acq_char == "V":
        acquisition = AcquisitionMode.TRIGGERED
    else:
        raise ProtocolError(
            f"Unexpected acquisition character {acq_char!r} in status reply {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    return DeviceStatus(measurement=measurement, acquisition=acquisition, raw=reply)


def parse_dip_switches(reply: str) -> DipSwitches:
    """Parse the 16-bit binary reply from the ``Ctrl-D`` (``\\x04``) command.

    The DTM-151 v7.1 confidential commands sheet documents this reply as a
    "16-bit binary number". Every documented teslameter reply starts with a
    space (manual section 4.5.2); this parser tolerates an optional leading
    space and requires exactly 16 ``0``/``1`` characters.

    Verified against an Antala bench unit (FT572EW5) on 2026-05-09:
    the firmware formats the reply as ``" 0101 0110 0000 0000 \\n\\r"`` —
    four space-separated 4-bit nibbles plus a trailing space — so this
    parser strips **all** whitespace, not just leading/trailing. Bit
    ordering is LSB-first per bank as assumed in the SDK design: bit 0 =
    S1-1 (address weight 1), bit 8 = S2-1, bit 11 = S2-4 (echo). Every
    cross-checkable bit (filter, terminator format, units, address) lined
    up with observed device behaviour.

    Args:
        reply: The normalised (terminator-stripped) reply string.

    Raises:
        ProtocolError: ``reply`` does not contain exactly 16 binary digits
            after whitespace is removed.
    """
    body = "".join(c for c in reply if not c.isspace())
    if len(body) != 16 or any(c not in "01" for c in body):
        raise ProtocolError(
            f"Expected 16-bit binary DIP-switch reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    raw_bits = int(body, 2)

    # TODO(bench): verify the bit ordering. Tentative assumption: LSB-first per
    # bank — bit 0 = S1-1, bit 7 = S1-8, bit 8 = S2-1, bit 15 = S2-8.
    def _bit(index: int) -> bool:
        return bool((raw_bits >> index) & 1)

    address = (
        (1 if _bit(0) else 0)
        + (2 if _bit(1) else 0)
        + (4 if _bit(2) else 0)
        + (8 if _bit(3) else 0)
        + (16 if _bit(4) else 0)
    )

    # Table 5: S1-6/S1-7/S1-8 (bits 5/6/7) → data bits, parity, stop bits.
    fmt_key = (_bit(7), _bit(6), _bit(5))  # (S1-8, S1-7, S1-6) per Table 5 row order
    fmt_table: dict[tuple[bool, bool, bool], SerialDataFormat] = {
        (False, False, False): SerialDataFormat(7, "E", 2),
        (False, False, True): SerialDataFormat(7, "O", 2),
        (False, True, False): SerialDataFormat(7, "E", 1),
        (False, True, True): SerialDataFormat(7, "O", 1),
        (True, False, False): SerialDataFormat(8, "N", 2),
        (True, False, True): SerialDataFormat(8, "N", 1),
        (True, True, False): SerialDataFormat(8, "E", 1),
        (True, True, True): SerialDataFormat(8, "O", 1),
    }
    data_format = fmt_table[fmt_key]

    return DipSwitches(
        raw_bits=raw_bits,
        address=address,
        data_format=data_format,
        transmit_every_reading=_bit(8),    # S2-1
        terminator_cr=_bit(9),             # S2-2
        double_terminator=_bit(10),        # S2-3
        echo_enabled=_bit(11),             # S2-4
        units_gauss=_bit(12),              # S2-5
        units_symbol=_bit(13),             # S2-6
        filter_enabled=_bit(14),           # S2-7
        reload_defaults_on_power=_bit(15), # S2-8
    )


_BAUD_CODE_TABLE: Final[dict[str, BaudCode]] = {
    "0": BaudCode.POS_0,
    "1": BaudCode.POS_1,
    "2": BaudCode.POS_2,
    "3": BaudCode.POS_3,
    "4": BaudCode.POS_4,
    "5": BaudCode.POS_5,
    "6": BaudCode.POS_6,
    "7": BaudCode.POS_7,
    "8": BaudCode.POS_8,
    "9": BaudCode.POS_9,
    "A": BaudCode.POS_A,
    "B": BaudCode.POS_B,
    "C": BaudCode.POS_C,
    "D": BaudCode.POS_D,
    "E": BaudCode.POS_E,
    "F": BaudCode.POS_F,
}


def parse_baud_code(reply: str) -> BaudCode:
    """Parse the single hex-character reply from the ``Ctrl-B`` (``\\x02``) command.

    Manual Table 7 (page 3-12) lists 16 switch positions ``0..F`` mapping to
    50 baud through 19200 baud. The v7.1 confidential sheet documents the
    reply as ``"char: A…F"`` — likely a typo for ``0..F``; this parser
    accepts the full range. Leading space (manual §4.5.2) is tolerated.

    Args:
        reply: The normalised (terminator-stripped) reply string.

    Raises:
        ProtocolError: ``reply`` is not a single hex character ``0..F``.
    """
    body = reply.strip().upper()
    if len(body) != 1 or body not in _BAUD_CODE_TABLE:
        raise ProtocolError(
            f"Expected single hex baud-code reply, got {reply!r}",
            raw=reply.encode("ascii", errors="replace"),
        )
    return _BAUD_CODE_TABLE[body]


def _unit_from_suffix(text: str) -> Unit:
    if text == "":
        return Unit.UNKNOWN
    normalised = text.lower()
    if normalised == "t":
        return Unit.TESLA
    if normalised == "g":
        return Unit.GAUSS
    if normalised == "kg":
        return Unit.KILOGAUSS
    if normalised == "c":
        return Unit.CELSIUS
    return Unit.UNKNOWN
