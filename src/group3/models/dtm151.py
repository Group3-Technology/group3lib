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

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Protocol, runtime_checkable

from group3.exceptions import (
    BadTemperatureReadingError,
    CommandError,
    NoTemperatureProbeError,
    ResetError,
)
from group3.protocol import commands
from group3.protocol.core import Group3Protocol
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
from group3.types import (
    DeviceMetadataSnapshot,
    DeviceProfile,
    DeviceStatus,
    Reading,
    ScriptCommandResult,
)


@runtime_checkable
class _ProtocolLike(Protocol):
    """Structural type shared by ``Group3Protocol`` and ``AddressedProtocol``.

    Only request/reply and write-only operations are declared here. The
    streaming primitives (``read_next``, ``drain_pending``) live on
    :class:`Group3Protocol` only — streaming on a G3CL addressed loop is
    unsafe because SM1 readings carry no address tag and cannot be
    attributed to a specific device. :meth:`DTM151Serial.stream_field`
    enforces this with a runtime check.
    """

    def send(self, command: str, timeout: float | None = None) -> str: ...
    def send_no_reply(self, command: str) -> None: ...
    def send_setter(self, command: str) -> None: ...


# Default wait time between ``V`` (trigger) and ``F`` (read) per manual section 4.7.3.
_TRIGGER_SETTLE_SECONDS: float = 0.175

# Default per-reply wait inside a field stream. At Kn=1 (1 Hz) we expect a
# reading every ~1 s; 2 s gives comfortable headroom. Callers can override for
# slower Kn values.
_DEFAULT_STREAM_TIMEOUT: float = 2.0

# Short window used to drain in-flight replies around streaming transitions
# (SM1→SM0, pause/resume). Needs to be long enough to catch a reading that
# was already mid-transmission, short enough that it doesn't slow the user
# down. 50 ms comfortably absorbs a ~20-byte reply at 9600 baud.
_STREAM_DRAIN_WINDOW: float = 0.05

_SCRIPT_NUMBER_RE = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?"
_SCRIPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"SM[01]"),
    re.compile(r"SU[01]"),
    re.compile(
        r"(?:D[01]|EZ|EP|EO|EC|EL|GA|GD|GC|GV|IC|ID|IG|IJ|IK|IY|IZ|IR|IO|IL|IN|NH|NN|NT)"
    ),
    re.compile(r"[FPQTZV]"),
    re.compile(r"R[0-3]"),
    re.compile(r"A(?:[0-9]|[12][0-9]|30)"),
    re.compile(r"J[0-9]+"),
    re.compile(r"K[0-9]+"),
    re.compile(rf"SC{_SCRIPT_NUMBER_RE}"),
    re.compile(rf"Y{_SCRIPT_NUMBER_RE}"),
    re.compile(rf"O{_SCRIPT_NUMBER_RE}"),
)

_SCRIPT_REQUEST_COMMANDS = frozenset(
    {"F", "P", "T", "IC", "ID", "IG", "IJ", "IK", "IY", "IZ", "IR", "IO", "IL", "IN"}
)
_SCRIPT_WRITE_ONLY_COMMANDS = frozenset({"V", "SM0", "SM1"})


