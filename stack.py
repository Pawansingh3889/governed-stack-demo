"""Governed-stack control plane: one cross-platform entry point.

    python stack.py up        # start mcpo + OPA + the governance gateway
    python stack.py status    # what's running and which backend each tool uses
    python stack.py verify    # assert governance + gateway policy hold
    python stack.py webui      # install (first run) and start Open WebUI
    python stack.py down       # stop everything

Topology: Open WebUI -> governance gateway (:8765) -> mcpo (internal :8766) -> tools.
The gateway authenticates each call (token to role), checks an OPA/Rego policy,
counts a per-role budget, and audits, before mcpo and the in-tool gates run.

Configuration lives in stack.env (copy stack.env.example).
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
CONFIG_FILE = ROOT / "mcpo.config.json"
IS_WINDOWS = platform.system() == "Windows"
SERVICES = ("gateway", "opa", "mcpo")  # display / stop order


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, str]:
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
    sub = "Scripts" if IS_WINDOWS else "bin"
    exe = f"{name}.exe" if IS_WINDOWS else name
    return str(ROOT / ".venv" / sub / exe)


def venv_python() -> str:
    return venv_bin("python")


def opa_bin() -> str:
    return str(ROOT / ".opa" / ("opa.exe" if IS_WINDOWS else "opa"))


def manager_token(cfg: dict[str, str]) -> str:
    for pair in cfg.get("GATEWAY_TOKENS", "").split(","):
        if ":" in pair and pair.split(":", 1)[1].strip() == "manager":
            return pair.split(":", 1)[0].strip()
    return ""


def server_names() -> list[str]:
    if CONFIG_FILE.exists():
        return list(json.loads(CONFIG_FILE.read_text(encoding="utf-8"))["mcpServers"])
    return ["sql-steward", "kql-sop", "doc-steward"]


# Servers that can run natively over Streamable HTTP (module exposing `mcp`).
# schema-scout is stdio-only for now: its catalog comes in as an argv flag.
NATIVE_TARGETS = {
    "sql-steward": "sql_steward.server",
    "kql-sop": "kql_sop.mcp_server",
    "doc-steward": "doc_steward.mcp_server",
    "thread-recall": "thread_recall.mcp_server",
    "compliance-check": "compliance_check.mcp_server",
    "gov-lake": "gov_lake.mcp_server",
}


def native_map(cfg: dict[str, str]) -> dict[str, int]:
    """server name -> local port, for servers configured to run over HTTP."""
    base = int(cfg.get("NATIVE_PORT_BASE", "8791") or 8791)
    wanted = [s.strip() for s in cfg.get("NATIVE_MCP_SERVERS", "").split(",") if s.strip()]
    return {name: base + i for i, name in enumerate(sorted(set(wanted))) if name in NATIVE_TARGETS}


def server_specs(cfg: dict[str, str]) -> dict:
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

    servers = {
        "sql-steward": {"command": py, "args": ["-m", "sql_steward.server"], "env": sql_env},
        "kql-sop": {"command": py, "args": ["-m", "kql_sop.mcp_server"], **({"env": kql_env} if kql_env else {})},
        "doc-steward": {"command": py, "args": ["-m", "doc_steward.mcp_server"], "env": doc_env},
    }
    catalog = cfg.get("SCHEMA_SCOUT_CATALOG", "")
    if catalog and Path(catalog).exists():
        servers["schema-scout"] = {"command": py, "args": ["-m", "schema_scout.mcp_server", "--catalog", catalog]}
    tr_db = cfg.get("THREAD_RECALL_DB", "")
    if tr_db:
        tr_env = {"THREAD_RECALL_DB": tr_db, "THREAD_RECALL_MASK": cfg.get("THREAD_RECALL_MASK", "1")}
        if cfg.get("THREAD_RECALL_EMBED", "hashing").lower() == "ollama":
            tr_env["THREAD_RECALL_EMBED"] = "ollama"
            tr_env["THREAD_RECALL_OLLAMA_HOST"] = cfg.get("DOC_STEWARD_OLLAMA_HOST", "http://localhost:11434")
            tr_env["THREAD_RECALL_OLLAMA_MODEL"] = cfg.get("DOC_STEWARD_OLLAMA_MODEL", "nomic-embed-text")
        servers["thread-recall"] = {"command": py, "args": ["-m", "thread_recall.mcp_server"], "env": tr_env}
    comp_db = cfg.get("COMPLIANCE_DB", "")
    if comp_db:
        comp_env = {
            "COMPLIANCE_DB": comp_db,
            "COMPLIANCE_AUDIT": f"{ROOT.as_posix()}/logs/compliance-audit.jsonl",
            "COMPLIANCE_TAXONOMY": f"{ROOT.as_posix()}/data/compliance/allergens_full.json",
            "PYTHONPATH": str(ROOT),
        }
        servers["compliance-check"] = {"command": py, "args": ["-m", "compliance_check.mcp_server"], "env": comp_env}
    lake_db = cfg.get("GOV_LAKE_DB", "")
    if lake_db:
        lake_env = {
            "GOV_LAKE_DB": lake_db,
            "GOV_SPANS": f"{ROOT.as_posix()}/logs/otel-spans.jsonl",
            "GOV_VERDICTS": f"{ROOT.as_posix()}/logs/compliance-audit.jsonl",
            "PYTHONPATH": str(ROOT),
        }
        servers["gov-lake"] = {"command": py, "args": ["-m", "gov_lake.mcp_server"], "env": lake_env}

    return servers


def render_config(cfg: dict[str, str]) -> Path:
    servers = server_specs(cfg)
    # Servers running natively over Streamable HTTP: mcpo consumes their URL
    # instead of spawning them, so one process serves both mcpo and MCP clients.
    for name, port in native_map(cfg).items():
        if name in servers:
            servers[name] = {"type": "streamable-http", "url": f"http://localhost:{port}/mcp"}
    CONFIG_FILE.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return CONFIG_FILE


# --------------------------------------------------------------------------- #
# Process management
# --------------------------------------------------------------------------- #
def _spawn(name: str, argv: list[str], env: dict | None = None) -> None:
    RUN_DIR.mkdir(exist_ok=True)
    log = open(RUN_DIR / f"{name}.log", "w", encoding="utf-8")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
    if env is not None:
        kwargs["env"] = {**os.environ, **env}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    (RUN_DIR / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")


def _stop(name: str) -> None:
    pf = RUN_DIR / f"{name}.pid"
    if not pf.exists():
        return
    pid = int(pf.read_text().strip())
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    pf.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def gateway_base(cfg: dict[str, str]) -> str:
    return f"http://localhost:{cfg.get('GATEWAY_PORT', '8765')}"


def request(url: str, body=None, token: str | None = None, method: str = "POST"):
    """Return (status_code, json_or_none); does not raise on 4xx/5xx."""
    code, data, _ = request_full(url, body, token, method)
    return code, data


def request_full(url: str, body=None, token: str | None = None, method: str = "POST"):
    """Return (status_code, json_or_none, response_headers); does not raise on 4xx/5xx."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null"), dict(exc.headers or {})
        except Exception:
            return exc.code, None, {}


