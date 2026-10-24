"""The write gate. Reads are free; anything else has to get past a person.

A write (POST, PUT, DELETE) runs only when all of these hold:

1. `CIN7_CLI_READ_ONLY` is not set. Set it on shared machines and agent hosts to
   switch writes off outright.
2. The command carries `--allow-write`.
3. A person at a real terminal sees the full request and types the operation
   name back (e.g. `sale create`). The answer is read from the controlling
   terminal, never stdin, so it cannot be piped or scripted.
"""

from __future__ import annotations

import os
import sys

from .client import Request
from .spec import Operation

READ_ONLY_ENV = "CIN7_CLI_READ_ONLY"


class WriteBlocked(RuntimeError):
    """A write was refused; the message says why and what would be needed."""


def _ask(prompt: str) -> str:
    """Read one line from the controlling terminal. Raises WriteBlocked when there is none."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip()
    except OSError as exc:
        raise WriteBlocked("a write needs a person at an interactive terminal to confirm it") from exc


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def check_write(op: Operation, req: Request, allow_write: bool, base_url: str) -> None:
    """Return normally only when the write may be sent; otherwise raise WriteBlocked."""
    if not op.is_write:
        return
    if os.environ.get(READ_ONLY_ENV, "").strip() not in ("", "0", "false", "no"):
        raise WriteBlocked(f"{op.full_name} is a write ({op.method}) and {READ_ONLY_ENV} is set: writes are off")
    if not allow_write:
        raise WriteBlocked(
            f"{op.full_name} is a write ({op.method}). This CLI is read-only by default; "
            "pass --allow-write to be asked to confirm it, or --dry-run to see the request"
        )
    _say("=" * 60)
    _say(f"WRITE to Cin7 at {base_url}")
    _say(f"Operation: {op.full_name}  ({op.group_title} / {op.resource} / {op.title})")
    _say(req.describe())
    _say("=" * 60)
    answer = _ask(f"Type `{op.full_name}` to send this write, anything else cancels: ")
    if answer != op.full_name:
        raise WriteBlocked("cancelled: the confirmation did not match")
