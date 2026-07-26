"""Minimal example — read a field value from a DTM-151-S on a local serial port.

Usage:
    python examples/read_field.py /dev/cu.usbserial-1

Defaults to the DTM-151-S factory configuration (9600 7E2, CR terminator).
Override via ``--baud``, ``--bytesize``, ``--parity``, ``--stopbits`` for
non-default DIP-switch settings.

Over a fiber-optic (FTR) link the host sits on a Group3 Communication Loop,
which ripples every command back before its reply (manual §4.5.1, page 4-7) —
as does a device with DIP S2-4 echo ON. ``--echo auto`` (the default) probes
for that and configures the protocol accordingly.
"""

from __future__ import annotations

import argparse

from group3 import DTM151Serial, Group3Protocol, SerialTransport


def main() -> None:
    parser = argparse.ArgumentParser(description="Read a field value from a DTM-151-S.")
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument("--timeout", type=float, default=1.0, help="Read timeout in seconds")
    parser.add_argument(
        "--echo",
        choices=["auto", "on", "off"],
        default="auto",
        help=(
            "Whether commands come back before their reply — true on a G3CL "
            "loop (fiber-optic/FTR link) or with DIP S2-4 echo ON. "
            "'auto' (default) probes the device and turns echo off if it "
            "finds it; 'on' declares a returning link without probing or "
            "repairing, so it fails if the device is also echoing; 'off' "
            "declares that nothing comes back. Use 'auto' unless you know "
            "the link state"
        ),
    )
    args = parser.parse_args()

    with SerialTransport(
        args.port,
        baudrate=args.baud,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
    ) as transport:
        protocol = Group3Protocol(transport, expect_command_returned=args.echo == "on")
        dtm = DTM151Serial(protocol)
        if args.echo == "auto":
            # identify(), not detect_command_echo(): detection alone leaves a
            # loop link whose device also echoes (S2-4) unusable, because the
            # echoed ASCII copy comes back corrupted. identify() sends SE0 and
            # re-probes, which is what makes the link readable.
            profile = dtm.identify()
            if not profile.command_returned:
                state = "none"
            elif profile.loop_echo:
                state = "commands returned by the G3CL loop (S2-4 echo now off)"
            else:
                state = "commands returned before reply"
            print(f"echo   = {state}")
        reading = dtm.read_field()
        print(f"field  = {reading.value:+.6f} {reading.unit.value}")
        print(f"raw    = {reading.raw!r}")


if __name__ == "__main__":
    main()
