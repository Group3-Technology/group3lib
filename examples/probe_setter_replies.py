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
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from group3 import (
    DeviceError,
    Group3Protocol,
    ProtocolError,
    SerialTransport,
    TransportError,
)
from group3.protocol import commands


def _safe_setters() -> list[str]:
    """Return a curated list of setter command strings safe to probe."""
    return [
        commands.su_set_send_units(False),
        commands.su_set_send_units(True),
        commands.su_set_send_units(False),  # restore
        commands.D0,
        commands.D1,
        commands.D0,  # restore
        commands.r_set_range(0),
        commands.r_set_range(1),
        commands.r_set_range(2),
        commands.r_set_range(3),
        commands.GA,
        commands.GD,  # restore (DC is the usual default)
        commands.GC,
        commands.GV,
        commands.GC,  # restore
        commands.Q,  # front-panel display self-test (vendor command reference)
    ]


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
        protocol = Group3Protocol(t)
        _probe(protocol, _safe_setters(), error_window=args.error_window)


if __name__ == "__main__":
    main()
