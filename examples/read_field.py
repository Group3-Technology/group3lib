"""Minimal example — read a field value from a DTM-151-S on a local serial port.

Usage:
    python examples/read_field.py /dev/cu.usbserial-1

Defaults to the DTM-151-S factory configuration (9600 7E2, CR terminator).
Override via ``--baud``, ``--bytesize``, ``--parity``, ``--stopbits`` for
non-default DIP-switch settings.
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
        reading = dtm.read_field()
        print(f"field  = {reading.value:+.6f} {reading.unit.value}")
        print(f"raw    = {reading.raw!r}")


if __name__ == "__main__":
    main()
