"""Hand the unsent Kaus draft to a native CLI's own prefill flag.

The draft arrives through the in-memory environment relay, so it never appears
in the launch script, the launch record or the command summary. It is removed
from the environment before exec and only reaches the native process as the
value of its documented prefill flag (for example Claude's ``--prefill``).
"""
from __future__ import annotations

import os
import sys

DRAFT_ENV = "KAUS_TERMINAL_DRAFT"
#: Above this the draft stays in Kaus and on the clipboard instead of argv.
MAX_DRAFT_CHARS = 16_000


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        raise SystemExit("usage: draft_prefill.py <flag> -- <command...>")
    flag, command = args[0], args[2:]
    env = dict(os.environ)
    draft = env.pop(DRAFT_ENV, "")
    os.execvpe(command[0], [*command, flag, draft] if draft else command, env)


if __name__ == "__main__":
    main()