def _start_auto_transmit(protocol: Group3Protocol) -> None:
    """Send ``SM1`` and clear the returned command off the wire.

    ``SMn`` carries no payload reply, so it goes out write-only. On a link
    that returns commands — a G3CL loop rippling the message back (manual
    §4.5.1, page 4-7) or DIP S2-4 echo — the ``SM1`` text is then left in
    the buffer, and the first streaming read parses it as a reading.
    Bench-observed on an FTR fiber-optic link (2026-07-26): the stream died
    on ``ProtocolError("Expected numeric field reply, got 'SM1'")``.

    Request/reply commands never hit this because
    :meth:`~group3.transport.serial.SerialTransport.request` resets the
    input buffer before writing; a streaming read cannot.

    Streaming is stopped when this runs, so the returned command and its
    bare-terminator ack are the only bytes in flight. If the device is
    free-running anyway (DIP S2-1 ON, "transmit every reading"), the drain
    sees a reading instead and raises rather than silently desyncing.
    """
    protocol.send_no_reply(commands.SM1)
    if protocol.expect_command_returned:
        protocol.drain_setter_ack(echo_command=commands.SM1)


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
    # connect-time identification
    # ------------------------------------------------------------------

    def detect_command_echo(self) -> bool:
        """Probe whether commands come back on the wire ahead of their replies.

        Sends ``Ctrl-B`` (``\\x02``, the baud-rate switch query — the cheapest
        reply the device offers) and reports whether the command was returned
        first. Both causes look identical on the wire and both are handled the
        same way, so this single probe covers them: the device's S2-4 echo
        (manual §3.6, page 3-11) and a G3CL loop rippling the message back to
        the host (manual §4.5.1, page 4-7).

        The result is recorded on the underlying
        :class:`~group3.protocol.core.Group3Protocol` as
        :attr:`~group3.protocol.core.Group3Protocol.expect_command_returned`,
        so subsequent commands strip the returned prefix correctly.

        Returns:
            ``True`` if the command came back before its reply.

        Raises:
            NotImplementedError: ``self._protocol`` is not a
                :class:`Group3Protocol` (control bytes are not supported via
                the addressed G3CL protocol in this release).
        """
        protocol = self._protocol
        if not isinstance(protocol, Group3Protocol):
            raise NotImplementedError(
                "detect_command_echo() requires a direct Group3Protocol; G3CL "
                "multi-drop probing is not supported in this release."
            )
        protocol.send_control(commands.CTRL_B, expect_echo=None)
        return protocol.expect_command_returned

    def identify(self, coerce_echo_off: bool = True) -> DeviceProfile:
        """Read the device's DIP-switch state and baud-rate switch.

        Probes the device with ``Ctrl-D`` (``\\x04``) and ``Ctrl-B`` (``\\x02``)
        to capture its boot-time configuration into a :class:`DeviceProfile`.
        Whether commands come back on the wire is auto-detected from the first
        reply and recorded on the underlying :class:`Group3Protocol` so
        subsequent commands strip the returned prefix correctly.

        Recommended as the first call after :meth:`Group3Protocol` is wired to a
        live transport — both for logging the device profile and for normalising
        echo state across firmware DIP defaults.

        Args:
            coerce_echo_off: When ``True`` (default), if commands are being
                returned the method sends ``SE0`` and then re-probes with
                :meth:`detect_command_echo`. ``SE0`` clears the device's S2-4
                echo but cannot stop a G3CL loop from rippling commands back,
                so the re-probe — not the ``SE0`` ack — decides the final
                state. A device that still returns commands afterwards is on
                a loop, reported as :attr:`DeviceProfile.loop_echo`. Set
                ``False`` to leave echo state as the device booted.

        Returns:
            A :class:`DeviceProfile` snapshot. Note that on G3CL multi-drop
            sessions the per-device probe is currently unsupported — call
            :meth:`identify` only on a :class:`DTM151Serial` constructed
            directly from a :class:`Group3Protocol`.

        Raises:
            NotImplementedError: ``self._protocol`` is not a
                :class:`Group3Protocol` (e.g. it's an addressed G3CL protocol).
            ProtocolError: A reply was not the expected shape — most often a
                bit-ordering mismatch in the DIP-switch parser (see TODO note in
                :func:`group3.protocol.parser.parse_dip_switches`).
        """
        protocol = self._protocol
        if not isinstance(protocol, Group3Protocol):
            raise NotImplementedError(
                "identify() requires a direct Group3Protocol; G3CL multi-drop "
                "identification is not supported in this release."
            )
        # Drain any S2-1 streaming noise that may have arrived before connect.
        protocol.drain_pending()

        dip_reply = protocol.send_control(commands.CTRL_D, expect_echo=None)
        dip = parse_dip_switches(dip_reply)

        # send_control already updated protocol.expect_command_returned from
        # the auto-detect.
        baud_reply = protocol.send_control(
            commands.CTRL_B, expect_echo=protocol.expect_command_returned
        )
        baud = parse_baud_code(baud_reply)

        loop_echo = False
        if coerce_echo_off and protocol.expect_command_returned:
            # Deliberately not send_setter(): this is a repair, and it runs
            # exactly when the link is misbehaving. Bench-observed on an FTR
            # loop with echo ON (2026-07-26): the device's echoed copy of an
            # ASCII command comes back corrupted (``SE0`` returned as
            # ``b'S\\x05`\\x000'``), so a strict echo-prefix check would abort
            # the one call that fixes the link. Write it, absorb whatever
            # comes back, and let the re-probe below establish the truth.
            drained = protocol.send_unvalidated(commands.SE0)
            # Corruption is expected in that drain, so decode leniently — but
            # a recognisable §4.5.3 error still means SE0 was rejected, and
            # reporting loop_echo for a device that simply refused the command
            # would be a lie.
            check_error(drained.decode("ascii", errors="ignore"))
            # SE0 turns off the device's own echo, but on a G3CL loop the
            # command is still retransmitted around the loop back to the host
            # (manual §4.5.1, page 4-7). Re-probe instead of assuming the ack
            # means silence — assuming it leaves the protocol stripping a
            # prefix that is still arriving, and every later reply desyncs.
            loop_echo = self.detect_command_echo()

        return DeviceProfile(
            dip=dip,
            baud=baud,
            command_returned=protocol.expect_command_returned,
            loop_echo=loop_echo,
        )

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

    def read_temperature(self) -> Reading:
        """Request and return the probe temperature (``T`` command).

        The probe's temperature sensor is calibrated for temperature-corrected
        Hall-probe variants (LPT/MPT-141/231). On a non-temperature-corrected
        probe the device replies with ``NO TEMPERATURE PROBE``; this method
        raises :class:`NoTemperatureProbeError` in that case. A faulty sensor
        raises :class:`BadTemperatureReadingError`.

        The reply format matches :meth:`read_field` — a floating-point value
        with an optional ``C`` unit suffix when ``SUn``/S2-6 is enabled.

        Source: vendor command reference (row "Temperature Reading: Request").
        """
        reply = self._protocol.send(commands.T)
        return parse_reading(reply)

    def set_send_units(self, enabled: bool) -> None:
        """Enable or disable unit suffixes in numeric replies (``SU1`` / ``SU0``)."""
        self._protocol.send_setter(commands.su_set_send_units(enabled))

    def read_metadata_snapshot(
        self,
        include_temperature: bool = True,
    ) -> DeviceMetadataSnapshot:
        """Read a compact metadata snapshot for logging or status displays.

        The snapshot mirrors the operator-facing information the LabVIEW app logs
        alongside streaming field data: current range, filter state, sampling
        interval, acquisition/mode status, and temperature when available.
        Temperature-probe absence or invalid-temperature faults are tolerated and
        represented as ``temperature=None`` so callers can log a ``?`` placeholder
        rather than aborting an acquisition.
        """
        temperature: Reading | None = None
        if include_temperature:
            try:
                temperature = self.read_temperature()
            except (BadTemperatureReadingError, NoTemperatureProbeError):
                temperature = None
        return DeviceMetadataSnapshot(
            range_index=self.get_range(),
            filter_enabled=self.get_filter_enabled(),
            sampling_interval=self.get_sampling_interval(),
            status=self.get_status(),
            temperature=temperature,
        )

    def front_panel_test(self) -> None:
        """Run the device's front-panel display self-test (``Q`` command).

        Per the vendor command reference, ``Q`` in base
        mode triggers a brief visual test of the LED display. The device
        emits no parsable reply. To reset the peak-hold value use
        :meth:`erase_peak` (the ``EP`` command).
        """
        self._protocol.send_setter(commands.Q)

    def display_text(self, text: str) -> None:
        """Display ``text`` on the front panel (``B<text>`` command).

        Up to 7 ASCII characters. Useful for showing operator-facing
        status during automated runs.
        """
        self._protocol.send_setter(commands.b_display_text(text))

    # ------------------------------------------------------------------
    # raw-field diagnostic readouts (vendor command reference)
    # ------------------------------------------------------------------

    def read_raw_field_post_adc(self) -> float:
        """Read the post-ADC raw field value (``WA``).

        Diagnostic reading with no zero, calibration, or scale applied —
        what the analog board produced before user-configurable corrections.
        """
        reply = self._protocol.send(commands.WA)
        return parse_float(reply)

    def read_raw_field_post_cal(self) -> float:
        """Read the post-calibration raw field value (``WE``).

        Calibration applied, zero NOT applied. Useful for separating zero
        drift from calibration drift during diagnostics.
        """
        reply = self._protocol.send(commands.WE)
        return parse_float(reply)

    def read_raw_field_post_zero(self) -> float:
        """Read the post-zero raw field value (``WZ``).

        Zero applied, calibration NOT applied. Complements :meth:`read_raw_field_post_cal`.
        """
        reply = self._protocol.send(commands.WZ)
        return parse_float(reply)

    # ------------------------------------------------------------------
    # calibration / scale (v7.1 customer-accessible additions)
    # ------------------------------------------------------------------

    def calibrate(self, value: float) -> None:
        """Calibrate the current range against ``value`` (``Cn`` command).

        .. warning::
           The exact semantic effect of ``Cn`` in base mode is not fully
           documented in the vendor command reference; verify against your
           hardware before relying on this for production calibration.
           Use :meth:`set_calibration_factor` (``SCn``) when you have the
           cal factor itself rather than a reference field value.
        """
        self._protocol.send_setter(commands.c_calibrate(value))

    def set_field_scale_for(self, value: float) -> None:
        """Adjust the global scale so the current field reading equals ``value`` (``Ln``).

        Convenient calibration entry: place the probe in a field of known
        magnitude, call this with that magnitude, and the device adjusts
        its global scale factor so the reading matches.
        """
        self._protocol.send_setter(commands.l_make_field_equal(value))

    def set_global_scale(self, value: float) -> None:
        """Set the global scale factor directly (``SLn`` command)."""
        self._protocol.send_setter(commands.sl_set_scale(value))

    def set_zero(self, value: float) -> None:
        """Set an explicit zero offset for the current range (``SZn`` command).

        In contrast to :meth:`zero` (the ``Z`` command, which uses the
        present reading as the zero), this writes the offset value directly.
        """
        self._protocol.send_setter(commands.sz_set_zero(value))

    # ------------------------------------------------------------------
    # display / echo (v7.1 customer-accessible additions)
    # ------------------------------------------------------------------

    def set_display_units(self, unit: str) -> None:
        """Set the front-panel display units (``Ufc`` command).

        Args:
            unit: ``"G"`` for gauss, ``"T"`` for tesla. Case-insensitive.
        """
        self._protocol.send_setter(commands.u_set_display_units(unit))

    def set_echo(self, enabled: bool) -> None:
        """Enable or disable the device's command-echo behaviour (``SEn``).

        Updates the underlying :class:`Group3Protocol`'s
        :attr:`~group3.protocol.core.Group3Protocol.expect_command_returned`
        flag after the device acknowledges, so subsequent reply parsing
        strips (or doesn't strip) the returned prefix correctly. Order is
        important: the device-side change is applied first, then the local
        flag is updated, so a failed ack leaves the protocol in its prior
        consistent state.

        Works on both bare :class:`Group3Protocol` and addressed G3CL
        sessions (an :class:`AddressedProtocol` exposes its underlying
        protocol via :attr:`inner` — without that walkthrough, addressed
        traffic after ``SE1`` would mis-parse the echoed ``An`` prefix).

        .. warning::
           ``SE0`` only clears the device's own echo. On a G3CL loop the
           command is still rippled back to the host by the loop itself
           (manual §4.5.1, page 4-7), so ``set_echo(False)`` will leave the
           protocol expecting silence that never comes. Follow it with
           :meth:`detect_command_echo` on a loop link.
        """
        self._protocol.send_setter(commands.se_set_echo(enabled))
        underlying = getattr(self._protocol, "inner", self._protocol)
        if isinstance(underlying, Group3Protocol):
            underlying.expect_command_returned = enabled

    # ------------------------------------------------------------------
    # restart / reset (control-byte commands, vendor command reference)
    # ------------------------------------------------------------------

    def restart(self) -> str:
        """Restart the DTM via the ``Ctrl-U`` (``\\x15``) control byte.

        Re-runs the firmware boot sequence and returns the banner string
        (e.g. ``"GROUP3 DTMS 7.10"``). Numerical user settings are NOT
        cleared unless DIP S2-8 is ON. Echo state is the device's runtime
        state from before — the protocol's
        :attr:`~group3.protocol.core.Group3Protocol.expect_command_returned` is not
        modified.

        Returns:
            The boot banner string, with leading space and terminator
            stripped.

        Raises:
            NotImplementedError: ``self._protocol`` is not a
                :class:`Group3Protocol` (control bytes are not supported
                via the addressed G3CL protocol in this release).
        """
        protocol = self._protocol
        if not isinstance(protocol, Group3Protocol):
            raise NotImplementedError(
                "restart() requires a direct Group3Protocol; control-byte "
                "commands are not supported on G3CL multi-drop sessions."
            )
        return protocol.send_control(
            commands.CTRL_U, expect_echo=protocol.expect_command_returned
        )

    def reset_to_defaults(self) -> None:
        """Reset the DTM to DIP-default configuration via ``Ctrl-X`` (``\\x18``).

        Clears all numerical user settings (calibration factors, zero
        offsets, scale factors, filter parameters) and reloads the
        DIP-switch-defined defaults. The device replies with ``"RESET"``,
        which this method consumes and treats as the success indication
        rather than the device-error it would otherwise represent.

        .. warning::
           After this call the protocol's cached
           :attr:`~group3.protocol.core.Group3Protocol.expect_command_returned` may
           be stale (echo reverts to S2-4's default at reset). Call
           :meth:`identify` to refresh the profile.

        Raises:
            NotImplementedError: ``self._protocol`` is not a
                :class:`Group3Protocol`.
        """
        protocol = self._protocol
        if not isinstance(protocol, Group3Protocol):
            raise NotImplementedError(
                "reset_to_defaults() requires a direct Group3Protocol."
            )
        # The "RESET" reply is the device confirming the reload — the SDK's
        # check_error sees it as a §4.5.3 error string, but here it's the
        # expected success indicator.
        with suppress(ResetError):
            protocol.send_control(
                commands.CTRL_X, expect_echo=protocol.expect_command_returned
            )

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

    # ------------------------------------------------------------------
    # Streaming mode (SM1 / Kn) and sampling rate
    # ------------------------------------------------------------------

    def set_auto_transmit(self, enabled: bool) -> None:
        """Enable or disable auto-transmit mode (``SM1`` / ``SM0``).

        When ``enabled`` is True, the device sends a reading every ``Kn``
        seconds without being asked. Use :meth:`set_sampling_interval` to
        control the rate, or :meth:`stream_field` as the higher-level
        context-managed API.

        Per the vendor command reference, ``SMn`` is documented as
        producing no reply (``GET_DATA=n``, ``GET_TERM=n``) — so unlike
        other setters we do not drain for a deferred error. After ``SM1``
        the next bytes on the bus are real streaming readings, not an
        error code.

        The exception is a link that returns commands (see
        :func:`_start_auto_transmit`): there ``SM1`` must be cleared off the
        wire before streaming reads begin.
        """
        protocol = self._protocol
        if enabled and isinstance(protocol, Group3Protocol):
            _start_auto_transmit(protocol)
            return
        # SM0 needs no such care: the bus is either quiet (a later request()
        # resets the input buffer anyway) or still streaming, in which case
        # the caller drains in-flight readings and the returned command
        # together via drain_pending.
        self._protocol.send_no_reply(commands.sm_set_send_mode(enabled))

    def set_sampling_interval(self, seconds: int) -> None:
        """Set the auto-transmit interval ``Kn`` in seconds.

        ``0`` selects the maximum rate (every internal measurement, 10 Hz).
        ``1`` selects 1 Hz, ``60`` selects once per minute, and so on up to
        ``65534`` (≈18 hours).
        """
        self._protocol.send_setter(commands.k_set_sampling_rate(seconds))

    def get_sampling_interval(self) -> int:
        """Return the current sampling interval ``Kn`` (``IK`` command)."""
        reply = self._protocol.send(commands.IK)
        return parse_int(reply)

    def run_script(self, script: str) -> list[ScriptCommandResult]:
        """Execute a LabVIEW-style concatenated command script.

        The LabVIEW DTM-151 application ships ``Standard.txt`` / ``Custom*.txt``
        files containing bare concatenations like ``SU1IRID``. This helper parses
        those command streams line-by-line, ignores comment/header lines, and
        executes each command in sequence using the same protocol rules as the
        normal Python API.
        """
        results: list[ScriptCommandResult] = []
        for line in script.splitlines():
            stripped = line.strip()
            if stripped == "" or stripped.startswith("#") or ".TXT" in stripped.upper():
                continue
            remaining = "".join(ch for ch in stripped if not ch.isspace())
            while remaining:
                command = _parse_script_command(remaining)
                results.append(self._execute_script_command(command))
                remaining = remaining[len(command) :]
        return results

    def _execute_script_command(self, command: str) -> ScriptCommandResult:
        return _execute_script_command_on_protocol(self._protocol, command)

    @contextmanager
    def stream_field(
        self,
        interval_seconds: int | None = None,
        timeout: float = _DEFAULT_STREAM_TIMEOUT,
    ) -> Iterator[FieldStream]:
        """Context-managed iterator over streamed field readings (``SM1``).

        On entry, optionally sets ``Kn`` from ``interval_seconds``, then sends
        ``SM1`` to start auto-transmission. On exit, sends ``SM0``, drains any
        in-flight reply, and restores the original ``Kn`` if it was changed.

        Args:
            interval_seconds: Seconds between readings — the ``Kn`` parameter
                (vendor command reference). Pass ``0`` for the device's internal
                maximum rate (every measurement, 10 Hz); pass ``1`` for 1 Hz,
                ``60`` for once per minute, and so on up to ``65534``. If
                omitted, the device's existing ``Kn`` is used unchanged. The
                device cannot express non-integer-second intervals other than
                the implicit 10 Hz (``K0``) — this API parameter mirrors the
                device's native units so intent is never silently quantized.
            timeout: Per-reading wait inside the iterator. Raises
                :class:`TimeoutError` from ``__next__`` if a reading doesn't
                arrive in time.

        Yields:
            A :class:`FieldStream` iterator. Use ``for reading in stream:``
            to consume, and ``with stream.paused():`` to issue other commands
            (e.g., :meth:`read_temperature`) without desyncing the bus.

        Raises:
            NotImplementedError: The protocol is a G3CL
                :class:`~group3.session.g3cl.AddressedProtocol`. Streaming
                on a multi-drop loop is unsafe because ``SM1`` readings do
                not carry an address tag; a streaming consumer on one
                address would indiscriminately absorb readings from any
                other device on the loop that also has ``SM1`` enabled. Use
                point-to-point (single ``Group3Protocol`` + one device) for
                streaming.
        """
        if not isinstance(self._protocol, Group3Protocol):
            raise NotImplementedError(
                "stream_field requires a point-to-point Group3Protocol. "
                "Streaming is not supported on G3CL AddressedProtocol because "
                "SM1 readings carry no address tag and could be confused with "
                "other devices' output on the shared loop."
            )
        protocol: Group3Protocol = self._protocol

        previous_kn: int | None = None
        if interval_seconds is not None:
            # Validate up-front via the command builder — raises CommandError
            # on a bad value without issuing the IK round-trip first.
            commands.k_set_sampling_rate(interval_seconds)
            previous_kn = self.get_sampling_interval()
            self.set_sampling_interval(interval_seconds)

        self.set_auto_transmit(True)
        stream = FieldStream(protocol, timeout=timeout)
        try:
            yield stream
        finally:
            try:
                self.set_auto_transmit(False)
            finally:
                # Always absorb any reading that was in flight when we sent
                # SM0, whether or not set_auto_transmit raised.
                with suppress(Exception):
                    protocol.drain_pending(_STREAM_DRAIN_WINDOW)
                if previous_kn is not None:
                    with suppress(Exception):
                        self.set_sampling_interval(previous_kn)

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


