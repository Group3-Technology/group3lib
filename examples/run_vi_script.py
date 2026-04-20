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
    parser.add_argument("--timeout", type=float, default=1.0, help="Read timeout in seconds")
    args = parser.parse_args()

    script_text = Path(args.script).read_text(encoding="utf-8")
    with SerialTransport(args.port, baudrate=args.baud, timeout=args.timeout) as transport:
        dtm = DTM151Serial(Group3Protocol(transport))
        for result in dtm.run_script(script_text):
            if result.reply is None:
                print(result.command)
            else:
                print(f"{result.command} -> {result.reply!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
