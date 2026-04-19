# group3lib

A typed Python driver library for Group3 Technology digital teslameters.

**v0.1 supports the DTM-151-S (serial variant).** The architecture is deliberately
layered (transport / protocol / session / model) so that future models — DTM-152,
DTM-333, HTM-121, etc. — slot in without rewriting the core.

## Install

Python 3.10 or later. The core package has **no required runtime dependencies** —
`pyserial` lives behind the `[serial]` extra so tests and custom transports work
without it.

### From a released wheel

Download `group3lib-<version>-py3-none-any.whl` from the project's distribution
channel (GitHub Release, internal index, shared drive, etc.) and install it
directly:

```bash
# Core library only (no pyserial):
pip install group3lib-0.1.0-py3-none-any.whl

# With pyserial for real RS-232 / fiber-optic hardware:
pip install "group3lib-0.1.0-py3-none-any.whl[serial]"
```

Point `pip` at a URL or a directory instead if that's how the wheels are
distributed:

```bash
pip install https://example.com/downloads/group3lib-0.1.0-py3-none-any.whl
pip install --find-links ./dist "group3lib[serial]"
```

### From source

```bash
git clone <repo-url> group3lib
cd group3lib
pip install -e ".[serial]"        # editable install with pyserial
pip install -e ".[all]"           # editable install with pyserial + dev tools
```

## Building a wheel for distribution

The project uses the standard PEP 517 build path via [`build`][pypa-build].

```bash
pip install build               # one-off, into your dev environment
python -m build                 # runs an isolated build
```

[pypa-build]: https://pypa-build.readthedocs.io/

This produces two files under `dist/` (which is already gitignored):

```
dist/
├── group3lib-0.1.0-py3-none-any.whl     # the wheel — what users install
└── group3lib-0.1.0.tar.gz               # sdist — fallback when no wheel fits
```

The wheel tag `py3-none-any` means it installs on any Python 3 interpreter on
any OS — there is no native code to compile.

**Release checklist:**

1. Bump the `version` field in [`pyproject.toml`](pyproject.toml); wheels with
   the same version cannot be re-published.
2. Run `pytest -q && ruff check src tests && mypy --strict src tests` — all
   must be green before building.
3. Remove previous artefacts with `rm -rf dist/ build/` so stale versions
   don't get uploaded by mistake.
4. Build: `python -m build`.
5. Smoke-test the wheel in a clean venv:
   ```bash
   python -m venv /tmp/g3-verify
   /tmp/g3-verify/bin/pip install "dist/group3lib-<version>-py3-none-any.whl[serial]"
   /tmp/g3-verify/bin/python -c "import group3; print(group3.__version__)"
   rm -rf /tmp/g3-verify
   ```
6. Publish the wheel (and optionally the sdist) to your distribution channel:
   - **GitHub Release:** attach `dist/*` to the tag.
   - **Private index** (`devpi`, `pypiserver`, AWS CodeArtifact, etc.): push
     via `twine upload --repository <name> dist/*`.
   - **Public PyPI:** `pip install twine && twine upload dist/*` (requires an
     unclaimed package name and a PyPI API token).

## Quickstart

Read the current field from a DTM-151-S on a local USB-serial port:

```python
from group3 import DTM151Serial, Group3Protocol, SerialTransport

with SerialTransport("/dev/cu.usbserial-1") as transport:
    dtm = DTM151Serial(Group3Protocol(transport))
    reading = dtm.read_field()
    print(f"{reading.value} {reading.unit.value}")
```

Set a range, zero, and take a triggered measurement:

```python
dtm.set_range(2)          # 1.2 T full-scale
dtm.zero()
dtm.set_triggered_mode()
reading = dtm.trigger()   # sends V, waits 175 ms, sends F
```

## G3CL multi-drop

Up to 31 Group3 devices share a single G3CL loop, addressed 0..30:

```python
from group3 import G3CLSession, Group3Protocol, SerialTransport

with SerialTransport("/dev/cu.usbserial-1") as transport:
    session = G3CLSession(Group3Protocol(transport))

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
| DTM-151 | S (serial) | ✅ v0.1 |
| DTM-151 | G (IEEE-488) | ❌ out of scope |
| DTM-152 | S | 🛣️ roadmap |
| DTM-333 | S | 🛣️ roadmap |

To extend to a new model, see [`docs/extending.md`](docs/extending.md).

## Testing

Tests cover both the in-memory `FakeTransport` (protocol/model/session) and the
pyserial path (via a fake `serial.Serial` injected through `monkeypatch`). No
real serial port is required:

```bash
pytest -q
ruff check src tests
mypy --strict src tests
```

Every public method on `DTM151Serial` has a **golden-transcript test** that pins
the exact bytes sent on the wire against Table 9 of the manual. If you change a
command and the test doesn't change too, it almost certainly means your change
did nothing or broke something. The `SerialTransport` tests separately verify
every DIP-switch-documented reply terminator (CR, LF, CR+LF, LF+CR) plus
timeout and error-wrapping behaviour.

Real-hardware smoke tests are manual, via `examples/read_field.py`, and are
not part of the automated suite.

## Assumptions / manual-dependent TODOs

The DTM-151-S manual (`manuals/DTM-151-S Manual_v7.1.pdf`) is the ground truth.
Where a command's exact syntax or reply format is not verifiable from the
visible pages of Table 9 (pages 4-10 / 4-11 render as images that do not
extract cleanly), we've marked code with `TODO(manual §x.y)` and kept
implementation narrow. Resolve these against hardware or a clearer manual copy:

- **`IZ` reply format** (`models/dtm151.py::get_zero_offset`) — assumed plain
  decimal. May actually be mantissa/exponent like `IC`.
- **`IY` reply format** (`models/dtm151.py::get_filter_window`) — assumed plain
  decimal.
- **`Yn` numeric format** (`protocol/commands.py::y_set_filter_window`) — sent
  as a plain decimal (`repr(float)`). The manual's accepted formats are not
  explicit.
- **`IC` reply delimiter** (`models/dtm151.py::get_calibration`) — Table 9
  notes "mantissa and exponent" but the delimiter is not visible. Assumed a
  standard exponential float.
Other inherited assumptions worth confirming on your unit's DIP-switch settings
(manual section 3.6):

- **Serial params**: default `9600-8-N-1`, no flow control. The DTM-151-S is
  fully DIP-switch-configurable between 50 and 19200 baud.
- **Response terminator**: CR by default (S2-2); LF, CR+LF, and LF+CR are
  accepted by `codec.strip_terminators`.

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
