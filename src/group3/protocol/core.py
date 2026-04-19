"""Group3Protocol — ties transport + codec + parser together.

One ``Group3Protocol`` wraps a single :class:`~group3.transport.base.Transport` and
provides two public methods:

* :meth:`send` — for request/reply commands. Encodes, writes, reads until terminator,
  strips the terminator, checks for device-reported errors, returns the cleaned reply.
* :meth:`send_no_reply` — for broadcast commands like ``V`` on the G3CL where no
  reply is expected.

Callers must open the transport themselves (or use ``with protocol.transport: ...``).
Every ``send`` stores the last raw TX and RX bytes on the protocol instance so that
tests and users can inspect them for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass

from group3.protocol import codec
from group3.protocol.parser import check_error
from group3.transport.base import Transport


@dataclass
class _LastExchange:
    tx: bytes = b""
    rx: bytes = b""


class Group3Protocol:
    """Encoded, terminator-aware request/reply layer over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        terminator: bytes = codec.CR,
        timeout: float | None = None,
    ) -> None:
        """
        Args:
            transport: Underlying byte transport (serial, fake, etc.).
            terminator: Bytes to append to every outgoing command. The DTM-151-S
                requires CR on the host→device direction (manual section 4.5.2).
            timeout: Default per-request timeout in seconds. ``None`` means "use the
                transport's default".
        """
        self.transport = transport
        self.terminator = terminator
        self.timeout = timeout
        self._last = _LastExchange()

    # ------------------------------------------------------------------
    # send helpers
    # ------------------------------------------------------------------

    def send(self, command: str, timeout: float | None = None) -> str:
        """Send ``command`` and return the normalised reply string.

        The reply has its trailing CR/LF stripped (any of CR, LF, CR+LF, LF+CR is
        tolerated per manual section 3.6 DIP-switch options). Device-reported
        errors (manual section 4.5.3) are mapped to :mod:`group3.exceptions`
        classes and raised before returning.

        Args:
            command: The command string, *without* terminator.
            timeout: Per-call timeout override. ``None`` uses ``self.timeout``.

        Returns:
            The reply string, terminator stripped, leading space (if any) preserved.

        Raises:
            CommandError: ``command`` contains non-ASCII characters.
            TransportError: Underlying I/O failed.
            TimeoutError: No reply within the timeout.
            ProtocolError: Reply was not valid ASCII.
            DeviceError: The device reported a named error.
        """
        payload = codec.encode(command, self.terminator)
        effective_timeout = timeout if timeout is not None else self.timeout
        raw = self.transport.request(payload, timeout=effective_timeout)
        self._last = _LastExchange(tx=payload, rx=raw)
        reply = codec.strip_terminators(codec.decode(raw))
        check_error(reply)
        return reply

    def send_no_reply(self, command: str) -> None:
        """Send ``command`` via :meth:`Transport.write_only` with no read.

        Used for broadcast commands such as ``V`` (trigger) on the G3CL where all
        listening devices respond in parallel and a host-side read would race.
        """
        payload = codec.encode(command, self.terminator)
        self.transport.write_only(payload)
        self._last = _LastExchange(tx=payload, rx=b"")

    # ------------------------------------------------------------------
    # debug accessors
    # ------------------------------------------------------------------

    @property
    def last_raw_tx(self) -> bytes:
        """Raw bytes of the most recent outgoing payload (post-terminator)."""
        return self._last.tx

    @property
    def last_raw_rx(self) -> bytes:
        """Raw bytes of the most recent incoming reply, before terminator stripping."""
        return self._last.rx
