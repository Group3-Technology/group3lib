"""Exception hierarchy for the Group3 SDK.

All exceptions raised by this library derive from :class:`Group3Error`, so a user who
does not care about the distinction can catch a single base. The tree mirrors the layers
of the SDK:

* transport-level problems (``TransportError``) — cannot talk to the hardware at all
* protocol-level problems (``ProtocolError``) — talked, but the reply was malformed
* command-level problems (``CommandError``) — the local call was invalid before sending
* device-reported problems (``DeviceError`` and subclasses) — the instrument replied
  with a named error string from DTM-151-S Manual v7.1 section 4.5.3

Note: ``group3.exceptions.TimeoutError`` intentionally shadows the built-in when imported
from ``group3``. Internal code must never ``raise TimeoutError`` without a qualified
import.
"""

from __future__ import annotations


class Group3Error(Exception):
    """Base class for every exception this library raises."""


# -----------------------------------------------------------------------------
# Transport layer
# -----------------------------------------------------------------------------


class TransportError(Group3Error):
    """Could not send or receive bytes over the transport (serial, fake, etc.)."""


class TimeoutError(TransportError):  # noqa: N818 - intentional shadow of built-in
    """The transport returned no (or not enough) data before the configured timeout."""


# -----------------------------------------------------------------------------
# Protocol layer
# -----------------------------------------------------------------------------


class ProtocolError(Group3Error):
    """A reply was received but did not match the expected shape."""

    def __init__(self, message: str, raw: bytes | None = None) -> None:
        super().__init__(message)
        self.raw = raw

    def __str__(self) -> str:
        base = super().__str__()
        if self.raw is not None:
            return f"{base} (raw={self.raw!r})"
        return base


class CommandError(Group3Error):
    """A local caller passed an invalid argument to a command builder or setter."""


# -----------------------------------------------------------------------------
# Device-reported errors (DTM-151-S Manual v7.1 section 4.5.3)
# -----------------------------------------------------------------------------


class DeviceError(Group3Error):
    """Base class for errors the instrument reports via its reply string."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class InvalidCommandError(DeviceError):
    """Device replied ``INVALID COMMAND ENTRY``."""


class NumberTooBigError(DeviceError):
    """Device replied ``NUMBER TOO BIG``."""


class PositiveNumberRequiredError(DeviceError):
    """Device replied ``POSITIVE NUMBER REQUIRED``."""


class DivideByZeroError(DeviceError):
    """Device replied ``DIVIDE BY ZERO``."""


class ResetError(DeviceError):
    """Device replied ``RESET`` — defaults were reloaded."""


class NoTemperatureProbeError(DeviceError):
    """Device replied ``NO TEMPERATURE PROBE``."""


class BadTemperatureReadingError(DeviceError):
    """Device replied ``BAD TEMPERATURE READING``."""


class FramingError(DeviceError):
    """Device replied ``FRAMING ERROR``."""


class OverrunError(DeviceError):
    """Device replied ``OVERRUN ERROR``."""


class ParityError(DeviceError):
    """Device replied ``PARITY ERROR``."""


class DataCarrierError(DeviceError):
    """Device replied ``DATA CARRIER NOT PRESENT``."""


class FixedRangeProbeError(DeviceError):
    """Device replied ``FIXED RANGE PROBE``."""


class NoProbeError(DeviceError):
    """Device replied ``NO PROBE``."""


class DeviceOverflowError(DeviceError):
    """Device replied ``OVERFLOW`` — computed field exceeded ±99999.9."""


class OverRangeError(DeviceError):
    """Device replied ``OVER RANGE`` — current field exceeds the selected range."""
