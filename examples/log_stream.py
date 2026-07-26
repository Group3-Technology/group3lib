"""VI-style TSV logger for DTM-151 field streaming plus periodic metadata.

The LabVIEW DTM-151 application logs two record types:

1. high-rate field readings
2. lower-rate metadata snapshots (temperature, range, filter state, Kn)

This example mirrors that pattern using the public Python API. Field rows are
written continuously from ``SM1`` streaming, while every ``--meta-every``
seconds the stream is paused briefly so a metadata snapshot can be taken without
desynchronising the serial link.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from group3 import DTM151Serial, Group3Protocol, SerialTransport

FIELD_HEADER = ["Date", "Time", "Type", "Field", "FieldUnit"]
META_HEADER = [
    "Date",
    "Time",
    "Type",
    "Temperature",
    "TemperatureUnit",
    "Range",
    "Filter",
    "Kn",
    "Mode",
    "Acquisition",
]


def _now_parts() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S.%f")[:-3]


def _write_field(writer: csv.writer, dtm: DTM151Serial, interval_seconds: int, meta_every: float) -> None:
    last_meta = time.monotonic()
    writer.writerow(FIELD_HEADER)
    with dtm.stream_field(interval_seconds=interval_seconds) as stream:
        for reading in stream:
            date_text, time_text = _now_parts()
            writer.writerow([date_text, time_text, "FIELD", f"{reading.value:.6g}", reading.unit.value])

            now = time.monotonic()
            if now - last_meta < meta_every:
                continue

            with stream.paused():
                snapshot = dtm.read_metadata_snapshot()
            writer.writerow(META_HEADER)
            writer.writerow(
                [
                    date_text,
                    time_text,
                    "META",
                    "?" if snapshot.temperature is None else f"{snapshot.temperature.value:.6g}",
                    "?" if snapshot.temperature is None else snapshot.temperature.unit.value,
                    snapshot.range_index,
                    int(snapshot.filter_enabled),
                    snapshot.sampling_interval,
                    snapshot.status.measurement.value,
                    snapshot.status.acquisition.value,
                ]
            )
            writer.writerow(FIELD_HEADER)
            last_meta = now


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Log DTM-151 field stream and periodic metadata to a TSV file.",
    )
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("output", help="Path to TSV file to create")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument("--timeout", type=float, default=2.0, help="Read timeout in seconds")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="Kn streaming interval in seconds; 0 = max rate (10 Hz)",
    )
    parser.add_argument(
        "--meta-every",
        type=float,
        default=10.0,
        help="Seconds between metadata rows (default 10.0)",
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

    output_path = Path(args.output)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
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
                # device also has S2-4 echo on, the echoed ASCII copy comes
                # back corrupted and only identify()'s SE0 step makes the
                # link usable.
                dtm.identify()
            try:
                _write_field(writer, dtm, args.interval_seconds, args.meta_every)
            except KeyboardInterrupt:
                print("\nInterrupted — log closed cleanly.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
