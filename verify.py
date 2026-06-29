"""Self-contained governance check for CI: start the gateway, assert, tear down.

Delegates to stack.py so there is a single source of truth for both the lifecycle
and the checks. For interactive use against an already-running stack, prefer
`python stack.py verify`.

    python verify.py    # exits non-zero if any guarantee fails
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PY = sys.executable
STACK = str(Path(__file__).resolve().parent / "stack.py")


def run(cmd: str) -> int:
    return subprocess.run([PY, STACK, cmd]).returncode


def main() -> int:
    if run("up") != 0:
        return 2
    try:
        return run("verify")
    finally:
        run("down")


if __name__ == "__main__":
    raise SystemExit(main())
