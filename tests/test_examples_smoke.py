"""Smoke tests for ``examples/`` against a simulated G3CL loop link.

The examples are the SDK's hardware-facing documentation, and the connect
path they share is exactly what broke over a fiber-optic (FTR) link: the
loop retransmits every command back to the host ahead of its reply (manual
§4.5.1, page 4-7), so an example that does not probe for that reads its own
command as the first reply.

These tests install a fake ``serial`` module whose device echoes each
command verbatim — the loop's behaviour — and run the terminating examples
end to end through ``SerialTransport``. Examples that need a GUI
(``live_plot``) or run until interrupted (``log_stream``) are covered only
for argument parsing; their connect path is byte-identical to the ones
exercised here.

No real serial port is touched (see ``.claude/rules/testing-with-faketransport.md``).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Replies keyed by the command the device received, terminator stripped.
# Values are the on-wire reply including its terminator.
_REPLIES: dict[bytes, bytes] = {
    # Connect-time probes issued by identify(): Ctrl-D (DIP switches) and
    # Ctrl-B (baud switch). DIP value is the factory default with S2-4 off.
    b"\x04": b" 0110001100000000\r",
    b"\x02": b" E\r",
    b"F": b" 1.2345T\r",
    b"T": b" 23.50C\r",
}


class _LoopSerial:
    """``serial.Serial`` stand-in that behaves like a device on a G3CL loop.

    Every payload written is rippled back byte-for-byte (terminator
    included) before the reply, which is what puts an unprepared host out
    of step.

    With ``echo_on`` the device *also* echoes, reproducing the configuration
    bench-observed on 2026-07-26: control bytes come back cleanly twice, but
    the echoed copy of an ASCII command is corrupted (``F`` returned as
    ``|``). Only the ``SE0`` in ``identify()`` makes such a link usable —
    which is why the examples call ``identify()`` and not the bare probe.
    """

    is_open = True

    def __init__(self, echo_on: bool = False, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.echo_on = echo_on
        self.written: bytearray = bytearray()
        self._rx: bytearray = bytearray()
        self.timeout: float | None = kwargs.get("timeout")
        self.closed = False

    def reset_input_buffer(self) -> None:
        self._rx.clear()  # a real flush discards; the examples must survive it

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        self._rx.extend(data)  # the loop ripple, byte-for-byte
        command = data.rstrip(b"\r\n")
        if self.echo_on:
            # Second copy. Control bytes echo verbatim; ASCII echoes come
            # back mangled — one corrupted byte, then the device resyncs.
            self._rx.extend(command if command == data else b"|")
        if command == b"SE0":
            self.echo_on = False  # the repair the examples rely on
        if command in _REPLIES:
            self._rx.extend(_REPLIES[command])
        else:  # a setter — bare-terminator ack, per bench observation
            self._rx.extend(b"\n")
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        if not self._rx:
            return b""
        take = self._rx[:n]
        del self._rx[:n]
        return bytes(take)

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _install_loop_serial(
    monkeypatch: pytest.MonkeyPatch, echo_on: bool = False
) -> types.ModuleType:
    mod = types.ModuleType("serial")
    mod.SerialException = type("SerialException", (Exception,), {})  # type: ignore[attr-defined]
    instances: list[_LoopSerial] = []

    def _factory(**kwargs: Any) -> _LoopSerial:
        inst = _LoopSerial(echo_on=echo_on, **kwargs)
        instances.append(inst)
        return inst

    mod.Serial = _factory  # type: ignore[attr-defined]
    mod.instances = instances  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial", mod)
    return mod


@pytest.fixture
def loop_serial(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``serial`` module backed by :class:`_LoopSerial`."""
    return _install_loop_serial(monkeypatch)


