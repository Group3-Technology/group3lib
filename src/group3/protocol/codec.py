"""ASCII codec and terminator handling.

The DTM-151-S speaks strict ASCII (manual section 4.5.2). Numeric commands sent from
the host are terminated by ``<cr>`` (0x0D). Device responses are terminated by a
character or pair selected by DIP switches S2-2 and S2-3: the options are CR, LF,
CR+LF, or LF+CR. We accept any of these on the read side via :func:`strip_terminators`
and send CR by default on the write side.
"""

from __future__ import annotations

from typing import Final

from group3.exceptions import CommandError, ProtocolError

CR: Final = b"\r"
LF: Final = b"\n"
CRLF: Final = b"\r\n"
LFCR: Final = b"\n\r"

TERMINATORS: Final[dict[str, bytes]] = {
    "CR": CR,
    "LF": LF,
    "CRLF": CRLF,
    "LFCR": LFCR,
}


def encode(command: str, terminator: bytes = CR) -> bytes:
    """Encode a command string as ASCII with an appended terminator.

    Args:
        command: The command, without terminator (e.g., ``"R2"``, ``"F"``, ``"A05"``).
        terminator: Bytes to append. Default ``b"\\r"`` matches the DTM-151-S host-side
            requirement from manual section 4.5.2.

    Returns:
        The full on-wire byte string.

    Raises:
        CommandError: ``command`` contains a non-ASCII character.
    """
    try:
        body = command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CommandError(
            f"Command must be ASCII; got non-ASCII character at index {exc.start}: "
            f"{command!r}"
        ) from exc
    return body + terminator


def decode(raw: bytes) -> str:
    """Decode raw device bytes as ASCII.

    Args:
        raw: Bytes as returned by the transport (terminator may still be present).

    Returns:
        The decoded string.

    Raises:
        ProtocolError: Bytes could not be decoded as ASCII.
    """
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError(
            f"Reply is not valid ASCII at byte {exc.start}",
            raw=raw,
        ) from exc


def strip_terminators(text: str) -> str:
    """Strip trailing CR/LF characters (in any combination) from ``text``.

    The device can be configured to send any of CR, LF, CR+LF, or LF+CR as its
    terminator (S2-2 + S2-3). Rather than branch on configuration, we accept any of
    them.
    """
    return text.rstrip("\r\n")
