"""Public data types shared across the SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Unit(str, Enum):
    """Field-measurement units reported by the teslameter.

    The DTM-151 reports in either tesla or gauss depending on an internal switch
    (DTM-151-S Manual v7.1, Table 4). Replies may append ``T`` or ``G`` as a
    single-character suffix when the format switch is configured to include units.
    """

    TESLA = "T"
    GAUSS = "G"
    KILOGAUSS = "kG"
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
class DeviceStatus:
    """Decoded reply from the ``IG`` inspect-general command.

    ``IG`` returns two characters: ``D`` or ``A`` for the measurement mode,
    followed by ``C`` or ``V`` for continuous/triggered (manual section 4.5.4).
    """

    measurement: MeasurementMode
    acquisition: AcquisitionMode
    raw: str
