"""group3lib — Python driver for Group3 Technology digital teslameters.

First supported device: **DTM-151-S** (serial variant). See :class:`DTM151Serial`.

Basic usage::

    from group3 import DTM151Serial, SerialTransport, Group3Protocol

    with SerialTransport("/dev/cu.usbserial-1") as transport:
        protocol = Group3Protocol(transport)
        dtm = DTM151Serial(protocol)
        reading = dtm.read_field()
        print(reading.value, reading.unit)

For multi-drop (G3CL) usage with multiple addressable devices::

    from group3 import G3CLSession

    with SerialTransport("/dev/cu.usbserial-1") as transport:
        protocol = Group3Protocol(transport)
        with G3CLSession(protocol) as session:
            dtm_a = session.device(address=0)
            dtm_b = session.device(address=1)
            dtm_a.set_triggered_mode()
            dtm_b.set_triggered_mode()
            session.broadcast_trigger()
            # wait >=175 ms, then read each device
"""

from __future__ import annotations

from group3.exceptions import (
    BadTemperatureReadingError,
    CommandError,
    DataCarrierError,
    DeviceError,
    DeviceOverflowError,
    DivideByZeroError,
    FixedRangeProbeError,
    FramingError,
    Group3Error,
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
    TimeoutError,
    TransportError,
)
from group3.models.dtm151 import DTM151Serial, FieldStream
from group3.protocol.core import Group3Protocol
from group3.session.g3cl import AddressedProtocol, G3CLSession
from group3.transport.base import Transport
from group3.transport.fake import FakeTransport
from group3.transport.serial import SerialTransport
from group3.types import (
    AcquisitionMode,
    BaudCode,
    DeviceMetadataSnapshot,
    DeviceProfile,
    DeviceStatus,
    DipSwitches,
    MeasurementMode,
    RangeIndex,
    Reading,
    ScriptCommandResult,
    SerialDataFormat,
    Unit,
)

__version__ = "0.2.0"

__all__ = [
    "AcquisitionMode",
    "AddressedProtocol",
    "BadTemperatureReadingError",
    "BaudCode",
    "CommandError",
    "DTM151Serial",
    "DataCarrierError",
    "DeviceError",
    "DeviceMetadataSnapshot",
    "DeviceOverflowError",
    "DeviceProfile",
    "DeviceStatus",
    "DipSwitches",
    "DivideByZeroError",
    "FakeTransport",
    "FieldStream",
    "FixedRangeProbeError",
    "FramingError",
    "G3CLSession",
    "Group3Error",
    "Group3Protocol",
    "InvalidCommandError",
    "MeasurementMode",
    "NoProbeError",
    "NoTemperatureProbeError",
    "NumberTooBigError",
    "OverRangeError",
    "OverrunError",
    "ParityError",
    "PositiveNumberRequiredError",
    "ProtocolError",
    "RangeIndex",
    "Reading",
    "ResetError",
    "ScriptCommandResult",
    "SerialDataFormat",
    "SerialTransport",
    "TimeoutError",
    "Transport",
    "TransportError",
    "Unit",
    "__version__",
]
