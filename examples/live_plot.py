"""Live-plotting example — streams field at 10 Hz and polls temperature at 1 Hz.

Demonstrates the ``SM1`` streaming API (:meth:`group3.DTM151Serial.stream_field`)
with interleaved request/reply commands via :meth:`FieldStream.paused`. A
matplotlib figure shows two synchronised traces — field on top, temperature
below — updated in place as samples arrive.

Requires the ``[plot]`` extra::

    pip install "group3lib[serial,plot]"

Usage::

    python examples/live_plot.py /dev/cu.usbserial-1

Ctrl-C cleanly exits: the context manager re-issues ``SM0`` and restores the
device's original ``Kn`` sampling interval.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from typing import TYPE_CHECKING

from group3 import (
    BadTemperatureReadingError,
    DTM151Serial,
    Group3Protocol,
    NoTemperatureProbeError,
    SerialTransport,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D


# Keep the last ~30 s of field samples at 10 Hz = 300 points, and ~60 s of
# temperature samples at 1 Hz = 60 points. Deques cap automatically.
FIELD_BUFFER_SIZE = 300
TEMP_BUFFER_SIZE = 60

# K0 = the device's internal maximum rate (10 Hz). Other useful values:
# 1 = 1 Hz, 60 = once per minute. The device only accepts integer seconds.
FIELD_SAMPLING_INTERVAL_S = 0
TEMP_POLL_INTERVAL_S = 1.0


def _setup_figure() -> tuple[Figure, Axes, Axes, Line2D, Line2D]:
    """Create the two-axis matplotlib figure. Returns (fig, ax_field, ax_temp, line_field, line_temp)."""
    import matplotlib.pyplot as plt

    fig, (ax_field, ax_temp) = plt.subplots(2, 1, figsize=(8, 6), sharex=False)
    fig.suptitle("DTM-151-S — live field and temperature")

    (line_field,) = ax_field.plot([], [], color="tab:blue", label="field")
    ax_field.set_ylabel("field")
    ax_field.grid(True, alpha=0.3)
    ax_field.legend(loc="upper right")

    (line_temp,) = ax_temp.plot([], [], color="tab:red", marker="o", label="probe temperature")
    ax_temp.set_xlabel("time since start (s)")
    ax_temp.set_ylabel("temperature (°C)")
    ax_temp.grid(True, alpha=0.3)
    ax_temp.legend(loc="upper right")

    plt.ion()
    plt.show(block=False)
    return fig, ax_field, ax_temp, line_field, line_temp


def _refresh_axes(ax: Axes, xs: deque[float], ys: deque[float]) -> None:
    """Rescale axes to fit the current data."""
    if not xs:
        return
    ax.set_xlim(xs[0], max(xs[-1], xs[0] + 1.0))
    ymin, ymax = min(ys), max(ys)
    if ymin == ymax:
        ymin -= 1.0
        ymax += 1.0
    margin = 0.05 * (ymax - ymin)
    ax.set_ylim(ymin - margin, ymax + margin)


def run(dtm: DTM151Serial) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "This example requires matplotlib. Install with "
            "pip install 'group3lib[plot]'.",
            file=sys.stderr,
        )
        sys.exit(1)

    fig, ax_field, ax_temp, line_field, line_temp = _setup_figure()

    field_times: deque[float] = deque(maxlen=FIELD_BUFFER_SIZE)
    field_values: deque[float] = deque(maxlen=FIELD_BUFFER_SIZE)
    temp_times: deque[float] = deque(maxlen=TEMP_BUFFER_SIZE)
    temp_values: deque[float] = deque(maxlen=TEMP_BUFFER_SIZE)

    start = time.monotonic()
    last_temp_poll = start
    # Warm temperature flag — suppresses the 'no temperature probe' spam on
    # non-temp-corrected probes by polling once, giving up gracefully, and
    # never trying again.
    temperature_available = True

    print("Streaming started. Press Ctrl-C to stop.")

    with dtm.stream_field(interval_seconds=FIELD_SAMPLING_INTERVAL_S) as stream:
        for reading in stream:
            now = time.monotonic()
            t = now - start

            field_times.append(t)
            field_values.append(reading.value)

            if temperature_available and (now - last_temp_poll) >= TEMP_POLL_INTERVAL_S:
                with stream.paused():
                    try:
                        temp = dtm.read_temperature()
                    except NoTemperatureProbeError:
                        temperature_available = False
                        print(
                            "Probe is not temperature-corrected — disabling temperature polling.",
                            file=sys.stderr,
                        )
                        temp = None
                    except BadTemperatureReadingError:
                        # Transient sensor fault — skip this tick but keep trying.
                        temp = None
                last_temp_poll = time.monotonic()
                if temp is not None:
                    temp_times.append(t)
                    temp_values.append(temp.value)

            line_field.set_data(list(field_times), list(field_values))
            line_temp.set_data(list(temp_times), list(temp_values))
            _refresh_axes(ax_field, field_times, field_values)
            if temp_times:
                _refresh_axes(ax_temp, temp_times, temp_values)

            fig.canvas.draw_idle()
            plt.pause(0.001)  # yield to the GUI event loop

            if not plt.fignum_exists(fig.number):
                break  # user closed the window


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live plot of DTM-151-S field (10 Hz) and temperature (1 Hz).",
    )
    parser.add_argument("port", help="Serial port, e.g. /dev/cu.usbserial-1 or COM3")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default 9600)")
    parser.add_argument("--bytesize", type=int, default=7, choices=[7, 8])
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=float, default=2, choices=[1, 1.5, 2])
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Per-request read timeout in seconds (default 2.0)",
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
            run(dtm)
        except KeyboardInterrupt:
            print("\nInterrupted — exiting cleanly.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
