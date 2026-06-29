"""Governed-stack control plane: one cross-platform entry point.

    python stack.py up        # render config, seed demo data, start the gateway
    python stack.py status    # what's running and which backend each tool uses
    python stack.py verify    # assert the governance holds (adapts to config)
    python stack.py webui      # install (first run) and start Open WebUI
    python stack.py down       # stop the gateway

Configuration lives in stack.env (copy stack.env.example). Every backend is a
config line, so the same stack runs the offline demo or points at real on-prem
Postgres / Ollama / a KQL cluster without touching code.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".stack"
PID_FILE = RUN_DIR / "mcpo.pid"
CONFIG_FILE = ROOT / "mcpo.config.json"
IS_WINDOWS = platform.system() == "Windows"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, str]:
    """Read stack.env (falling back to stack.env.example), expand {root}."""
    path = ROOT / "stack.env"
    if not path.exists():
        path = ROOT / "stack.env.example"
    cfg: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().replace("{root}", ROOT.as_posix())
    return cfg


def venv_bin(name: str) -> str:
    """Path to an executable inside the project venv, per-OS."""
    sub = "Scripts" if IS_WINDOWS else "bin"
    exe = f"{name}.exe" if IS_WINDOWS else name
    return str(ROOT / ".venv" / sub / exe)


def venv_python() -> str:
    return venv_bin("python")


def render_config(cfg: dict[str, str]) -> Path:
    """Build mcpo.config.json from stack.env. Only wires env a server needs."""
    py = venv_python()

    sql_env = {
        "SQL_STEWARD_LAYER": cfg["SQL_STEWARD_LAYER"],
        "SQL_STEWARD_DB_URL": cfg["SQL_STEWARD_DB_URL"],
        "SQL_STEWARD_AUDIT_DB": f"{ROOT.as_posix()}/logs/sql-steward-audit.db",
    }

    doc_env = {"DOC_STEWARD_CORPUS": cfg["DOC_STEWARD_CORPUS"]}
    if cfg.get("DOC_STEWARD_EMBED", "hashing").lower() == "ollama":
        doc_env["DOC_STEWARD_EMBED"] = "ollama"
        doc_env["DOC_STEWARD_OLLAMA_HOST"] = cfg.get("DOC_STEWARD_OLLAMA_HOST", "http://localhost:11434")
        doc_env["DOC_STEWARD_OLLAMA_MODEL"] = cfg.get("DOC_STEWARD_OLLAMA_MODEL", "nomic-embed-text")

    kql_env = {}
    if cfg.get("KQL_SOP_CLUSTER") and cfg.get("KQL_SOP_DATABASE"):
        kql_env = {"KQL_SOP_CLUSTER": cfg["KQL_SOP_CLUSTER"], "KQL_SOP_DATABASE": cfg["KQL_SOP_DATABASE"]}

    config = {
        "mcpServers": {
            "sql-steward": {"command": py, "args": ["-m", "sql_steward.server"], "env": sql_env},
            "kql-sop": {"command": py, "args": ["-m", "kql_sop.mcp_server"], **({"env": kql_env} if kql_env else {})},
            "doc-steward": {"command": py, "args": ["-m", "doc_steward.mcp_server"], "env": doc_env},
        }
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return CONFIG_FILE


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def base_url(cfg: dict[str, str]) -> str:
    return f"http://localhost:{cfg.get('MCPO_PORT', '8765')}"


def call(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def gateway_ready(base: str) -> bool:
    try:
        for name in ("sql-steward", "kql-sop", "doc-steward"):
            urllib.request.urlopen(f"{base}/{name}/openapi.json", timeout=2).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def maybe_seed(cfg: dict[str, str]) -> None:
    """Seed the bundled SQLite demo db only if that's the configured backend."""
    db_url = cfg["SQL_STEWARD_DB_URL"]
    demo_db = ROOT / "data" / "sql" / "demo.db"
    if db_url.startswith("sqlite") and demo_db.as_posix() in db_url and not demo_db.exists():
        subprocess.run([venv_python(), str(ROOT / "data" / "sql" / "build_demo_db.py")], check=True)


def cmd_up(args: argparse.Namespace) -> int:
    cfg = load_config()
    base = base_url(cfg)
    if gateway_ready(base):
        print(f"Gateway already running at {base}")
    else:
        RUN_DIR.mkdir(exist_ok=True)
        (ROOT / "logs").mkdir(exist_ok=True)
        render_config(cfg)
        maybe_seed(cfg)
        port = cfg.get("MCPO_PORT", "8765")
        log = open(RUN_DIR / "mcpo.log", "w", encoding="utf-8")
        kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [venv_bin("mcpo"), "--config", str(CONFIG_FILE), "--port", str(port)], **kwargs
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        print(f"Starting gateway on {base} (pid {proc.pid}) ...")
        for _ in range(60):
            if gateway_ready(base):
                break
            time.sleep(1)
        else:
            print("Gateway did not become ready; see .stack/mcpo.log", file=sys.stderr)
            return 2

    print("\nGoverned tools live:")
    for name in ("sql-steward", "kql-sop", "doc-steward"):
        print(f"  {base}/{name}/docs")
    _print_backends(cfg)
    if args.webui:
        return cmd_webui(args)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    if not PID_FILE.exists():
        print("No pid file; gateway not started by this tool.")
        return 0
    pid = int(PID_FILE.read_text().strip())
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        print(f"Stopped gateway (pid {pid}).")
    except (ProcessLookupError, OSError) as exc:
        print(f"Could not stop pid {pid}: {exc}")
    PID_FILE.unlink(missing_ok=True)
    return 0


