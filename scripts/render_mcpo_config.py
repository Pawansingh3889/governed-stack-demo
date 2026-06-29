"""Render mcpo.config.json from stack.env.

Thin wrapper kept for backwards compatibility. The canonical entry point is now
`python stack.py up` (which renders, seeds, and starts). This just renders.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stack import load_config, render_config  # noqa: E402


def main() -> None:
    out = render_config(load_config())
    print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
