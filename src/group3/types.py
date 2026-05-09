"""Public data types shared across the SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Literal


class Unit(str, Enum):
    """Units reported by the teslameter.

    The DTM-151 reports field values in tesla or gauss (selected by DIP switch
    S2-5; DTM-151-S Manual v7.1, Table 4). Replies may append a single-
    character unit suffix when the format switch is configured to include units
    (S2-6, or the ``SUn`` command). Temperature replies from the ``T`` command
    use ``C`` (degrees Celsius).
    """

    TESLA = "T"
    GAUSS = "G"
    KILOGAUSS = "kG"
    CELSIUS = "C"
    UNKNOWN = "?"


class MeasurementMode(Enum):
    """DC vs AC measurement mode (``GD`` / ``GA`` commands)."""

    DC = "DC"
    AC = "AC"


class AcquisitionMode(Enum):
    """Continuous vs triggered acquisition (``GC`` / ``GV`` commands)."""

    CONTINUOUS = "CONTINUOUS"
    TRIGGERED = "TRIGGERED"


class RangeIndex(int, Enum):
    """DTM-151 field ranges (``R0``..``R3``; inspect with ``IR``).

    See the Specifications section of the manual — four ranges from 0.3 T to 3.0 T.
    """

    R_0_3T = 0  # 0.3 T full scale
    R_0_6T = 1  # 0.6 T full scale
    R_1_2T = 2  # 1.2 T full scale
    R_3_0T = 3  # 3.0 T full scale


@dataclass(frozen=True, slots=True)
class Reading:
    """A single field measurement.

    Attributes:
        value: Numeric value as a ``float``. Sign follows the field polarity.
        unit: Units reported by the device. ``Unit.UNKNOWN`` if the reply did not
            include a unit suffix (which depends on a DIP-switch-selected format).
        raw: The normalised reply string with terminators stripped and the leading
            space (see manual section 4.5.2) preserved. Kept for debugging and
            regression tests.
    """

    value: float
    unit: Unit
    raw: str


@dataclass(frozen=True, slots=True)
class DeviceMetadataSnapshot:
    """A lightweight metadata snapshot for logging and monitoring.

    Attributes:
        range_index: Current DTM-151 range index from ``IR``.
        filter_enabled: Digital-filter state from ``ID``.
        sampling_interval: Current ``Kn`` sampling interval in seconds from ``IK``.
        status: Current DC/AC + continuous/triggered mode from ``IG``.
        temperature: Most recent probe-temperature reading from ``T`` when available,
            else ``None`` for probes without temperature support or with an invalid
            temperature sensor reading.
    """

    range_index: int
    filter_enabled: bool
    sampling_interval: int
    status: DeviceStatus
    temperature: Reading | None


@dataclass(frozen=True, slots=True)
class ScriptCommandResult:
    """One command executed by :meth:`group3.DTM151Serial.run_script`.

    Attributes:
        command: The exact command text sent on the wire, without terminator.
        reply: The normalised reply string for request/reply commands, or ``None``
            for setters (which carry no payload on success — see
            :meth:`Group3Protocol.send_setter`) and write-only commands.
    """

    command: str
    reply: str | None


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """Decoded reply from the ``IG`` inspect-general command.

    ``IG`` returns two characters: ``D`` or ``A`` for the measurement mode,
    followed by ``C`` or ``V`` for continuous/triggered (manual section 4.5.4).
    """

    measurement: MeasurementMode
    acquisition: AcquisitionMode
    raw: str


# ---------------------------------------------------------------------------
# Connect-time identification (DIP switches, baud code, device profile)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SerialDataFormat:
    """Decoded serial data format from S1-6/S1-7/S1-8 (manual Table 5, p. 3-11)."""

    data_bits: Literal[7, 8]
    parity: Literal["E", "O", "N"]
    stop_bits: Literal[1, 2]


@dataclass(frozen=True, slots=True)
class DipSwitches:
    """Decoded DIP-switch state from the ``Ctrl-D`` (``\\x04``) reply.

    The reply is documented in the DTM-151 v7.1 confidential commands sheet as a
    "16-bit binary number" — one bit per switch across the S1 (8) and S2 (8) banks
    documented in manual Tables 4 and 5 (pages 3-10/3-11).

    .. warning::
       TODO(bench): the bit ordering of the 16-character binary reply has not yet
       been confirmed against a real DTM-151-S. The :attr:`raw_bits` field is
       always trustworthy (it stores the integer parsed from the reply MSB-first).
       The named per-switch fields are decoded under the working assumption that
       the firmware emits the switches LSB-first as ``S1-1, S1-2, ..., S1-8,
       S2-1, ..., S2-8`` — i.e. ``raw_bits & 1`` is S1-1, ``(raw_bits >> 8) & 1``
       is S2-1. Verify on the bench unit and update :func:`parser.parse_dip_switches`
       if the actual ordering differs.

    Attributes:
        raw_bits: The integer value of the 16-bit binary reply, MSB-first
            (i.e. ``int(reply, 2)``). Always correct regardless of the per-switch
            ordering question above.
        address: Decoded device address from S1-1..S1-5 (sum of weights 1, 2, 4,
            8, 16; max 30).
        data_format: Serial data format from S1-6/S1-7/S1-8 via Table 5.
        transmit_every_reading: S2-1. ``True`` = standalone streaming mode.
        terminator_cr: S2-2. ``True`` = CR terminator (default), ``False`` = LF.
        double_terminator: S2-3. ``True`` adds the *other* terminator byte as a
            pre-terminator (so CR+LF or LF+CR depending on S2-2).
        echo_enabled: S2-4. ``True`` = the device echoes every command byte before
            replying. **This is the bit that drives :meth:`Group3Protocol.send_control`
            echo handling.**
        units_gauss: S2-5. ``True`` = gauss, ``False`` = tesla.
        units_symbol: S2-6. ``True`` = append unit suffix to readings.
        filter_enabled: S2-7. ``True`` = digital filter ON at power-up.
        reload_defaults_on_power: S2-8. ``True`` = reload defaults every power-up.
    """

    raw_bits: int
    address: int
    data_format: SerialDataFormat
    transmit_every_reading: bool
    terminator_cr: bool
    double_terminator: bool
    echo_enabled: bool
    units_gauss: bool
    units_symbol: bool
    filter_enabled: bool
    reload_defaults_on_power: bool


class BaudCode(IntEnum):
    """Bit-rate switch positions from ``Ctrl-B`` (``\\x02``) — manual Table 7, p. 3-12.

    The 16-position rotary switch on the Processor Board selects one of these
    rates. The enum values are bits-per-second (rounded to integer for position
    2 — the manual lists 134.5 baud).

    .. warning::
       TODO(bench): the v7.1 confidential sheet documents the Ctrl-B reply as
       ``"char: A…F"``, but Table 7 of the manual lists the full hex range
       ``0..F``. The parser accepts the full range; verify on bench whether real
       firmware also returns ``0..9`` or only ``A..F``.
    """

    POS_0 = 50
    POS_1 = 110
    POS_2 = 135  # 134.5 in the manual
    POS_3 = 150
    POS_4 = 200
    POS_5 = 300
    POS_6 = 600
    POS_7 = 900
    POS_8 = 1050
    POS_9 = 1200
    POS_A = 1800
    POS_B = 2000
    POS_C = 2400
    POS_D = 4800
    POS_E = 9600  # factory preferred per Table 7
    POS_F = 19200


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """Snapshot of a DTM-151-S's connect-time identification.

    Returned by :meth:`group3.DTM151Serial.identify`. Captures the DIP-switch
    state, the baud-rate switch position, and the live echo state (which may
    have been mutated by ``SEn`` after boot, so it is not necessarily
    ``dip.echo_enabled``).
    """

    dip: DipSwitches
    baud: BaudCode
    echo_enabled: bool
