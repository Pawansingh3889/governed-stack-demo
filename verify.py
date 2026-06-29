"""End-to-end proof that the governed stack enforces its guarantees through mcpo.

Starts mcpo with all three MCP servers, calls each tool over HTTP exactly as Open
WebUI does, and asserts the governance holds: sql-steward refuses PII and never
exposes raw SQL, kql-sop blocks mutating queries, doc-steward scopes documents by
role and redacts PII. Prints a PASS/FAIL line per check and exits non-zero on any
failure. Stdlib only -- no test framework, no extra dependencies.

    python verify.py            # uses port 8765
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8765
BASE = f"http://localhost:{PORT}"
MCPO = ROOT / ".venv" / "Scripts" / "mcpo.exe"
CONFIG = ROOT / "mcpo.config.json"

_passed = 0
_failed = 0


def check(label: str, ok: bool) -> None:
    global _passed, _failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"  [{mark}] {label}")


def call(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def wait_until_up(timeout: int = 60) -> bool:
    # Each MCP server is mounted as a sub-app once it finishes connecting, which
    # can lag the root app by several seconds. Wait for all three sub-apps, not
    # just the root, or the first tool call races the mount and 404s.
    deadline = time.monotonic() + timeout
    subapps = ["sql-steward", "kql-sop", "doc-steward"]
    while time.monotonic() < deadline:
        try:
            for name in subapps:
                urllib.request.urlopen(f"{BASE}/{name}/openapi.json", timeout=2).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


def run_checks() -> None:
    print("\nsql-steward (governed SQL: no run_sql, PII blocked, compiled queries)")
    entities = call("/sql-steward/list_entities", {})
    names = {e["name"] for e in entities.get("entities", [])}
    check("exposes only the whitelisted entities", names == {"customers", "subscriptions"})

    email = call("/sql-steward/get_records", {"entity": "customers", "fields": ["id", "email"]})
    check("refuses a PII (email) field", email.get("refused") and email.get("kind") == "pii_blocked")

    metric = call("/sql-steward/get_metric", {"metric": "mrr_total", "dimensions": ["plan"]})
    check("compiles and runs an approved metric", metric.get("rowcount", 0) > 0 and "sql" in metric)

    print("\nkql-sop (governed KQL: the gatekeeper refuses mutating queries)")
    drop = call("/kql-sop/run_kql", {"query": ".drop table StormEvents"})
    check("blocks a control command (.drop)", drop.get("blocked") is True and drop.get("executed") is False)

    safe = call("/kql-sop/run_kql", {"query": "StormEvents | where StartTime > ago(1d) | take 10"})
    check("allows a safe, time-bounded query", safe.get("blocked") is False)

    print("\ndoc-steward (governed RAG: role-scoped retrieval, PII redacted)")
    viewer = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "viewer", "k": 5})
    viewer_ids = {r["doc_id"] for r in viewer["results"]}
    check("viewer is denied the finance-only documents", "comp-bonus-2026" not in viewer_ids)

    finance = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "finance", "k": 5})
    finance_ids = {r["doc_id"] for r in finance["results"]}
    check("finance is granted the finance-only documents", "comp-bonus-2026" in finance_ids)

    it = call("/doc-steward/search_docs", {"query": "contact IT support helpdesk", "role": "viewer", "k": 1})
    text = it["results"][0]["text"] if it["results"] else ""
    check("redacts PII (email/phone) in returned chunks", "[REDACTED]" in text and "@" not in text)


def main() -> int:
    # Prerequisites: rendered config + seeded database.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "render_mcpo_config.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "data" / "sql" / "build_demo_db.py")], check=True)
    (ROOT / "logs").mkdir(exist_ok=True)

    print(f"\nstarting mcpo on :{PORT} ...")
    proc = subprocess.Popen(
        [str(MCPO), "--config", str(CONFIG), "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(ROOT),
    )
    try:
        if not wait_until_up():
            print("mcpo did not become ready in time", file=sys.stderr)
            return 2
        run_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
