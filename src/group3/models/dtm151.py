"""DTM-151-S — user-facing driver for Group3's serial-variant digital teslameter.

This class is intentionally boring: methods are one-to-one mappings onto commands in
:mod:`group3.protocol.commands`, with parsing delegated to :mod:`group3.protocol.parser`.
Business logic belongs elsewhere.

Use it with either a :class:`~group3.protocol.core.Group3Protocol` (standalone point-
to-point) or an :class:`~group3.session.g3cl.AddressedProtocol` (multi-drop G3CL).
The class accepts any object that exposes ``send(str, timeout)`` and
``send_no_reply(str)`` — the two we provide, plus any you write yourself.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from group3.exceptions import CommandError
from group3.protocol import commands
from group3.protocol.parser import (
    parse_bool_flag,
    parse_float,
    parse_int,
    parse_reading,
    parse_status,
)
from group3.types import DeviceStatus, Reading


@runtime_checkable
class _ProtocolLike(Protocol):
    """Structural type shared by ``Group3Protocol`` and ``AddressedProtocol``."""

    def send(self, command: str, timeout: float | None = None) -> str: ...
    def send_no_reply(self, command: str) -> None: ...
    def send_setter(self, command: str) -> None: ...


# Default wait time between ``V`` (trigger) and ``F`` (read) per manual section 4.7.3.
_TRIGGER_SETTLE_SECONDS: float = 0.175


class DTM151Serial:
    """Driver for the DTM-151-S Digital Teslameter.

    Args:
        protocol: Either a :class:`Group3Protocol` (direct RS-232 / fiber-optic,
            single device) or an :class:`AddressedProtocol` from a
            :class:`G3CLSession` (multi-drop).
    """

    def __init__(self, protocol: _ProtocolLike) -> None:
        self._protocol = protocol

    # ------------------------------------------------------------------
    # field measurement
    # ------------------------------------------------------------------

    def read_field(self) -> Reading:
        """Request and return the current field reading (``F`` command)."""
        reply = self._protocol.send(commands.F)
        return parse_reading(reply)

    def read_peak(self) -> Reading:
        """Request and return the peak-hold field reading (``P`` command)."""
        reply = self._protocol.send(commands.P)
        return parse_reading(reply)

    def reset_peak(self) -> None:
        """Reset the peak-hold value to zero (``Q`` command)."""
        self._protocol.send_setter(commands.Q)

    # ------------------------------------------------------------------
    # range selection
    # ------------------------------------------------------------------

    def get_range(self) -> int:
        """Return the current range index (``IR`` command). 0..3 → 0.3..3.0 T."""
        reply = self._protocol.send(commands.IR)
        value = parse_int(reply)
        if not 0 <= value <= 3:
            raise CommandError(f"device reported out-of-range index {value} for IR")
        return value

    def set_range(self, index: int) -> None:
        """Select a field range (``Rn`` command).

        Args:
            index: 0 (0.3 T), 1 (0.6 T), 2 (1.2 T), or 3 (3.0 T).

        Raises:
            CommandError: ``index`` is outside 0..3.
            FixedRangeProbeError: The attached probe is single-range (raised from
                a deferred error reply within the setter drain window).
        """
        cmd = commands.r_set_range(index)
        self._protocol.send_setter(cmd)

    # ------------------------------------------------------------------
    # zero / erase
    # ------------------------------------------------------------------

    def zero(self) -> None:
        """Zero the currently selected range (``Z`` command)."""
        self._protocol.send_setter(commands.Z)

    def erase_zero(self) -> None:
        """Cancel the zero correction on the current range (``EZ`` command)."""
        self._protocol.send_setter(commands.EZ)

    def erase_peak(self) -> None:
        """Reset the peak-hold value (``EP`` command)."""
        self._protocol.send_setter(commands.EP)

    def erase_offset(self) -> None:
        """Clear the offset on all ranges (``EO`` command)."""
        self._protocol.send_setter(commands.EO)

    def get_zero_offset(self) -> float:
        """Inspect the stored zero-offset for the current range (``IZ`` command).

        .. warning::
           TODO(manual §4.5.2): reply format for ``IZ`` is documented as
           "returns current zeroing offset added to field values" but the exact
           numeric format (mantissa/exponent vs. plain decimal) is not explicit in
           the visible manual pages. Current implementation parses as a plain float.
        """
        reply = self._protocol.send(commands.IZ)
        return parse_float(reply)

    # ------------------------------------------------------------------
    # filter
    # ------------------------------------------------------------------

    def get_filter_enabled(self) -> bool:
        """Return whether digital filtering is enabled (``ID`` → ``0``/``1``)."""
        reply = self._protocol.send(commands.ID)
        return parse_bool_flag(reply)

    def set_filter_enabled(self, enabled: bool) -> None:
        """Enable or disable digital filtering (``D1`` / ``D0``)."""
        self._protocol.send_setter(commands.D1 if enabled else commands.D0)

    def get_filter_factor(self) -> int:
        """Return the current filter factor ``J`` (``IJ`` command; manual §4.6)."""
        reply = self._protocol.send(commands.IJ)
        return parse_int(reply)

    def set_filter_factor(self, factor: int) -> None:
        """Set the filter factor ``J`` (``Jn`` command; range 1..65534, default 41)."""
        self._protocol.send_setter(commands.j_set_filter_factor(factor))

    def get_filter_window(self) -> float:
        """Return the current filter window half-width (``IY`` command).

        .. warning::
           TODO(manual §4.6): the reply format for ``IY`` is not explicit in the
           visible manual pages. Current implementation assumes a plain decimal.
        """
        reply = self._protocol.send(commands.IY)
        return parse_float(reply)

    def set_filter_window(self, value: float) -> None:
        """Set the filter window half-width (``Yn``; manual §4.6)."""
        self._protocol.send_setter(commands.y_set_filter_window(value))

    # ------------------------------------------------------------------
    # mode (DC/AC, continuous/triggered)
    # ------------------------------------------------------------------

    def set_ac_mode(self) -> None:
        """Switch to AC measurement mode (``GA``)."""
        self._protocol.send_setter(commands.GA)

    def set_dc_mode(self) -> None:
        """Switch to DC measurement mode (``GD``)."""
        self._protocol.send_setter(commands.GD)

    def set_continuous_mode(self) -> None:
        """Set continuous acquisition (``GC``)."""
        self._protocol.send_setter(commands.GC)

    def set_triggered_mode(self) -> None:
        """Set triggered acquisition (``GV``; manual §4.7).

        After this, the device will only measure on receipt of a broadcast ``V``.
        """
        self._protocol.send_setter(commands.GV)

    def get_status(self) -> DeviceStatus:
        """Return DC/AC and continuous/triggered status (``IG`` command)."""
        reply = self._protocol.send(commands.IG)
        return parse_status(reply)

    def trigger(self, settle: float = _TRIGGER_SETTLE_SECONDS) -> Reading:
        """Trigger a measurement and read it back.

        Sends ``V`` (non-broadcast for a single-device setup) and, after ``settle``
        seconds, sends ``F`` to read the new value. Per manual section 4.7.3, do
        **not** send ``F`` sooner than 175 ms after ``V`` — you'll get the old value.

        For multi-device broadcast triggering, use :meth:`G3CLSession.broadcast_trigger`
        to fire the ``V`` and then call :meth:`read_field` on each addressed device.

        Args:
            settle: Seconds to sleep between ``V`` and ``F``. Default 0.175 per manual.

        Returns:
            The new :class:`Reading`.
        """
        if settle < 0:
            raise CommandError(f"settle must be non-negative, got {settle}")
        self._protocol.send_no_reply(commands.V)
        if settle:
            time.sleep(settle)
        return self.read_field()

    # ------------------------------------------------------------------
    # addressing, calibration, offset
    # ------------------------------------------------------------------

    def set_address(self, address: int) -> None:
        """Explicitly address a device on the G3CL (``An`` command).

        This is the raw address command. For normal G3CL usage, prefer
        :class:`G3CLSession.select` which returns an :class:`AddressedProtocol`
        wrapper that handles addressing automatically.

        Args:
            address: Device address, 0..30.
        """
        self._protocol.send_no_reply(commands.a_set_address(address))

    def get_calibration(self) -> float:
        """Inspect the calibration factor (``IC`` command).

        .. warning::
           TODO(manual §4.5.2): Table 9 notes ``IC`` returns "mantissa and exponent"
           — exact delimiter is not verifiable from the visible manual pages.
           Current implementation expects a single decimal/exponential number.
        """
        reply = self._protocol.send(commands.IC)
        return parse_float(reply)

    def set_calibration(self, factor: float) -> None:
        """Set the calibration factor directly (``SCn`` command)."""
        self._protocol.send_setter(commands.sc_set_calibration(factor))

    def erase_calibration(self) -> None:
        """Reset the calibration factor to 1 on the current range (``EC``)."""
        self._protocol.send_setter(commands.EC)

    def get_offset(self) -> float:
        """Inspect the offset (``IO`` command)."""
        reply = self._protocol.send(commands.IO)
        return parse_float(reply)

    def set_offset(self, value: float) -> None:
        """Set the offset added to every field reading (``On`` command)."""
        self._protocol.send_setter(commands.o_set_offset(value))

    def get_scale(self) -> float:
        """Inspect the scale factor (``IL`` command)."""
        reply = self._protocol.send(commands.IL)
        return parse_float(reply)

    def erase_scale(self) -> None:
        """Reset the scale factor to 1 on all ranges (``EL`` command)."""
        self._protocol.send_setter(commands.EL)
