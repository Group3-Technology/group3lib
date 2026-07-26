"""Minimal example — read the probe temperature from a DTM-151-S.

The ``T`` command works only with a temperature-corrected probe (LPT/MPT-141
or LPT/MPT-231). On a single-range or non-temperature-corrected probe the
device replies ``NO TEMPERATURE PROBE``, which this script surfaces as a
:class:`group3.NoTemperatureProbeError`.

Usage::

    python examples/read_temperature.py /dev/cu.usbserial-1

Serial settings default to 9600 7E2 with CR terminator (DTM-151-S factory
default, manual §3.6). Override with ``--baud``, ``--bytesize``, ``--parity``,
``--stopbits`` for different DIP-switch settings.
"""

from __future__ import annotations

import argparse
import sys

from group3 import (
    BadTemperatureReadingError,
    DTM151Serial,
    Group3Protocol,
    NoTemperatureProbeError,
    SerialTransport,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the probe temperature from a DTM-151-S.",
    )
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Read timeout per request in seconds (default 1.0)",
    )
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
            # identify(), not detect_command_echo(): on a loop link whose
            # device also has S2-4 echo on, the echoed ASCII copy comes back
            # corrupted and only identify()'s SE0 step makes the link usable.
            dtm.identify()
        try:
            reading = dtm.read_temperature()
        except NoTemperatureProbeError:
            print(
                "ERROR: the attached probe is not a temperature-corrected type. "
                "Temperature reads require an LPT/MPT-141 or -231 probe.",
                file=sys.stderr,
            )
            return 2
        except BadTemperatureReadingError:
            print(
                "ERROR: the probe's temperature sensor is giving an invalid reading. "
                "Check the probe cable and connections.",
                file=sys.stderr,
            )
            return 3

    print(f"temperature = {reading.value:+.2f} {reading.unit.value}")
    print(f"raw         = {reading.raw!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