@pytest.fixture
def loop_serial_with_echo(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A loop link whose device also echoes — the corrupted-ASCII case."""
    return _install_loop_serial(monkeypatch, echo_on=True)


def _load(name: str) -> Any:
    """Import an example script by path (``examples/`` is not a package).

    The module is registered in ``sys.modules`` *before* it is executed, and
    removed afterwards. Executing it unregistered looks equivalent and is not:
    anything that resolves its own module at class-creation time fails, and
    the failure surfaces as an unrelated-looking error from deep inside the
    stdlib. ``@dataclass`` under ``from __future__ import annotations`` is the
    case that found this — it looks itself up in ``sys.modules`` to turn its
    string annotations back into types, and raised ``'NoneType' object has no
    attribute '__dict__'`` from ``dataclasses.py``. ``typing.get_type_hints``,
    pickling and ``super()`` in some forms need it for the same reason.

    The entry is left in place on success, deliberately. The module object
    outlives this call — callers hold it and invoke its functions — and
    anything resolving the module lazily (``get_type_hints`` at call time,
    say) would then fail exactly as it did at import time. A later ``_load``
    of the same example simply replaces the entry. It is removed only when
    execution raises, so a half-initialised module is never left for another
    test to import.
    """
    spec = importlib.util.spec_from_file_location(f"_example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


ALL_EXAMPLES = [
    "read_field",
    "read_temperature",
    "run_vi_script",
    "dtm151_walkthrough",
    "log_stream",
    "live_plot",  # imports matplotlib lazily, so --help works without the extra
    "probe_setter_replies",
]


class TestExampleLoader:
    """The loader is test scaffolding, but examples are written against it."""

    @pytest.mark.parametrize("name", ALL_EXAMPLES)
    def test_module_is_registered_while_executing(self, name: str) -> None:
        """An example must be able to find itself in ``sys.modules`` on import.

        Without this, any example using ``@dataclass`` (or anything else that
        resolves its own module at class-creation time) fails to import, with
        an error pointing at the stdlib rather than at the loader. Asserting
        the registration directly says what the requirement is; asserting only
        that the examples happen to import would pass again the moment someone
        removed the last dataclass.
        """
        recorded: list[bool] = []
        real_exec = importlib.machinery.SourceFileLoader.exec_module

        target = f"_example_{name}"

        def spy(self: Any, module: Any) -> None:
            # Only the example itself. The patch is process-wide, so without
            # this filter any source import the example triggers is recorded
            # too - and the assertion below would then report the opposite of
            # the truth the moment an example grew a top-level third-party
            # import.
            if module.__name__ == target:
                recorded.append(sys.modules.get(module.__name__) is module)
            real_exec(self, module)

        with mock.patch.object(
            importlib.machinery.SourceFileLoader, "exec_module", spy
        ):
            _load(name)
        assert recorded == [True], (
            f"{name} was executed without being registered in sys.modules"
        )

    def test_failed_import_leaves_nothing_registered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that raises while executing must not stay in ``sys.modules``.

        Otherwise the next import of that name gets a half-initialised module
        back instead of re-running it, and the resulting failure points
        anywhere but here.

        This replaces a guard that could not fail. It asserted that
        ``_load("read_field")`` left nothing new in ``sys.modules``, but
        earlier tests in the file load that example first, so the name was
        already present and the set difference was empty whatever the loader
        did — and the loader does keep successful imports registered anyway,
        so the guard was asserting the opposite of the intended behaviour.
        """
        broken = tmp_path / "kaboom.py"
        broken.write_text("raise RuntimeError('deliberate')\n", encoding="utf-8")
        monkeypatch.setitem(globals(), "EXAMPLES", tmp_path)

        with pytest.raises(RuntimeError, match="deliberate"):
            _load("kaboom")
        assert "_example_kaboom" not in sys.modules


class TestExampleArguments:
    @pytest.mark.parametrize("name", ALL_EXAMPLES)
    def test_echo_option_is_offered(
        self, name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every example must expose --echo so a loop link can be driven."""
        module = _load(name)
        monkeypatch.setattr(sys, "argv", [name, "--help"])
        with pytest.raises(SystemExit) as excinfo:
            module.main()
        assert excinfo.value.code == 0
        assert "--echo {auto,on,off}" in capsys.readouterr().out


class TestProbeSetterRepliesRestoresState:
    """The sweep changes device state, so restoring it is its contract.

    The review that prompted these found `_snapshot`/`_restore` with no
    coverage at all: the example was only ever exercised with ``--help``.
    """

    def test_restores_every_setting_it_disturbs(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        module = _load("probe_setter_replies")
        monkeypatch.setattr(
            sys,
            "argv",
            # A short error window: these tests are about restoring state, not
            # about how long a setter is given to answer, and the default
            # 0.2s x 16 commands is pure sleeping.
            ["probe_setter_replies", "COM_FAKE", "--error-window", "0.01"],
        )
        module.main()
        out = capsys.readouterr().out

        assert "state before :" in out
        assert "state after  :" in out
        assert "FAILED to restore" not in out

        # Assert against the restore report, not the snapshot line. Checking
        # that "filter" appears somewhere in the output passes on the strength
        # of the "state before" line alone, so it would still pass with the
        # restore removed entirely - which is the whole behaviour under test.
        #
        # Every setting must be *accounted for*, restored or not. The loop
        # simulator answers neither IR nor IG nor ID, so here they all come
        # back unreadable - which is the case worth pinning down, because a
        # silent skip leaves the device on whatever the sweep last set (R3,
        # i.e. 3.0 T) while looking like the bug was fixed. The happy path is
        # covered directly in TestProbeSetterRestoreLogic.
        report = out.split("restoring device state:", 1)[1].split("state after", 1)[0]
        for setting in ("range", "measurement mode", "acquisition mode",
                        "digital filter", "send-units"):
            assert setting in report, (
                f"{setting!r} is disturbed by the sweep but the restore report "
                f"never mentions it:\n{report}"
            )

    def test_reports_rather_than_guesses_when_the_suffix_survives_su0(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A DIP S2-6 device must not be issued SU1 on a hunch.

        A unit suffix means ``SU1`` *or* DIP S2-6. If it survives the sweep's
        closing ``SU0`` it was S2-6 all along, and ``SU``'s prior value cannot
        be known - so restoring "send-units=on" would change state on exactly
        the devices this is meant to protect. The simulator always answers
        with a unit, which is that case.
        """
        module = _load("probe_setter_replies")
        monkeypatch.setattr(
            sys,
            "argv",
            # A short error window: these tests are about restoring state, not
            # about how long a setter is given to answer, and the default
            # 0.2s x 16 commands is pure sleeping.
            ["probe_setter_replies", "COM_FAKE", "--error-window", "0.01"],
        )
        module.main()
        out = capsys.readouterr().out

        assert "send-units not restorable" in out
        assert "DIP S2-6" in out
        assert "restored send-units=on" not in out

    def test_restores_even_when_the_sweep_raises(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A sweep that dies halfway is when restoring matters most."""
        module = _load("probe_setter_replies")
        monkeypatch.setattr(
            sys,
            "argv",
            # A short error window: these tests are about restoring state, not
            # about how long a setter is given to answer, and the default
            # 0.2s x 16 commands is pure sleeping.
            ["probe_setter_replies", "COM_FAKE", "--error-window", "0.01"],
        )

        boom = RuntimeError("sweep exploded")

        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise boom

        monkeypatch.setattr(module, "_probe", explode)
        with pytest.raises(RuntimeError, match="sweep exploded"):
            module.main()

        out = capsys.readouterr().out
        assert "restoring device state:" in out, (
            "a sweep that raised left the device as the sweep had set it"
        )


class _StubDevice:
    """A DTM that answers the four queries the snapshot makes.

    The loop simulator answers none of IR/IG/ID, so it can only exercise the
    unreadable path. This covers the other one, and records what was sent so
    the restore can be checked by what it *did* rather than what it printed.
    """

    def __init__(
        self,
        range_index: int,
        measurement: Any,
        acquisition: Any,
        filter_enabled: bool,
        unit: Any,
    ) -> None:
        self._range = range_index
        self._measurement = measurement
        self._acquisition = acquisition
        self._filter = filter_enabled
        self._unit = unit
        self.calls: list[tuple[str, Any]] = []

    # --- queries ---
    def get_range(self) -> int:
        return self._range

    def get_filter_enabled(self) -> bool:
        return self._filter

    def get_status(self) -> Any:
        return types.SimpleNamespace(
            measurement=self._measurement, acquisition=self._acquisition
        )

    def read_field(self) -> Any:
        return types.SimpleNamespace(unit=self._unit)

    # --- setters ---
    def set_range(self, value: int) -> None:
        self.calls.append(("set_range", value))

    def set_ac_mode(self) -> None:
        self.calls.append(("set_ac_mode", None))

    def set_dc_mode(self) -> None:
        self.calls.append(("set_dc_mode", None))

    def set_triggered_mode(self) -> None:
        self.calls.append(("set_triggered_mode", None))

    def set_continuous_mode(self) -> None:
        self.calls.append(("set_continuous_mode", None))

    def set_filter_enabled(self, value: bool) -> None:
        self.calls.append(("set_filter_enabled", value))
        self._filter = value

    def set_send_units(self, value: bool) -> None:
        self.calls.append(("set_send_units", value))


class TestProbeSetterRestoreLogic:
    """_snapshot/_restore against a device that answers, which the simulator does not."""

    def test_restores_the_exact_state_it_found(self) -> None:
        """The state the sweep would have wrecked is the state that comes back.

        Range 1 / AC / triggered / filter on is chosen because every one of
        those differs from where the sweep leaves the device (R3, DC,
        continuous, filter off), so a restore that silently did nothing would
        be indistinguishable from one that worked if the values matched.
        """
        from group3 import AcquisitionMode, MeasurementMode, Unit

        module = _load("probe_setter_replies")
        dev = _StubDevice(
            range_index=1,
            measurement=MeasurementMode.AC,
            acquisition=AcquisitionMode.TRIGGERED,
            filter_enabled=True,
            unit=Unit.TESLA,
        )
        before = module._snapshot(dev)
        assert before.range_index == 1
        assert before.filter_enabled is True
        assert before.has_unit_suffix is True

        dev.calls.clear()
        module._restore(dev, before)
        assert ("set_range", 1) in dev.calls
        assert ("set_ac_mode", None) in dev.calls
        assert ("set_triggered_mode", None) in dev.calls
        assert ("set_filter_enabled", True) in dev.calls

    def test_restores_su1_when_the_suffix_vanishes_under_su0(self) -> None:
        """The one case where SU1 can be attributed, so the one where it is sent."""
        from group3 import AcquisitionMode, MeasurementMode, Unit

        module = _load("probe_setter_replies")
        dev = _StubDevice(
            range_index=0,
            measurement=MeasurementMode.DC,
            acquisition=AcquisitionMode.CONTINUOUS,
            filter_enabled=False,
            unit=Unit.TESLA,
        )
        before = module._snapshot(dev)
        # The sweep ends on SU0; model that by dropping the suffix.
        dev._unit = Unit.UNKNOWN
        dev.calls.clear()
        lines = module._restore(dev, before)

        assert ("set_send_units", True) in dev.calls
        assert any("restored send-units=on" in ln for ln in lines)

    def test_does_not_send_su1_when_the_suffix_is_from_the_dip_switch(self) -> None:
        """A suffix that survives SU0 is S2-6's, and SU's prior value is unknowable.

        Sending SU1 on that hunch changes state on exactly the devices this is
        meant to leave alone.
        """
        from group3 import AcquisitionMode, MeasurementMode, Unit

        module = _load("probe_setter_replies")
        dev = _StubDevice(
            range_index=0,
            measurement=MeasurementMode.DC,
            acquisition=AcquisitionMode.CONTINUOUS,
            filter_enabled=False,
            unit=Unit.TESLA,
        )
        before = module._snapshot(dev)
        dev.calls.clear()  # suffix still present after the sweep
        lines = module._restore(dev, before)

        assert not any(call[0] == "set_send_units" for call in dev.calls)
        assert any("not restorable" in ln for ln in lines)


class TestExamplesOverLoopLink:
    """Run the terminating examples end to end against the loop simulator."""

    def test_read_field(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        module = _load("read_field")
        monkeypatch.setattr(sys, "argv", ["read_field", "COM_FAKE"])
        module.main()
        out = capsys.readouterr().out
        # The probe recognised the loop and reported it; SE0 went out and the
        # command still came back, so it is the loop rather than S2-4 echo.
        assert "commands returned by the G3CL loop" in out
        assert "field  = +1.234500 T" in out

    def test_read_field_echo_off_hits_the_diagnostic(
        self, loop_serial: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--echo off on a loop link fails with the named cause, not a parser error."""
        from group3 import ProtocolError

        module = _load("read_field")
        monkeypatch.setattr(sys, "argv", ["read_field", "COM_FAKE", "--echo", "off"])
        with pytest.raises(ProtocolError, match="returned verbatim"):
            module.main()

    def test_read_field_on_loop_with_echo_on(
        self,
        loop_serial_with_echo: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The configuration that detection alone cannot survive.

        Loop ripple *and* S2-4 echo: the echoed ASCII copy is corrupted, so
        the example only works because ``--echo auto`` runs ``identify()``,
        whose ``SE0`` turns the echo off before any reading is requested.
        """
        module = _load("read_field")
        monkeypatch.setattr(sys, "argv", ["read_field", "COM_FAKE"])
        module.main()
        out = capsys.readouterr().out
        assert "commands returned by the G3CL loop" in out
        assert "field  = +1.234500 T" in out

    def test_read_field_bare_probe_would_not_survive_echo_on(
        self,
        loop_serial_with_echo: types.ModuleType,
    ) -> None:
        """Guards the reason for the example change: the bare probe is not enough.

        If someone swaps ``identify()`` back to ``detect_command_echo()``,
        this is what the user hits on the documented FTR + S2-4 setup.
        """
        from group3 import DTM151Serial, Group3Protocol, ProtocolError, SerialTransport

        with SerialTransport("COM_FAKE", timeout=0.2) as transport:
            dtm = DTM151Serial(Group3Protocol(transport))
            assert dtm.detect_command_echo() is True  # detection itself is fine
            with pytest.raises(ProtocolError):
                dtm.read_field()  # ...but the corrupted echo breaks the read

    def test_read_temperature(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        module = _load("read_temperature")
        monkeypatch.setattr(sys, "argv", ["read_temperature", "COM_FAKE"])
        assert module.main() == 0
        assert "+23.50" in capsys.readouterr().out

    def test_run_vi_script(
        self,
        loop_serial: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        script = tmp_path / "script.txt"
        script.write_text("F\n", encoding="utf-8")
        module = _load("run_vi_script")
        monkeypatch.setattr(sys, "argv", ["run_vi_script", "COM_FAKE", str(script)])
        assert module.main() == 0
        assert "F -> ' 1.2345T'" in capsys.readouterr().out