def gateway_ready(cfg: dict[str, str]) -> bool:
    opa_port = cfg.get("OPA_PORT", "8181")
    token = manager_token(cfg)
    try:
        urllib.request.urlopen(f"http://localhost:{opa_port}/health", timeout=2).read()
        for name in server_names():
            code, _ = request(f"{gateway_base(cfg)}/{name}/openapi.json", token=token, method="GET")
            if code != 200:
                return False
        return True
    except (urllib.error.URLError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def maybe_seed(cfg: dict[str, str]) -> None:
    db_url = cfg["SQL_STEWARD_DB_URL"]
    demo_db = ROOT / "data" / "sql" / "demo.db"
    if db_url.startswith("sqlite") and demo_db.as_posix() in db_url and not demo_db.exists():
        subprocess.run([venv_python(), str(ROOT / "data" / "sql" / "build_demo_db.py")], check=True)

    catalog = cfg.get("SCHEMA_SCOUT_CATALOG", "")
    demo_catalog = (ROOT / "data" / "schema" / "catalog.json").as_posix()
    if catalog and Path(catalog).as_posix() == demo_catalog and not Path(catalog).exists():
        subprocess.run(
            [venv_python(), "-m", "schema_scout.cli", "demo", "--out", str(ROOT / "data" / "schema")], check=True
        )


def cmd_up(args: argparse.Namespace) -> int:
    cfg = load_config()
    base = gateway_base(cfg)
    if gateway_ready(cfg):
        print(f"Gateway already running at {base}")
    else:
        RUN_DIR.mkdir(exist_ok=True)
        (ROOT / "logs").mkdir(exist_ok=True)
        render_config(cfg)
        maybe_seed(cfg)
        if not Path(opa_bin()).exists():
            print("OPA binary missing at .opa/ — run setup.ps1 to download it.", file=sys.stderr)
            return 2

        mport = cfg.get("MCPO_PORT", "8766")
        oport = cfg.get("OPA_PORT", "8181")

        specs = server_specs(cfg)
        natives = native_map(cfg)
        for name, port in natives.items():
            spec = specs.get(name)
            if spec is None:
                continue
            env = {**spec.get("env", {}), "PYTHONPATH": str(ROOT)}
            _spawn(
                f"native-{name}",
                [venv_python(), str(ROOT / "native_runner.py"), NATIVE_TARGETS[name], str(port)],
                env=env,
            )
        for name, port in natives.items():
            if not _wait_port(port, 30):
                print(f"native {name} did not open port {port}; see .stack/native-{name}.log", file=sys.stderr)
                return 2

        _spawn("mcpo", [venv_bin("mcpo"), "--config", str(CONFIG_FILE), "--port", mport])
        _spawn("opa", [opa_bin(), "run", "--server", "--addr", f"localhost:{oport}", "policy/"])
        _spawn(
            "gateway",
            [venv_python(), "-m", "uvicorn", "gateway:app", "--host", "127.0.0.1", "--port", cfg.get("GATEWAY_PORT", "8765")],
            env={
                "MCPO_INTERNAL_URL": f"http://localhost:{mport}",
                "OPA_URL": f"http://localhost:{oport}/v1/data/governed/decision",
                "GATEWAY_TOKENS": cfg.get("GATEWAY_TOKENS", ""),
                "GATEWAY_CACHE_TTL": cfg.get("GATEWAY_CACHE_TTL", "0"),
                "GATEWAY_NATIVE_SERVERS": ",".join(
                    f"{n}=http://localhost:{p}/mcp" for n, p in native_map(cfg).items()
                ),
                "GATEWAY_AUDIT_DB": f"{ROOT.as_posix()}/logs/gateway-audit.db",
                "OTEL_EXPORTER_OTLP_ENDPOINT": cfg.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                "OTEL_SPAN_FILE": f"{ROOT.as_posix()}/logs/otel-spans.jsonl",
            },
        )
        print(f"Starting gateway on {base} (auth + OPA policy) ...")
        for _ in range(60):
            if gateway_ready(cfg):
                break
            time.sleep(1)
        else:
            print("Gateway did not become ready; see .stack/*.log", file=sys.stderr)
            return 2

    print("\nGoverned tools (auth required) live behind the gateway:")
    for name in server_names():
        print(f"  {base}/{name}/docs")
    _print_backends(cfg)
    if args.webui:
        return cmd_webui(args)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    for name in SERVICES:
        _stop(name)
    for pf in RUN_DIR.glob("native-*.pid"):
        _stop(pf.stem)
    print("Stopped gateway, OPA, mcpo, and any native MCP servers.")
    return 0


def _wait_port(port: int, seconds: int) -> bool:
    import socket

    for _ in range(seconds * 2):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _print_backends(cfg: dict[str, str]) -> None:
    sql = "SQLite (demo)" if cfg["SQL_STEWARD_DB_URL"].startswith("sqlite") else cfg["SQL_STEWARD_DB_URL"].split("://")[0]
    embed = cfg.get("DOC_STEWARD_EMBED", "hashing")
    kql = "execute" if cfg.get("KQL_SOP_CLUSTER") else "validate-only"
    roles = [p.split(":", 1)[1].strip() for p in cfg.get("GATEWAY_TOKENS", "").split(",") if ":" in p]
    print("\nGateway:")
    print(f"  auth   : {len(roles)} role token(s) ({', '.join(roles)})")
    print(f"  policy : OPA/Rego at :{cfg.get('OPA_PORT', '8181')} (default deny)")
    print(f"  mcpo   : internal :{cfg.get('MCPO_PORT', '8766')}")
    print("Backends:")
    print(f"  sql-steward : {sql}")
    print(f"  doc-steward : {embed} embedder")
    print(f"  kql-sop     : {kql}")
    if cfg.get("SCHEMA_SCOUT_CATALOG") and Path(cfg["SCHEMA_SCOUT_CATALOG"]).exists():
        print("  schema-scout: catalog discovery")
    if cfg.get("THREAD_RECALL_DB"):
        mask = "masked" if cfg.get("THREAD_RECALL_MASK", "1").lower() in ("1", "true", "yes", "on") else "unmasked"
        print(f"  thread-recall: SQLite memory ({mask} on write)")
    if cfg.get("COMPLIANCE_DB"):
        print("  compliance-check: deterministic verdicts (fail-closed, hash-chain audited)")
    if cfg.get("GOV_LAKE_DB"):
        print("  gov-lake     : DuckDB lakehouse over the stack's own audit trail")
    ttl = cfg.get("GATEWAY_CACHE_TTL", "0")
    if ttl not in ("", "0"):
        print(f"  gateway-cache: exact-match, TTL {ttl}s (policy re-checked on every hit)")
    natives = native_map(cfg)
    if natives:
        names = ", ".join(f"{n} (:{p})" for n, p in natives.items())
        print(f"  native-mcp   : Streamable HTTP, governed at /<server>/mcp — {names}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    up = gateway_ready(cfg)
    print(f"Gateway {gateway_base(cfg)}: {'UP' if up else 'DOWN'}")
    if up:
        _print_backends(cfg)
    return 0 if up else 1


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config()
    base = gateway_base(cfg)
    if not gateway_ready(cfg):
        print("Gateway not running. Start it with: python stack.py up", file=sys.stderr)
        return 2

    mgr = manager_token(cfg)
    tokens = {p.split(":", 1)[1].strip(): p.split(":", 1)[0].strip() for p in cfg.get("GATEWAY_TOKENS", "").split(",") if ":" in p}
    passed = failed = 0

    def check(label: str, ok: bool) -> None:
        nonlocal passed, failed
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed += int(bool(ok))
        failed += int(not ok)

    def call(path: str, body: dict, token: str | None = mgr):
        return request(f"{base}{path}", body, token=token)[1]

    print("\ngateway (auth + OPA policy)")
    code, _ = request(f"{base}/sql-steward/get_metric", {"metric": "mrr_total"}, token=None)
    check("rejects an unauthenticated call (401)", code == 401)
    code, _ = request(f"{base}/sql-steward/get_metric", {"metric": "mrr_total"}, token=tokens.get("viewer"))
    check("OPA denies viewer the metric tool (403)", code == 403)
    code, _ = request(f"{base}/kql-sop/run_kql", {"query": ".drop table T"}, token=tokens.get("analyst"))
    check("OPA denies analyst a KQL control command (403)", code == 403)
    code, _ = request(f"{base}/kql-sop/run_kql", {"query": ".drop table T"}, token=mgr)
    check("OPA allows manager the control command (200)", code == 200)

    print("\nin-tool gates (defense in depth, via manager token)")
    email = call("/sql-steward/get_records", {"entity": "customers", "fields": ["id", "email"]})
    check("sql-steward refuses a PII field", (email or {}).get("kind") == "pii_blocked")
    metric = call("/sql-steward/get_metric", {"metric": "mrr_total", "dimensions": ["plan"]})
    check("sql-steward compiles and runs a metric", (metric or {}).get("rowcount", 0) > 0)
    dq = call("/sql-steward/run_checks", {})
    check("sql-steward data-quality checks pass (readiness 100)", (dq or {}).get("status") == "ok" and (dq or {}).get("readiness") == 100)
    drop = call("/kql-sop/run_kql", {"query": ".drop table T"})
    check("kql-sop still blocks the control command", (drop or {}).get("blocked") is True)
    viewer = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "viewer", "k": 5})
    check("doc-steward denies finance docs to a viewer", "comp-bonus-2026" not in {r["doc_id"] for r in (viewer or {}).get("results", [])})
    finance = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "finance", "k": 5})
    check("doc-steward grants finance docs to finance", "comp-bonus-2026" in {r["doc_id"] for r in (finance or {}).get("results", [])})
    if cfg.get("SCHEMA_SCOUT_CATALOG") and Path(cfg["SCHEMA_SCOUT_CATALOG"]).exists():
        tables = call("/schema-scout/list_tables", {})
        check("schema-scout lists catalog tables", isinstance(tables, list) and len(tables) > 0)
    if cfg.get("THREAD_RECALL_DB"):
        tid = "verify-check"
        call("/thread-recall/remember", {"thread_id": tid, "content": "reach me at zoe@example.com about Pro"})
        rec = call("/thread-recall/recall", {"thread_id": tid, "query": "contact details", "k": 3})
        text = " ".join(r["content"] for r in (rec or {}).get("results", []))
        check("thread-recall masks PII on write", (rec or {}).get("count", 0) > 0 and "@example.com" not in text)
        call("/thread-recall/forget", {"thread_id": tid})
    if cfg.get("COMPLIANCE_DB"):
        code, verdict = request(f"{base}/compliance-check/batch_compliance",
                                 {"batch_id": "B-1003"}, token=tokens.get("viewer"))
        allergen_fail = any(
            f.get("check") == "allergen_crosscheck" and not f.get("passed")
            for f in (verdict or {}).get("findings", [])
        )
        check("compliance-check flags an undeclared allergen (verdict computed pre-LLM)",
              code == 200 and (verdict or {}).get("compliant") is False and allergen_fail)
        temp = call("/compliance-check/batch_compliance", {"batch_id": "B-1002"})
        check("compliance-check flags a cold-chain breach",
              (temp or {}).get("compliant") is False)
        ghost = call("/compliance-check/batch_compliance", {"batch_id": "B-NOPE"})
        check("compliance-check fails closed on an unknown batch",
              (ghost or {}).get("compliant") is False)
        ok = call("/compliance-check/batch_compliance", {"batch_id": "B-1001"})
        canon = any(
            f.get("check") == "allergen_crosscheck" and f.get("passed")
            and "canonicalized" in f.get("detail", "")
            for f in (ok or {}).get("findings", [])
        )
        check("allergen names canonicalize via the Open Food Facts taxonomy (Lactose = Milk)",
              (ok or {}).get("compliant") is True and canon)

    natives = native_map(cfg)
    if "compliance-check" in natives and cfg.get("COMPLIANCE_DB"):
        print("\nnative MCP (Streamable HTTP through the gateway)")

        def mcp_call(token: str | None, batch: str):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "batch_compliance", "arguments": {"batch_id": batch}}}
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(f"{base}/compliance-check/mcp",
                                          data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")

        code, text = mcp_call(tokens.get("viewer"), "B-1003")
        check("a native MCP tools/call flows through governance (viewer, 200)",
              code == 200 and '"compliant"' in text and "false" in text)
        code, text = mcp_call(tokens.get("finance"), "B-1003")
        check("the MCP route is no side door (finance denied by policy)",
              "denied by policy" in text)
        code, text = mcp_call(None, "B-1003")
        check("the MCP route rejects an unauthenticated call (401)", code == 401)

    if cfg.get("GATEWAY_CACHE_TTL", "0") not in ("", "0"):
        print("\ngoverned cache (exact-match, policy re-checked on hits)")
        call("/sql-steward/get_metric", {"metric": "mrr_total"})  # prime the cache
        code, _, hdrs = request_full(f"{base}/sql-steward/get_metric", {"metric": "mrr_total"}, token=mgr)
        hdrs = {k.lower(): v for k, v in hdrs.items()}
        check("gateway serves a repeat call from the cache", code == 200 and hdrs.get("x-gov-cache") == "hit")
        code, _ = request(f"{base}/sql-steward/get_metric", {"metric": "mrr_total"}, token=tokens.get("viewer"))
        check("a cached response never bypasses policy (viewer stays denied)", code == 403)

    if cfg.get("GOV_LAKE_DB"):
        print("\ngov-lake (analytics over the stack's own audit trail)")
        code, ratio = request(f"{base}/gov-lake/cache_hit_ratio", {"days": 1}, token=tokens.get("analyst"))
        check("analyst reads the cache hit ratio from the lake",
              code == 200 and (ratio or {}).get("hits", 0) >= 1)
        code, trend = request(f"{base}/gov-lake/compliance_trend", {"days": 1}, token=tokens.get("analyst"))
        rows = (trend or {}).get("rows", [])
        check("compliance trend aggregates verdicts and the ledger chain verifies",
              code == 200 and (trend or {}).get("verdict_chain_ok") is True
              and sum(r.get("non_compliant", 0) for r in rows) >= 1)
        code, _ = request(f"{base}/gov-lake/decision_summary", {"days": 1}, token=tokens.get("viewer"))
        check("governance metadata is governed too (viewer denied the lake)", code == 403)

    # Data budget: finance has a tight budget_bytes in policy/roles.json, so a
    # short run of calls exhausts it and OPA denies further ones.
    fin = tokens.get("finance")
    budget_hit = False
    for _ in range(15):
        code, data = request(f"{base}/doc-steward/search_docs",
                              {"query": "annual leave policy", "role": "finance", "k": 3}, token=fin)
        if code == 403 and "budget" in str(data):
            budget_hit = True
            break
    check("OPA cuts a role off at its data budget", budget_hit)

    span_file = ROOT / "logs" / "otel-spans.jsonl"
    spans = span_file.read_text(encoding="utf-8") if span_file.exists() else ""
    otel_ok = '"gov.decision"' in spans and '"gen_ai.operation.name"' in spans and '"execute_tool ' in spans
    check("gateway exports GenAI-convention OTel spans for decisions", otel_ok)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def cmd_webui(args: argparse.Namespace) -> int:
    py = venv_python()
    have = subprocess.run([py, "-c", "import open_webui"], capture_output=True).returncode == 0
    if not have:
        print("Installing Open WebUI (first run, a few minutes) ...")
        rc = subprocess.run(
            [py, "-m", "pip", "install", "--upgrade", "--trusted-host", "pypi.org",
             "--trusted-host", "files.pythonhosted.org", "open-webui"]
        ).returncode
        if rc != 0:
            return rc
    cfg = load_config()
    ollama = cfg.get("DOC_STEWARD_OLLAMA_HOST", "http://localhost:11434")
    print("Starting Open WebUI on http://localhost:8080 ...")
    print("Add tool servers as http://localhost:8765/<server> with a gateway token as the API key.")
    env = {
        **os.environ, "WEBUI_AUTH": "False", "OLLAMA_BASE_URL": ollama,
        "RAG_EMBEDDING_ENGINE": "ollama", "RAG_EMBEDDING_MODEL": "nomic-embed-text",
        "HF_HUB_OFFLINE": "1", "DEFAULT_MODELS": cfg.get("CHAT_MODEL", "qwen3:14b"),
    }
    subprocess.run([venv_bin("open-webui"), "serve", "--port", "8080"], env=env)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Governed-stack control plane.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="start mcpo + OPA + the governance gateway")
    up.add_argument("--webui", action="store_true", help="also start Open WebUI")
    sub.add_parser("down", help="stop everything")
    sub.add_parser("status", help="show what's running and each tool's backend")
    sub.add_parser("verify", help="assert governance + gateway policy hold")
    sub.add_parser("webui", help="install and start Open WebUI (no Docker)")
    args = parser.parse_args()
    return {"up": cmd_up, "down": cmd_down, "status": cmd_status, "verify": cmd_verify, "webui": cmd_webui}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
