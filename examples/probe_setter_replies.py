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

One piece of state cannot be honoured: the digital filter. ``D0``/``D1`` have
no corresponding query, so the device cannot be asked what it was and the
sweep leaves the filter OFF. That is reported at the end rather than papered
over.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
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


class _DeviceState:
    """What the sweep disturbs and the device can report back.

    The digital filter is absent deliberately: ``D0``/``D1`` have no query,
    so there is nothing to record and nothing to restore.

    A plain class rather than a dataclass. ``tests/test_examples_smoke.py``
    loads each example with ``exec_module`` without registering it in
    ``sys.modules``, and ``@dataclass`` under ``from __future__ import
    annotations`` has to look the module up there to resolve its string
    annotations — so it raises on import and takes the smoke test with it.
    No other example needs one either.
    """

    def __init__(
        self,
        range_index: int | None,
        measurement: MeasurementMode | None,
        acquisition: AcquisitionMode | None,
        send_units: bool | None,
    ) -> None:
        self.range_index = range_index
        self.measurement = measurement
        self.acquisition = acquisition
        self.send_units = send_units

    def describe(self) -> str:
        unknown = "?"
        rng = self.range_index if self.range_index is not None else unknown
        meas = self.measurement.name if self.measurement else unknown
        acq = self.acquisition.name if self.acquisition else unknown
        units = unknown if self.send_units is None else (
            "on" if self.send_units else "off"
        )
        return f"range={rng}  mode={meas}/{acq}  units={units}"


def _snapshot(dtm: DTM151Serial) -> _DeviceState:
    """Read back everything the sweep will disturb."""
    status = _attempt(dtm.get_status)
    # No query exists for SU. Infer it from whether a reading carries a unit
    # suffix, which is the only observable that setting has.
    reading = _attempt(dtm.read_field)
    return _DeviceState(
        range_index=_attempt(dtm.get_range),
        measurement=status.measurement if status else None,
        acquisition=status.acquisition if status else None,
        send_units=(reading.unit is not Unit.UNKNOWN) if reading else None,
    )


def _restore(dtm: DTM151Serial, before: _DeviceState) -> list[str]:
    """Put back what was recorded. Returns one report line per action."""
    actions: list[str] = []

    def do(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            actions.append(f"  restored {label}")
        except Exception as exc:
            actions.append(f"  FAILED to restore {label}: {type(exc).__name__}")

    if before.range_index is not None:
        target = before.range_index
        do(f"range={target}", lambda: dtm.set_range(target))
    if before.measurement is MeasurementMode.AC:
        do("AC mode", dtm.set_ac_mode)
    elif before.measurement is MeasurementMode.DC:
        do("DC mode", dtm.set_dc_mode)
    if before.acquisition is AcquisitionMode.TRIGGERED:
        do("triggered acquisition", dtm.set_triggered_mode)
    elif before.acquisition is AcquisitionMode.CONTINUOUS:
        do("continuous acquisition", dtm.set_continuous_mode)
    if before.send_units is not None:
        want = bool(before.send_units)
        do("send-units=" + ("on" if want else "off"),
           lambda: dtm.set_send_units(want))
    actions.append(
        "  digital filter left OFF - D0/D1 have no query, so its prior "
        "state could not be read"
    )
    return actions


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
