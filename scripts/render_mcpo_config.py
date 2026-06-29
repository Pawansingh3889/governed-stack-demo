"""Render mcpo.config.json from the example template.

mcpo launches each MCP server as a subprocess; on Windows the command must be an
absolute, resolvable path, and the env paths must be absolute too (the servers
read them relative to mcpo's working directory otherwise). So the committed
template uses a ``__ROOT__`` token, and this script stamps in the real absolute
path of this folder, using forward slashes (valid in JSON and accepted by the
Windows process launcher and SQLAlchemy).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "mcpo.config.example.json"
OUTPUT = ROOT / "mcpo.config.json"


def main() -> None:
    root = ROOT.as_posix()  # forward slashes, e.g. C:/Users/.../governed-stack-demo
    rendered = TEMPLATE.read_text(encoding="utf-8").replace("__ROOT__", root)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.name} (root: {root})")


if __name__ == "__main__":
    main()
