"""Public data types shared across the SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