class FieldStream:
    """Iterator over field readings from an ``SM1``-enabled device.

    Created internally by :meth:`DTM151Serial.stream_field` — you don't
    instantiate this directly. Each iteration blocks up to ``timeout``
    seconds for the next device-originated reading.

    Example::

        with dtm.stream_field(interval_seconds=0) as stream:
            for reading in stream:
                plot(reading)
                if done:
                    break

    For interleaving other commands without desyncing the bus, use
    :meth:`paused`::

        with dtm.stream_field(interval_seconds=0) as stream:
            for reading in stream:
                if time_for_temp():
                    with stream.paused():
                        temp = dtm.read_temperature()
    """

    def __init__(self, protocol: Group3Protocol, timeout: float) -> None:
        self._protocol = protocol
        self._timeout = timeout
        self._paused = False

    def __iter__(self) -> FieldStream:
        return self

    def __next__(self) -> Reading:
        if self._paused:
            raise RuntimeError(
                "FieldStream is paused — exit the paused() block before iterating"
            )
        reply = self._protocol.read_next(self._timeout)
        return parse_reading(reply)

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Pause streaming so the caller can send request/reply commands.

        Sends ``SM0`` and drains any reply that was already in flight, then
        on exit sends ``SM1`` again. Inside the block, the iterator is
        disabled — calling ``next(stream)`` raises ``RuntimeError``.
        """
        if self._paused:
            raise RuntimeError("FieldStream is already paused")
        # SMn is documented as producing no reply (vendor command reference).
        # Use send_no_reply rather than send_setter so the setter-drain does
        # not accidentally consume an in-flight streaming reading as an
        # "unexpected setter reply". drain_pending below handles cleanup.
        self._protocol.send_no_reply(commands.SM0)
        self._protocol.drain_pending(_STREAM_DRAIN_WINDOW)
        self._paused = True
        try:
            yield
        finally:
            try:
                _start_auto_transmit(self._protocol)
            finally:
                self._paused = False


def _parse_script_command(text: str) -> str:
    for pattern in _SCRIPT_PATTERNS:
        match = pattern.match(text)
        if match is not None:
            return match.group(0)
    raise CommandError(f"Could not parse script command at {text!r}")


def _script_command_requires_reply(command: str) -> bool:
    return command in _SCRIPT_REQUEST_COMMANDS


def _script_command_is_write_only(command: str) -> bool:
    return command in _SCRIPT_WRITE_ONLY_COMMANDS or command.startswith("A")


def _validate_script_command(command: str) -> None:
    if command.startswith("R"):
        commands.r_set_range(int(command[1:]))
    elif command.startswith("A"):
        commands.a_set_address(int(command[1:]))
    elif command.startswith("J"):
        commands.j_set_filter_factor(int(command[1:]))
    elif command.startswith("K"):
        commands.k_set_sampling_rate(int(command[1:]))
    elif command.startswith("SC"):
        commands.sc_set_calibration(float(command[2:]))
    elif command.startswith("Y"):
        commands.y_set_filter_window(float(command[1:]))
    elif command.startswith("O"):
        commands.o_set_offset(float(command[1:]))
    elif command.startswith("SU"):
        commands.su_set_send_units(command == "SU1")
    elif command.startswith("SM"):
        commands.sm_set_send_mode(command == "SM1")


def _is_parameterised_command(command: str) -> bool:
    return command.startswith(("R", "A", "J", "K", "SC", "Y", "O", "SU", "SM"))


def _normalise_script_command(command: str) -> str:
    if not _is_parameterised_command(command):
        return command
    _validate_script_command(command)
    return command


def _execute_script_command_on_protocol(
    protocol: _ProtocolLike,
    command: str,
) -> ScriptCommandResult:
    normalised = _normalise_script_command(command)
    if _script_command_requires_reply(normalised):
        return ScriptCommandResult(command=normalised, reply=protocol.send(normalised))
    if _script_command_is_write_only(normalised):
        protocol.send_no_reply(normalised)
        return ScriptCommandResult(command=normalised, reply=None)
    protocol.send_setter(normalised)
    return ScriptCommandResult(command=normalised, reply=None)
