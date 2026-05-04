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
    args = parser.parse_args()

    with SerialTransport(
        args.port,
        baudrate=args.baud,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
    ) as transport:
        dtm = DTM151Serial(Group3Protocol(transport))
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
