"""Probe what the DTM-151-S actually replies to setter commands.

Manual §4.5.2 says setters are silent on success, but empirical observation
shows at least ``SU1``/``SU0`` reply with a bare LF terminator. This script
sweeps a curated list of safe (idempotent or trivially-reversible) setters
with a generous error window and prints the raw bytes the device sent back,
so we can build an accurate map.

Usage:
    python examples/probe_setter_replies.py /dev/cu.usbserial-XXXX \\
        --bytesize 7 --parity E --stopbits 2

Defaults match the user's bench device (9600 7E2). Destructive setters
(``Z``, ``EZ``, ``EP``, ``EO``, ``EC``, ``EL``, ``SC``, ``On``, ``Kn``,
``Jn``, ``Yn``) are intentionally excluded — run them by hand with operator
awareness, not from a sweep script.

The sweep changes device state, so it reads that state first and puts it back
afterwards. It used to instead end each pair on an *assumed* default, with a
``# restore`` comment saying so, which is a weaker and different thing: a
device on ``SU1`` came back on ``SU0`` and silently lost the unit suffix from
every reading, a device in AC mode came back in DC, and the range sweep ended
on ``R3`` with no restore at all, so a device on range 0 was left on 3.0 T.
All three observed on the bench.

Every setting the sweep touches is read back and restored, including the
digital filter — ``D0``/``D1`` are answered by ``ID``
(:meth:`DTM151Serial.get_filter_enabled`).

Send-units is the one that cannot always be pinned down, because it has no
query of its own. Its only observable is whether a reading carries a unit
suffix, and a suffix appears when DIP S2-6 is set *or* ``SU1`` is active. So a
suffix seen before the sweep does not by itself mean ``SU1``. The sweep ends
on ``SU0``, which makes the two distinguishable after the fact: if the suffix
disappears, ``SU1`` was supplying it and is restored; if it survives, S2-6 is
supplying it and the ``SU`` register's prior value is genuinely unknowable.
That last case is reported rather than guessed at — issuing ``SU1`` on a
hunch would change state on exactly the devices this is trying to protect.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from group3 import (
    AcquisitionMode,
    DeviceError,
    DTM151Serial,
    Group3Protocol,
    MeasurementMode,
    ProtocolError,
    SerialTransport,
    TransportError,
    Unit,
)
from group3.protocol import commands


def _safe_setters() -> list[str]:
    """Return a curated list of setter command strings safe to probe."""
    return [
        # Both directions of each setter are probed on purpose: the reply
        # shape is what this script exists to record, and the on and off
        # forms cannot be assumed to answer alike. None of these pairs is a
        # restore — the device's own prior state is snapshotted before the
        # sweep and put back by _restore() afterwards.
        commands.su_set_send_units(False),
        commands.su_set_send_units(True),
        commands.su_set_send_units(False),
        commands.D0,
        commands.D1,
        commands.D0,
        commands.r_set_range(0),
        commands.r_set_range(1),
        commands.r_set_range(2),
        commands.r_set_range(3),
        commands.GA,
        commands.GD,
        commands.GC,
        commands.GV,
        commands.GC,
        commands.Q,  # front-panel display self-test (vendor command reference)
    ]


_T = TypeVar("_T")


def _attempt(fn: Callable[[], _T]) -> _T | None:
    """Run a query, or report it as unknown.

    Each read is independent so that one unsupported or flaky query does not
    cost us the others — a value we fail to read is a value we cannot put
    back, so failures have to be per-field.
    """
    try:
        return fn()
    except Exception:
        return None


@dataclass
class _DeviceState:
    """What the sweep disturbs and the device can report back.

    The digital filter is absent deliberately: ``D0``/``D1`` have no query,
    so there is nothing to record and nothing to restore.
    """

    range_index: int | None
    measurement: MeasurementMode | None
    acquisition: AcquisitionMode | None
    filter_enabled: bool | None
    #: Whether a reading carried a unit suffix. Deliberately not named
    #: ``send_units``: the suffix can come from DIP S2-6 instead, so this
    #: records the observable, not the inference drawn from it.
    has_unit_suffix: bool | None

    def describe(self) -> str:
        unknown = "?"

        def flag(value: bool | None) -> str:
            return unknown if value is None else ("on" if value else "off")

        rng = self.range_index if self.range_index is not None else unknown
        meas = self.measurement.name if self.measurement else unknown
        acq = self.acquisition.name if self.acquisition else unknown
        return (
            f"range={rng}  mode={meas}/{acq}  "
            f"filter={flag(self.filter_enabled)}  "
            f"suffix={flag(self.has_unit_suffix)}"
        )


def _snapshot(dtm: DTM151Serial) -> _DeviceState:
    """Read back everything the sweep will disturb."""
    status = _attempt(dtm.get_status)
    reading = _attempt(dtm.read_field)
    return _DeviceState(
        range_index=_attempt(dtm.get_range),
        measurement=status.measurement if status else None,
        acquisition=status.acquisition if status else None,
        filter_enabled=_attempt(dtm.get_filter_enabled),
        has_unit_suffix=(reading.unit is not Unit.UNKNOWN) if reading else None,
    )


def _restore(dtm: DTM151Serial, before: _DeviceState) -> list[str]:
    """Put back what was recorded. Returns one report line per field.

    Every field gets a line, including the ones that cannot be restored. A
    silent skip is the failure mode this whole function exists to remove: if
    ``F`` answered ``NO PROBE`` during the snapshot, or ``IR`` timed out, the
    device is left on whatever the sweep last set — ``R3``, i.e. 3.0 T — and
    saying nothing about it reproduces the original bug while looking like it
    has been fixed.
    """
    actions: list[str] = []

    def do(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            actions.append(f"  restored {label}")
        except Exception as exc:
            actions.append(f"  FAILED to restore {label}: {type(exc).__name__}")

    def unreadable(label: str, left_at: str) -> None:
        actions.append(
            f"  NOT RESTORED {label} - could not be read before the sweep; "
            f"left at {left_at}"
        )

    if before.range_index is not None:
        target = before.range_index
        do(f"range={target}", lambda: dtm.set_range(target))
    else:
        unreadable("range", "R3 (3.0 T)")

    if before.measurement is MeasurementMode.AC:
        do("AC mode", dtm.set_ac_mode)
    elif before.measurement is MeasurementMode.DC:
        do("DC mode", dtm.set_dc_mode)
    else:
        unreadable("measurement mode", "DC")

    if before.acquisition is AcquisitionMode.TRIGGERED:
        do("triggered acquisition", dtm.set_triggered_mode)
    elif before.acquisition is AcquisitionMode.CONTINUOUS:
        do("continuous acquisition", dtm.set_continuous_mode)
    else:
        unreadable("acquisition mode", "continuous")

    if before.filter_enabled is not None:
        want_filter = before.filter_enabled
        do("filter=" + ("on" if want_filter else "off"),
           lambda: dtm.set_filter_enabled(want_filter))
    else:
        unreadable("digital filter", "off")

    actions.extend(_restore_send_units(dtm, before))
    return actions


def _restore_send_units(dtm: DTM151Serial, before: _DeviceState) -> list[str]:
    """Restore ``SU`` using the only evidence there is: the suffix, twice.

    ``SU`` has no query, and a unit suffix can come from DIP S2-6 as well as
    from ``SU1``, so the snapshot alone cannot tell them apart. The sweep ends
    on ``SU0``, which does: read the suffix again now and compare.

    - suffix before, none now -> ``SU1`` was supplying it. Restore it.
    - suffix before and still now -> S2-6 supplies it; ``SU``'s prior value is
      unknowable and does not affect what the device emits. Say so.
    - no suffix before -> ``SU`` was already off. Nothing to do.
    """
    if before.has_unit_suffix is None:
        return ["  NOT RESTORED send-units - no reading before the sweep; "
                "left at SU0"]
    if not before.has_unit_suffix:
        return ["  send-units already off before the sweep; left at SU0"]

    now = _attempt(dtm.read_field)
    if now is None:
        return ["  NOT RESTORED send-units - no reading available; left at SU0"]
    if now.unit is not Unit.UNKNOWN:
        return ["  send-units not restorable - the suffix survives SU0, so it "
                "comes from DIP S2-6 and SU's prior value cannot be known; "
                "left at SU0 with the suffix intact"]
    try:
        dtm.set_send_units(True)
    except Exception as exc:
        return [f"  FAILED to restore send-units=on: {type(exc).__name__}"]
    return ["  restored send-units=on (the suffix vanished under SU0, so SU1 "
            "was supplying it)"]


def _probe(
    protocol: Group3Protocol,
    cmds: Iterable[str],
    error_window: float,
) -> None:
    print(f"{'cmd':<6} {'status':<8} raw_rx")
    print("-" * 60)
    for cmd in cmds:
        try:
            protocol.send_setter(cmd, error_window=error_window)
            status = "ok"
            raw = protocol.last_raw_rx
            note = ""
        except DeviceError as exc:
            status = "device"
            raw = getattr(exc, "raw", b"")
            if isinstance(raw, str):
                raw = raw.encode("ascii", errors="replace")
            note = f"  ({type(exc).__name__})"
        except ProtocolError as exc:
            status = "proto"
            raw = getattr(exc, "raw", b"") or b""
            note = f"  ({exc})"
        except TransportError as exc:
            status = "io"
            raw = b""
            note = f"  ({exc})"
        print(f"{cmd:<6} {status:<8} {raw!r}{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-XXXX")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument(
        "--error-window",
        type=float,
        default=0.2,
        help="Seconds to wait for a setter reply (default 0.2 = 200 ms)",
    )
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument(
        "--echo",
        choices=["auto", "on", "off"],
        default="auto",
        help="Whether commands come back before their reply — true on a G3CL "
        "loop (fiber-optic/FTR link) or with DIP S2-4 echo ON. 'auto' "
        "(default) probes the device and turns echo off if it finds it; "
        "'on' declares a returning link without probing or repairing, so it "
        "fails if the device is also echoing; 'off' declares that nothing "
        "comes back. Use 'auto' unless you know the link state",
    )
    args = parser.parse_args()

    transport = SerialTransport(
        args.port,
        baudrate=args.baud,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
    )
    with transport as t:
        protocol = Group3Protocol(t, expect_command_returned=args.echo == "on")
        dtm = DTM151Serial(protocol)
        if args.echo == "auto":
            # The probe itself lives on the model layer; this script drives
            # the protocol directly, so borrow it for the one call.
            dtm.identify()

        before = _snapshot(dtm)
        print(f"state before : {before.describe()}")
        print()
        try:
            _probe(protocol, _safe_setters(), error_window=args.error_window)
        finally:
            # In a finally block because a sweep that dies halfway leaves the
            # device in a worse state than one that runs to completion, and
            # that is exactly when restoring matters most.
            print()
            print("restoring device state:")
            for line in _restore(dtm, before):
                print(line)
            print()
            print(f"state after  : {_snapshot(dtm).describe()}")


if __name__ == "__main__":
    main()
