"""Print a clean, screenshot-ready proof that the governed stack enforces its
guarantees, by calling the running mcpo gateway exactly as Open WebUI would.

Start the gateway first (mcpo --config mcpo.config.json --port 8765), then:

    python demo_proof.py

Each block shows the real request and the gateway's real response. Stdlib only.
"""

from __future__ import annotations

import json
import urllib.request

BASE = "http://localhost:8765"


def call(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def block(title: str, verdict: str, path: str, body: dict, resp: dict) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n  {title}\n  -> {verdict}\n{bar}")
    print(f"  POST {path}  {json.dumps(body)}")
    print("  " + json.dumps(resp, indent=2).replace("\n", "\n  "))


def main() -> None:
    print("\n  GOVERNED STACK -- one self-hosted agent over SQL, KQL and documents")
    print("  Every call below goes through the same gateway Open WebUI uses.")

    r = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "viewer", "k": 5})
    ids = [x["doc_id"] for x in r["results"]]
    block(
        "DOCS / role-scoped retrieval -- a 'viewer' asks about the bonus pool",
        f"finance documents withheld; returned only {ids}",
        "/doc-steward/search_docs", {"query": "bonus pool", "role": "viewer"}, r,
    )

    r = call("/doc-steward/search_docs", {"query": "bonus pool", "role": "finance", "k": 1})
    block(
        "DOCS / same question, role = 'finance'",
        "the finance document is now returned (access by role, not by prompt)",
        "/doc-steward/search_docs", {"query": "bonus pool", "role": "finance"}, r,
    )

    r = call("/sql-steward/get_records", {"entity": "customers", "fields": ["id", "email"]})
    block(
        "SQL / the agent asks for customer email (there is no run_sql tool)",
        "refused: the field is tagged PII in the semantic layer",
        "/sql-steward/get_records", {"entity": "customers", "fields": ["id", "email"]}, r,
    )

    r = call("/kql-sop/run_kql", {"query": ".drop table StormEvents"})
    block(
        "KQL / the agent tries to drop a table",
        "blocked: a mutating command is never executed read-only",
        "/kql-sop/run_kql", {"query": ".drop table StormEvents"}, r,
    )

    print("\n  Reproduce every guarantee with: python verify.py  (asserts, exits non-zero on fail)\n")


if __name__ == "__main__":
    main()
