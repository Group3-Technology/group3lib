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

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

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
    """Import an example script by path (``examples/`` is not a package)."""
    spec = importlib.util.spec_from_file_location(f"_example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
