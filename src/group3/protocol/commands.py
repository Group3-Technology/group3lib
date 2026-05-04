"""DTM-151-S command registry.

Every command this SDK sends to a DTM-151-S is defined here — either as a module-level
constant for zero-argument commands or as a builder function for parametrised commands.
No other module in ``group3`` constructs a command string by concatenation.

All references are against **DTM-151 (serial) User's Manual v7.1**, section 4.5.2
Table 9 and sections 4.6–4.7. Where a command's syntax is not verifiable from the
visible portion of Table 9, a ``TODO(manual)`` marker is placed on the relevant
builder; the corresponding model method raises :class:`NotImplementedError` until the
TODO is resolved.

The codec appends the terminator (``\\r`` by default) — builders here return strings
*without* the terminator.
"""

from __future__ import annotations

from typing import Final

from group3.exceptions import CommandError

# -----------------------------------------------------------------------------
# Zero-argument commands (Table 9, verbatim)
# -----------------------------------------------------------------------------

F: Final = "F"  # Field reading — current selected range.
P: Final = "P"  # Peak hold field reading.
Q: Final = "Q"  # Reset peak hold.
T: Final = "T"  # Temperature reading from the probe's temperature sensor.

Z: Final = "Z"  # Zero the currently selected range.
EZ: Final = "EZ"  # Erase zero (cancel zero correction on current range).
EP: Final = "EP"  # Erase peak hold.
EO: Final = "EO"  # Erase offset (sets offset to 0 on all ranges).
EC: Final = "EC"  # Erase calibration (sets cal factor to 1 on current range).
EL: Final = "EL"  # Erase scale factor (sets scale factor to 1 on all ranges).

D0: Final = "D0"  # Digital filter OFF.
D1: Final = "D1"  # Digital filter ON.

GA: Final = "GA"  # General function AC — AC measurement mode.
GD: Final = "GD"  # General function DC — DC measurement mode.
GC: Final = "GC"  # General function Continuous — continuous measurement.
GV: Final = "GV"  # General function Triggered — wait for V before taking reading.
V: Final = "V"  # Trigger a reading. BROADCAST: never prefix with an address.

# Inspect (query) commands — reply shape is single character or short numeric string.

IC: Final = "IC"  # Inspect calibration factor (mantissa + exponent).
ID: Final = "ID"  # Inspect filter state: "0" (off) or "1" (on).
IG: Final = "IG"  # Inspect general function: "D"/"A" + "C"/"V".
IJ: Final = "IJ"  # Inspect filter factor (J).
IK: Final = "IK"  # Inspect sampling interval (seconds); 0 = max rate (10 Hz).
IY: Final = "IY"  # Inspect filter window half-width.
IZ: Final = "IZ"  # Inspect zero offset.
IR: Final = "IR"  # Inspect range index: "0".."3".
IO: Final = "IO"  # Inspect offset value.
IL: Final = "IL"  # Inspect scale factor.
IN: Final = "IN"  # Inspect display mode.

# Fixed-argument range selects.
R0: Final = "R0"  # Select 0.3 T range.
R1: Final = "R1"  # Select 0.6 T range.
R2: Final = "R2"  # Select 1.2 T range.
R3: Final = "R3"  # Select 3.0 T range.

# Display mode setters (NH/NN/NT from Table 9 — only NN exposed in public API for now).
NH: Final = "NH"  # Display mode: hold (peak).
NN: Final = "NN"  # Display mode: normal (field).
NT: Final = "NT"  # Display mode: temperature.

# Send-mode / streaming control (DTM-151 Commands v7.1, Send Mode row).
SM0: Final = "SM0"  # Send mode: F-Request. Device replies only when host sends F.
SM1: Final = "SM1"  # Send mode: Timed. Device auto-transmits every Kn seconds.


# -----------------------------------------------------------------------------
# Parametrised builders
# -----------------------------------------------------------------------------


def r_set_range(index: int) -> str:
    """Build the ``Rn`` range-select command.

    Args:
        index: Range index, 0–3 (see :class:`group3.types.RangeIndex`).

    Raises:
        CommandError: ``index`` is not in 0..3.
    """
    if not isinstance(index, int) or isinstance(index, bool):
        raise CommandError(f"range index must be int, got {type(index).__name__}")
    if not 0 <= index <= 3:
        raise CommandError(f"range index must be 0..3, got {index}")
    return f"R{index}"


def a_set_address(address: int) -> str:
    """Build the ``An`` address-select command (Table 9, manual §4.5.2).

    The manual specifies ``n = 0 to 30`` — a plain decimal integer with no padding.
    Single-digit addresses go as ``A5``, not ``A05``.

    Args:
        address: Device address in 0..30 (31 devices on the G3CL).

    Raises:
        CommandError: ``address`` is not in 0..30.
    """
    if not isinstance(address, int) or isinstance(address, bool):
        raise CommandError(f"address must be int, got {type(address).__name__}")
    if not 0 <= address <= 30:
        raise CommandError(f"address must be 0..30, got {address}")
    return f"A{address}"


