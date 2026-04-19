"""Walk through DTM-151-S Manual v7.1 §4.5.4 on a real instrument.

Section 4.5.4 "Some Examples Using the Commands" works through the basic
interactive flow a user would perform on a terminal: check the current range,
change it, zero each range in turn, switch measurement modes, and read a field
value. This script mirrors that sequence end-to-end using the public
:mod:`group3` API, so it doubles as both a hardware smoke test and a
demonstration of how the manual's examples map onto Python.

The sequences from the manual implemented here:

    1. ``Z``                — zero the currently selected range
    2. ``IR``               — inspect the current range index (0..3)
    3. ``R2`` then ``IR``   — change range and confirm the new index
    4. ``GA`` / ``GD``      — AC / DC mode
    5. ``IG``               — inspect mode (DC/AC + continuous/triggered)
    6. ``R0/Z R1/Z R2/Z R3/Z`` — zero every range (the manual writes ``/`` for
       a 1–2 second pause; we sleep 2 s to be safe)
    7. ``GA R0/Z ... R3/Z GD`` — zero every AC range, then return to DC
    8. ``F``                — read the field value after the above

This script **writes to device state** (ranges, zero offsets, measurement
mode). By default it runs a read-only preview — use ``--execute`` to actually
issue the state-changing commands.

Usage::

    python examples/walkthrough.py /dev/cu.usbserial-1            # dry run
    python examples/walkthrough.py /dev/cu.usbserial-1 --execute  # for real

The instrument is assumed to be configured for 9600-8-N-1 with CR terminator
(factory default; manual §3.6). Override baud via ``--baud``.
"""

from __future__ import annotations

import argparse
import sys
import time

from group3 import (
    DTM151Serial,
    FixedRangeProbeError,
    Group3Protocol,
    MeasurementMode,
    SerialTransport,
)

# Manual §4.5.4: "Always wait a second or two after a range change before
# zeroing." 2 s gives comfortable headroom.
POST_RANGE_CHANGE_SLEEP_SECONDS: float = 2.0

RANGE_LABELS = {
    0: "0.3 T",
    1: "0.6 T",
    2: "1.2 T",
    3: "3.0 T",
}


def show_current_state(dtm: DTM151Serial) -> None:
    """Inspect and print the current range, mode, and a field reading (§4.5.4)."""
    current_range = dtm.get_range()                           # IR
    status = dtm.get_status()                                 # IG
    reading = dtm.read_field()                                # F
    print(f"  range    : {current_range} ({RANGE_LABELS[current_range]})")
    print(f"  mode     : {status.measurement.name} / {status.acquisition.name}")
    print(f"  field    : {reading.value:+.6f} {reading.unit.value}")
    print(f"  raw F    : {reading.raw!r}")


def zero_all_ranges(dtm: DTM151Serial) -> None:
    """Manual §4.5.4: "R0/ZR1/ZR2/ZR3/Z" — zero every range in turn.

    Skips ranges with a FIXED RANGE PROBE reply so single-range probes don't
    abort the walkthrough.
    """
    for index in (0, 1, 2, 3):
        label = RANGE_LABELS[index]
        try:
            dtm.set_range(index)
        except FixedRangeProbeError:
            print(f"  [{label}] fixed-range probe — skipping")
            continue
        # Wait for the range to settle before zeroing (manual §4.5.4).
        time.sleep(POST_RANGE_CHANGE_SLEEP_SECONDS)
        dtm.zero()
        # Confirm the range selection (manual notes: "IR will return 2 to
        # confirm the range selection").
        confirmed = dtm.get_range()
        print(f"  [{label}] zeroed; IR confirms range index = {confirmed}")


def zero_all_ac_ranges_then_return_to_dc(dtm: DTM151Serial) -> None:
    """Manual §4.5.4: "GAR0/ZR1/ZR2/ZR3/ZGD" — AC zero sweep, back to DC."""
    print("Switching to AC mode (GA)...")
    dtm.set_ac_mode()
    zero_all_ranges(dtm)
    print("Returning to DC mode (GD)...")
    dtm.set_dc_mode()


def preview(dtm: DTM151Serial) -> None:
    """Read-only preview — no state changes."""
    print("Current state (read-only):")
    show_current_state(dtm)


def execute_walkthrough(dtm: DTM151Serial) -> None:
    """Full §4.5.4 walkthrough — modifies device state."""
    print("=" * 60)
    print("DTM-151-S walkthrough — Manual v7.1 §4.5.4")
    print("=" * 60)

    print("\n1. Initial state")
    show_current_state(dtm)

    # Remember where we started so we can restore at the end.
    initial_range = dtm.get_range()
    initial_mode = dtm.get_status().measurement

    print("\n2. DC zero sweep (R0/Z R1/Z R2/Z R3/Z)")
    dtm.set_dc_mode()
    zero_all_ranges(dtm)

    print("\n3. AC zero sweep then back to DC (GA R0/Z ... R3/Z GD)")
    zero_all_ac_ranges_then_return_to_dc(dtm)

    print("\n4. Restoring initial range and mode")
    if initial_mode is MeasurementMode.AC:
        dtm.set_ac_mode()
    else:
        dtm.set_dc_mode()
    try:
        dtm.set_range(initial_range)
        time.sleep(POST_RANGE_CHANGE_SLEEP_SECONDS)
    except FixedRangeProbeError:
        pass  # Probe is single-range; nothing to restore.

    print("\n5. Final state")
    show_current_state(dtm)

    print("\nWalkthrough complete.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Walk through DTM-151-S manual §4.5.4 on a real instrument.",
    )
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Read timeout per request in seconds (default 1.0)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually issue the state-changing commands. Without this flag the "
        "script only reads current range/mode/field (safe preview).",
    )
    args = parser.parse_args()

    with SerialTransport(args.port, baudrate=args.baud, timeout=args.timeout) as transport:
        dtm = DTM151Serial(Group3Protocol(transport))
        if args.execute:
            execute_walkthrough(dtm)
        else:
            preview(dtm)
            print(
                "\n(preview only — re-run with --execute to perform the "
                "zero-all-ranges sweep and AC/DC mode changes.)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
