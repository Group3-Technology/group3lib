# group3lib

A typed Python driver library for Group3 Technology digital teslameters.

**Supports the DTM-151-S (serial variant), including live `SM1` streaming
and probe-temperature reads.** The architecture is deliberately layered
(transport / protocol / session / model) so that future models —
DTM-351, HTM-141, etc. — slot in without rewriting the core.

## Install

Python 3.10 or later. The core package has **no required runtime dependencies** —
`pyserial` lives behind the `[serial]` extra so tests and custom transports work
without it.

### From a released wheel

Tagged versions are published to the project's
[GitHub Releases page](https://github.com/Group3-Technology/group3lib/releases) by the
[`Release`](.github/workflows/release.yml) workflow. Each release attaches a
`group3lib-<version>-py3-none-any.whl` and a matching `.tar.gz` sdist.

Replace `<VERSION>` below with the release tag (e.g. `0.X.Y`) — see the
[Releases page](https://github.com/Group3-Technology/group3lib/releases) for the
latest tag.

```bash
# Core library only (no pyserial):
pip install https://github.com/Group3-Technology/group3lib/releases/download/v<VERSION>/group3lib-<VERSION>-py3-none-any.whl

# With pyserial for real RS-232 / fiber-optic hardware:
pip install "group3lib[serial] @ https://github.com/Group3-Technology/group3lib/releases/download/v<VERSION>/group3lib-<VERSION>-py3-none-any.whl"

# Or download the wheel locally and install it:
pip install ./group3lib-<VERSION>-py3-none-any.whl
pip install "./group3lib-<VERSION>-py3-none-any.whl[serial,plot]"
```

For a pre-release build off a feature branch, every PR's
[`CI`](.github/workflows/ci.yml) run uploads the wheel as a `dist-<sha>`
artifact (14-day retention) — download it from the run's *Summary* page in
the Actions tab.

### From source

```bash
git clone <repo-url> group3lib
cd group3lib
pip install -e ".[serial]"        # editable install with pyserial
pip install -e ".[all]"           # editable install with pyserial + dev tools
```

## Building a wheel for distribution

CI handles this automatically:

- The [`CI`](.github/workflows/ci.yml) workflow runs lint (`ruff`), types
  (`mypy --strict`), and the test matrix (Python 3.10–3.13) on every PR and
  push to `main`, then builds the wheel and uploads it as a `dist-<sha>`
  artifact.
- The [`Release`](.github/workflows/release.yml) workflow runs on `v*` tag
  pushes. It verifies that the tag matches `pyproject.toml`'s `project.version`,
  re-runs the full quality gate, builds the wheel + sdist, and creates a
  GitHub Release with the artifacts attached and auto-generated notes.

The wheel tag `py3-none-any` means it installs on any Python 3 interpreter on
any OS — there is no native code to compile.

**Release flow:**

1. Bump the `version` field in [`pyproject.toml`](pyproject.toml); wheels with
   the same version cannot be re-published.
2. Merge to `main` (CI must be green).
3. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
4. The Release workflow builds and attaches the artifacts to a new GitHub
   Release. If the tag and `pyproject.toml` version disagree, the workflow
   fails fast — bump the version and re-tag.

**Building locally** (rarely needed; useful for smoke-testing before tagging):

```bash
pip install build
python -m build                 # produces dist/*.whl and dist/*.tar.gz
```

[pypa-build]: https://pypa-build.readthedocs.io/

## Quickstart

Read the current field from a DTM-151-S on a local USB-serial port:

```python
from group3 import DTM151Serial, Group3Protocol, SerialTransport

with SerialTransport("/dev/cu.usbserial-1") as transport:
    dtm = DTM151Serial(Group3Protocol(transport))
    dtm.identify()          # connect-time probe — see "Fiber-optic (FTR)" below
    reading = dtm.read_field()
    print(f"{reading.value} {reading.unit.value}")
```

`identify()` is optional on a plain point-to-point RS-232 link with echo off,
and required on anything that returns commands — a fiber-optic/G3CL link, or a
device with DIP S2-4 echo ON. Make it your first call and neither case can
surprise you.

### Fiber-optic (FTR) and echo-enabled links

On a fiber-optic link the host sits on a Group3 Communication Loop, and the loop
retransmits every command back to the host ahead of its reply (manual §4.5.1,
page 4-7). A device with DIP S2-4 echo ON does the same thing over plain RS-232.
Either way the protocol has to expect the returned command. Call `identify()`
once after connecting — it probes, and repairs the link if it needs repairing:

```python
dtm = DTM151Serial(Group3Protocol(transport))
profile = dtm.identify()    # probes; sends SE0 if the device is echoing
reading = dtm.read_field()

profile.command_returned    # True: commands come back before their reply
profile.loop_echo           # True: it is the loop, so SE0 cannot stop it
```

Skip it on such a link and the first reply is your own command coming back; the
SDK raises a `ProtocolError` naming that cause rather than failing downstream in
the parser.

**Use `identify()`, not `detect_command_echo()`, as the connect-time call.** The
latter only *detects* — it reports whether commands come back and configures the
protocol accordingly, which is enough for a loop-only link. It is **not** enough
when the device also has S2-4 echo ON *over* a loop: the command then comes back
twice (manual §3.6, page 3-11), and on real hardware the echoed ASCII copy
arrives corrupted (bench-observed: `F` returned as `|`), so no amount of
prefix-stripping recovers it. Only the `SE0` that `identify()` sends makes such a
link usable — control-byte probes stay clean, which is what lets the repair get
through. `SE0` clears the device's echo but cannot stop the loop, so `identify()`
re-probes afterwards and reports the remaining return as `profile.loop_echo`.

Set a range, zero, and take a triggered measurement:

```python
dtm.set_range(2)          # 1.2 T full-scale
dtm.zero()
dtm.set_triggered_mode()
reading = dtm.trigger()   # sends V, waits 175 ms, sends F
```

Read probe temperature (requires a temperature-corrected probe, LPT/MPT-141
or -231):

```python
temp = dtm.read_temperature()
print(f"{temp.value:+.2f} °C")
```

Enable unit suffixes in numeric replies and inspect a metadata snapshot:

```python
dtm.set_send_units(True)          # SU1
snapshot = dtm.read_metadata_snapshot()
print(snapshot.range_index, snapshot.filter_enabled, snapshot.sampling_interval)
```

## Live streaming (SM1 / Kn)

> **Point-to-point only.** Streaming is not supported on the G3CL addressed
> loop — ``SM1`` replies carry no address tag, so a consumer listening on
> one addressed device would indiscriminately absorb readings from any
> other streaming device on the loop. `dtm.stream_field()` on an
> `AddressedProtocol` raises `NotImplementedError`.

Enable the device's built-in auto-transmit mode (`SM1`) for continuous
acquisition. `stream_field` is a context manager — it sets up `SM1` (and
optionally `Kn` via `interval_seconds`) on entry and restores the prior
state on exit, including draining any reading that was in flight when the
stream stops:

```python
with dtm.stream_field(interval_seconds=0) as stream:   # 0 = device max rate (10 Hz)
    for reading in stream:
        handle(reading)
        if done:
            break
```

`interval_seconds` maps directly to the device's `Kn` command — it's an
integer number of seconds between readings: `0` = max rate (10 Hz
internal), `1` = 1 Hz, `60` = once per minute, up to `65534`. The DTM-151
cannot produce non-integer-second intervals, so the API deliberately
mirrors that restriction rather than silently quantising a `rate_hz` value.

Interleave other commands (like `read_temperature`) without desyncing the
bus by pausing the stream:

```python
with dtm.stream_field(interval_seconds=0) as stream:
    last_temp = 0.0
    for reading in stream:
        plot_field(reading)
        now = time.monotonic()
        if now - last_temp >= 1.0:
            with stream.paused():       # sends SM0, drains tail
                temp = dtm.read_temperature()
            plot_temp(temp)
            last_temp = now             # SM1 re-issued on block exit
```

See [`examples/live_plot.py`](examples/live_plot.py) for a full matplotlib
example that streams field at 10 Hz and temperature at 1 Hz simultaneously
(requires the `[plot]` extra for `matplotlib`).

See [`examples/log_stream.py`](examples/log_stream.py) for a VI-inspired TSV
logger that writes high-rate field rows plus a metadata row every 10 seconds.

## G3CL multi-drop

Up to 31 Group3 devices share a single G3CL loop, addressed 0..30:

```python
from group3 import G3CLSession, Group3Protocol, SerialTransport

with SerialTransport("/dev/cu.usbserial-1") as transport:
    # A multi-drop G3CL is a loop by construction, so commands always come
    # back before their replies — declare it rather than probing. Don't call
    # identify() here: its Ctrl-D/Ctrl-B probes are unaddressed, so every
    # device on the loop would answer at once.
    protocol = Group3Protocol(transport, expect_command_returned=True)
    session = G3CLSession(protocol)

    probe_a = session.device(address=0)
    probe_b = session.device(address=5)
    probe_a.set_triggered_mode()
    probe_b.set_triggered_mode()

    session.broadcast_trigger()   # unaddressed V — all triggered devices fire
    # after >=175 ms, read each in turn:
    print(probe_a.read_field())
    print(probe_b.read_field())
```

Addressing is handled automatically by `session.device(...)` — every command is
prefixed with the `An` designator, which is the conservative behaviour for
unattended lab runs.

## Supported models

| Model | Variant | Status |
| --- | --- | --- |
| DTM-151 | S (serial) | ✅ field, peak, temperature, streaming, G3CL |
| DTM-151 | G (IEEE-488) | ❌ out of scope |
| DTM-152 | S | 🛣️ roadmap |
| DTM-333 | S | 🛣️ roadmap |

To extend to a new model, see [`docs/extending.md`](docs/extending.md).

## Supported commands

Cross-reference: `manuals/DTM-151-S Manual_v7.1.pdf` (Table 9, §4.5–4.7).

| Command | Python API | Notes |
| --- | --- | --- |
| `F` | `dtm.read_field()` | Field reading |
| `P` | `dtm.read_peak()` | Peak-hold field |
| `Q` | `dtm.front_panel_test()` | Front-panel display self-test |
| `EP` | `dtm.erase_peak()` | Reset peak-hold value |
| `T` | `dtm.read_temperature()` | Probe temperature (temp-corrected probes) |
| `Z` / `EZ` | `dtm.zero()` / `dtm.erase_zero()` | Current-range zero |
| `Rn` / `IR` | `dtm.set_range(n)` / `dtm.get_range()` | 0.3 / 0.6 / 1.2 / 3.0 T |
| `GA` / `GD` | `dtm.set_ac_mode()` / `dtm.set_dc_mode()` | Measurement mode |
| `GC` / `GV` | `dtm.set_continuous_mode()` / `dtm.set_triggered_mode()` | Acquisition mode |
| `V` | `dtm.trigger()`, `session.broadcast_trigger()` | Triggered reading |
| `D0` / `D1` / `ID` | `dtm.set_filter_enabled()` / `dtm.get_filter_enabled()` | Digital filter |
| `Jn` / `IJ` | `dtm.set_filter_factor()` / `dtm.get_filter_factor()` | Filter factor |
| `Yn` / `IY` | `dtm.set_filter_window()` / `dtm.get_filter_window()` | Filter window |
| `On` / `IO` / `EO` | `dtm.set_offset()` / `dtm.get_offset()` / `dtm.erase_offset()` | Offset |
| `SCn` / `IC` / `EC` | `dtm.set_calibration()` / `dtm.get_calibration()` / `dtm.erase_calibration()` | Calibration |
| `IL` / `EL` | `dtm.get_scale()` / `dtm.erase_scale()` | Scale factor |
| `IG` | `dtm.get_status()` | DC/AC + continuous/triggered |
| `An` | `session.select(addr)`, `dtm.set_address()` | G3CL addressing |
| `SMn` | `dtm.set_auto_transmit()`, `dtm.stream_field()` | Auto-transmit (streaming) |
| `Kn` / `IK` | `dtm.set_sampling_interval()` / `dtm.get_sampling_interval()` | Streaming rate |
| `SUn` | `dtm.set_send_units()` | Include units in numeric replies |
| `SEn` | `dtm.set_echo()` | Command-byte echo on/off |
| `UFG` / `UFT` | `dtm.set_display_units("G"/"T")` | Front-panel display units |
| `B<text>` | `dtm.display_text(text)` | Show up to 7 chars on front panel |
| `Ln` | `dtm.set_field_scale_for(value)` | Scale: make current reading equal *value* |
| `SLn` | `dtm.set_global_scale(value)` | Set global scale factor directly |
| `SZn` | `dtm.set_zero(value)` | Set explicit zero offset (vs `Z` which uses present reading) |
| `Cn` | `dtm.calibrate(value)` | Live calibration — exact semantics TBC, see docstring |
| `WA` / `WE` / `WZ` | `dtm.read_raw_field_post_adc()` / `_post_cal()` / `_post_zero()` | Raw-field diagnostics |
| `Ctrl-D` | `dtm.identify()` | DIP-switch + baud probe; normalises echo state |
| `Ctrl-B` | (via `identify()`) | Baud-rate switch position; also `dtm.detect_command_echo()` |
| `Ctrl-U` | `dtm.restart()` | Restart firmware; returns banner string |
| `Ctrl-X` | `dtm.reset_to_defaults()` | Reload DIP-defined defaults; clears numerical user settings |

Higher-level helpers built on those commands:

| Helper | Purpose |
| --- | --- |
| `dtm.identify()` | **Connect-time call**: DIP switches, baud-rate switch, and whether commands are returned — repairs an echoing link |
| `dtm.detect_command_echo()` | Detection only — reports a returned-command prefix without repairing it (see above) |
| `dtm.read_metadata_snapshot()` | Temperature + range + filter + `Kn` + `IG` in one call set |
| `dtm.run_script(text)` | Execute LabVIEW-style concatenated command scripts such as `SU1IRID` |

## Testing

Tests cover both the in-memory `FakeTransport` (protocol/model/session) and the
pyserial path (via a fake `serial.Serial` injected through `monkeypatch`). No
real serial port is required:

```bash
pytest -q
ruff check src tests
mypy --strict src
```

The same three commands run in [`CI`](.github/workflows/ci.yml) on every PR
and push to `main`, across Python 3.10, 3.11, 3.12, and 3.13.

Every public method on `DTM151Serial` has a **golden-transcript test** that pins
the exact bytes sent on the wire against Table 9 of the manual. If you change a
command and the test doesn't change too, it almost certainly means your change
did nothing or broke something. The `SerialTransport` tests separately verify
every DIP-switch-documented reply terminator (CR, LF, CR+LF, LF+CR) plus
timeout and error-wrapping behaviour.

Real-hardware smoke tests are manual — see
[`examples/read_field.py`](examples/read_field.py),
[`examples/read_temperature.py`](examples/read_temperature.py),
[`examples/dtm151_walkthrough.py`](examples/dtm151_walkthrough.py),
[`examples/live_plot.py`](examples/live_plot.py),
[`examples/log_stream.py`](examples/log_stream.py),
[`examples/run_vi_script.py`](examples/run_vi_script.py), and
[`examples/probe_setter_replies.py`](examples/probe_setter_replies.py) — and
are not part of the automated suite.

## Assumptions / manual-dependent TODOs

The DTM-151-S manual (`manuals/DTM-151-S Manual_v7.1.pdf`) is the ground truth.
Where a command's exact syntax or reply format is not verifiable from the
visible pages of Table 9 (pages 4-10 / 4-11 render as images that do not
extract cleanly), we've marked code with `TODO(manual §x.y)` and kept
implementation narrow. Resolve these against hardware or a clearer manual copy:

- **`Yn` numeric format** (`protocol/commands.py::y_set_filter_window`) — sent
  as a plain decimal (`repr(float)`). The manual's accepted formats are not
  explicit.
- **`IC` reply delimiter** (`models/dtm151.py::get_calibration`) — Table 9
  notes "mantissa and exponent" but the delimiter is not visible. The bench
  unit returned `' 1.000000'` (plain decimal) at factor 1.0; non-unit
  factors may switch to mantissa+exponent and would need re-verification.
- **`IZ` and `IY` reply formats** — previously listed as TODOs; verified on
  the bench unit as plain decimal (`' 1.55'`, `' 1.00'`).
Other inherited assumptions worth confirming on your unit's DIP-switch settings
(manual section 3.6):

- **Serial params**: factory default is `9600 7E2`, no flow control (verified
  against a bench unit), and `SerialTransport` defaults match. Pass
  `bytesize=8, parity="N", stopbits=1` for a unit reconfigured to 8N1, or use
  the `--bytesize/--parity/--stopbits` flags on the example scripts. The
  DTM-151-S is fully DIP-switch-configurable between 50 and 19200 baud.
- **Response terminator**: documented options are CR, LF, CR+LF, LF+CR
  (S2-2 / S2-3). `SerialTransport` drains all consecutive CR/LF bytes after
  the first terminator, so any documented combination *and* the
  longer-than-documented sequences observed on real hardware (see Empirical
  findings below) are handled transparently.

### Empirical findings (verified against real hardware)

- **Setters ack with a bare terminator, not silence.** The manual (§4.5.2)
  describes setters (`Z`, `Rn`, `GA`, `GD`, `Jn`, `SUn`, `Dn`, `Q`, …) as
  "silent on success", but every setter probed against a bench DTM-151-S at
  9600 7E2 acked with `b'\n'`. `Group3Protocol.send_setter` treats both
  silence and a terminator-only frame as success — see
  [`examples/probe_setter_replies.py`](examples/probe_setter_replies.py) to
  re-run the probe on your own unit. A setter that returns a non-empty,
  non-error reply is still treated as desync (`ProtocolError`).
- **Numeric replies always carry a decimal point** — even when the value is
  integer-valued (e.g. `IK` returns `' 0.'`, `IJ` returns `' 15.0000'`).
  This matches the manual's §4.5.2 rule but contradicts what status-index
  replies like `IR` (`' 3'`) might suggest. `parse_int` accepts both forms.
- **Terminator length varies by message type.** Setter acks use 1 byte
  (`\n`), streaming readings use 2 bytes (`\n\r`), and request-reply
  responses use 3 bytes (`\n\r\n`) on the bench unit's DIP-switch
  configuration. Manual §3.6 only documents 1- and 2-byte forms; the
  transport drains all consecutive terminator bytes after the first to
  cover any combination transparently.

## Layer diagram

```
┌─────────────────────┐
│ DTM151Serial        │  ← public API: read_field, set_range, zero, trigger…
├─────────────────────┤
│ G3CLSession         │  ← optional: addressing, broadcast V
│ AddressedProtocol   │
├─────────────────────┤
│ Group3Protocol      │  ← encode/decode, terminator, raise on device errors
│ codec, parser,      │
│ commands (registry) │
├─────────────────────┤
│ Transport           │  ← SerialTransport (pyserial) or FakeTransport (tests)
└─────────────────────┘
```

## License

MIT — see [LICENSE](LICENSE).