def _print_backends(cfg: dict[str, str]) -> None:
    sql = "SQLite (demo)" if cfg["SQL_STEWARD_DB_URL"].startswith("sqlite") else cfg["SQL_STEWARD_DB_URL"].split("://")[0]
    embed = cfg.get("DOC_STEWARD_EMBED", "hashing")
    kql = "execute" if cfg.get("KQL_SOP_CLUSTER") else "validate-only"
    print("\nBackends:")
    print(f"  sql-steward : {sql}")
    print(f"  doc-steward : {embed} embedder")
    print(f"  kql-sop     : {kql}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    base = base_url(cfg)
    up = gateway_ready(base)
    print(f"Gateway {base}: {'UP' if up else 'DOWN'}")
    if up:
        _print_backends(cfg)
    return 0 if up else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Assert governance against the running gateway, adapting to config."""
    cfg = load_config()
    base = base_url(cfg)
    if not gateway_ready(base):
        print("Gateway not running. Start it with: python stack.py up", file=sys.stderr)
        return 2

    passed = failed = 0

    def check(label: str, ok: bool) -> None:
        nonlocal passed, failed
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if ok:
            passed += 1
        else:
            failed += 1

    email = call(base, "/sql-steward/get_records", {"entity": "customers", "fields": ["id", "email"]})
    check("sql-steward refuses a PII field", email.get("kind") == "pii_blocked")
    metric = call(base, "/sql-steward/get_metric", {"metric": "mrr_total", "dimensions": ["plan"]})
    check("sql-steward compiles and runs a metric", metric.get("rowcount", 0) > 0)

    drop = call(base, "/kql-sop/run_kql", {"query": ".drop table T"})
    check("kql-sop blocks a control command", drop.get("blocked") is True)

    viewer = call(base, "/doc-steward/search_docs", {"query": "bonus pool", "role": "viewer", "k": 5})
    check("doc-steward denies finance docs to a viewer",
          "comp-bonus-2026" not in {r["doc_id"] for r in viewer["results"]})
    finance = call(base, "/doc-steward/search_docs", {"query": "bonus pool", "role": "finance", "k": 5})
    check("doc-steward grants finance docs to finance",
          "comp-bonus-2026" in {r["doc_id"] for r in finance["results"]})

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def cmd_webui(args: argparse.Namespace) -> int:
    """Install (first run) and start Open WebUI natively, no Docker."""
    py = venv_python()
    have = subprocess.run([py, "-c", "import open_webui"], capture_output=True).returncode == 0
    if not have:
        print("Installing Open WebUI (first run, this takes a few minutes) ...")
        rc = subprocess.run(
            [py, "-m", "pip", "install", "--upgrade",
             "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org",
             "open-webui"]
        ).returncode
        if rc != 0:
            print("Open WebUI install failed.", file=sys.stderr)
            return rc
    print("Starting Open WebUI on http://localhost:8080 ...")
    cfg = load_config()
    ollama = cfg.get("DOC_STEWARD_OLLAMA_HOST", "http://localhost:11434")
    # Use Ollama for chat AND for RAG embeddings, so no SentenceTransformer model
    # is downloaded from HuggingFace (which a TLS-inspecting proxy tends to block).
    env = {
        **os.environ,
        "WEBUI_AUTH": "False",
        "OLLAMA_BASE_URL": ollama,
        "RAG_EMBEDDING_ENGINE": "ollama",
        "RAG_EMBEDDING_MODEL": "nomic-embed-text",
        "HF_HUB_OFFLINE": "1",
        # Default new chats to a real chat model, not the embedder.
        "DEFAULT_MODELS": cfg.get("CHAT_MODEL", "qwen3:14b"),
    }
    subprocess.run([venv_bin("open-webui"), "serve", "--port", "8080"], env=env)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Governed-stack control plane.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="render config, seed demo data, start the gateway")
    up.add_argument("--webui", action="store_true", help="also start Open WebUI")
    sub.add_parser("down", help="stop the gateway")
    sub.add_parser("status", help="show what's running and each tool's backend")
    sub.add_parser("verify", help="assert the governance holds")
    sub.add_parser("webui", help="install and start Open WebUI (no Docker)")
    args = parser.parse_args()

    return {
        "up": cmd_up, "down": cmd_down, "status": cmd_status,
        "verify": cmd_verify, "webui": cmd_webui,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