def j_set_filter_factor(factor: int) -> str:
    """Build the ``Jn`` filter-factor command (Table 9; manual section 4.6).

    ``J`` is the filter factor in the recursive-smoothing formula
    ``F(new) = F(old) + (F - F(old))/J``. Higher values produce more smoothing.
    The default after reset is 41, which gives a 4-second time constant at 10 Hz
    sampling.

    Args:
        factor: Filter factor. Table 9 lists the valid range as 1..65534, default 41.

    Raises:
        CommandError: ``factor`` is outside 1..65534.
    """
    if not isinstance(factor, int) or isinstance(factor, bool):
        raise CommandError(f"filter factor must be int, got {type(factor).__name__}")
    if not 1 <= factor <= 65534:
        raise CommandError(f"filter factor must be 1..65534, got {factor}")
    return f"J{factor}"


def y_set_filter_window(value: float) -> str:
    """Build the ``Yn`` filter-window command (Table 9; manual section 4.6).

    The value is the **half-window** width in current display units (tesla or gauss).
    The default after reset is 1 gauss. Table 9 lists the valid range as
    ``0 < n < 1`` reading overshoots (i.e., the full-scale of the current range).

    Args:
        value: Half-window width, must be positive.

    Raises:
        CommandError: ``value`` is not positive, not finite, or not representable.

    Notes:
        TODO(manual §4.6): the exact numeric format the device expects (decimal places,
        scientific notation) is not explicit in the visible manual pages. We send a
        plain decimal representation via ``repr(float)``. Verify once the manual
        pages 4-10/4-11 render correctly.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CommandError(f"filter window must be numeric, got {type(value).__name__}")
    fvalue = float(value)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise CommandError(f"filter window must be finite, got {value}")
    if fvalue <= 0:
        raise CommandError(f"filter window must be positive, got {value}")
    return f"Y{_format_number(fvalue)}"


def sm_set_send_mode(enabled: bool) -> str:
    """Build the ``SMn`` send-mode command (DTM-151 Commands v7.1).

    Args:
        enabled: ``True`` selects ``SM1`` (Timed — device auto-transmits every
            ``Kn`` seconds). ``False`` selects ``SM0`` (F-Request — device only
            replies when the host sends ``F``; this is the default).

    Returns:
        ``"SM1"`` or ``"SM0"``.
    """
    if not isinstance(enabled, bool):
        raise CommandError(f"enabled must be bool, got {type(enabled).__name__}")
    return SM1 if enabled else SM0


def su_set_send_units(enabled: bool) -> str:
    """Build the ``SUn`` send-units command (DTM-151 Commands v7.1).

    Args:
        enabled: ``True`` selects ``SU1`` so numeric replies include unit suffixes
            (e.g., ``T``, ``G``, ``C`` when applicable). ``False`` selects ``SU0``.
    """
    if not isinstance(enabled, bool):
        raise CommandError(f"enabled must be bool, got {type(enabled).__name__}")
    return "SU1" if enabled else "SU0"


def k_set_sampling_rate(seconds: int) -> str:
    """Build the ``Kn`` sampling-rate command (DTM-151 Commands v7.1).

    Controls how often the device auto-transmits a reading when ``SM1`` is
    active. ``0`` means "every reading" — the internal 10 Hz measurement
    rate. Larger values throttle: ``K1`` = 1 s between readings, ``K60`` =
    1-minute interval, etc.

    Args:
        seconds: Sampling interval in seconds, 0..65534.

    Raises:
        CommandError: ``seconds`` is out of range.
    """
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise CommandError(
            f"sampling rate must be int, got {type(seconds).__name__}"
        )
    if not 0 <= seconds <= 65534:
        raise CommandError(f"sampling rate must be 0..65534, got {seconds}")
    return f"K{seconds}"


def sc_set_calibration(factor: float) -> str:
    """Build the ``SCn`` calibration-factor command (Table 9).

    Args:
        factor: Calibration factor, positive.

    Raises:
        CommandError: ``factor`` is not positive or not finite.
    """
    if not isinstance(factor, (int, float)) or isinstance(factor, bool):
        raise CommandError(f"calibration factor must be numeric, got {type(factor).__name__}")
    fvalue = float(factor)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise CommandError(f"calibration factor must be finite, got {factor}")
    if fvalue <= 0:
        raise CommandError(f"calibration factor must be positive, got {factor}")
    return f"SC{_format_number(fvalue)}"


def o_set_offset(value: float) -> str:
    """Build the ``On`` offset command (Table 9).

    The offset is added to every field reading on every range.

    Args:
        value: Offset value (signed).

    Raises:
        CommandError: ``value`` is not finite.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CommandError(f"offset must be numeric, got {type(value).__name__}")
    fvalue = float(value)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise CommandError(f"offset must be finite, got {value}")
    return f"O{_format_number(fvalue)}"


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


def _format_number(value: float) -> str:
    """Render ``value`` as an ASCII decimal suitable for a DTM-151 numeric command.

    Uses ``repr`` for round-tripping of floats, except for integers which render
    without a trailing ``.0`` — the manual notes that whole-number entries do not
    require a decimal point (section 4.5.2).
    """
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)
