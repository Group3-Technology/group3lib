"""Run a LabVIEW-style DTM-151 script file.

The LabVIEW application ships script files like ``vi/Standard.txt`` whose
payload is a concatenated command string such as ``SU1IRID``. This example reads
one of those files, executes it via :meth:`group3.DTM151Serial.run_script`, and
prints each command plus any reply text.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from group3 import DTM151Serial, Group3Protocol, SerialTransport


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a LabVIEW-style DTM-151 script file.")
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("script", help="Path to script file, e.g. vi/Standard.txt")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument("--timeout", type=float, default=1.0, help="Read timeout in seconds")
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

    script_text = Path(args.script).read_text(encoding="utf-8")
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
        for result in dtm.run_script(script_text):
            if result.reply is None:
                print(result.command)
            else:
                print(f"{result.command} -> {result.reply!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
