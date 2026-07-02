"""gov-lake: governed analytics over the stack's own governance history.

The gateway's audited tool calls and the compliance verdict ledger land in a
local DuckDB file, and this server exposes pre-approved aggregations over it.
There is no raw SQL over the lake on purpose: who queried what, and who was
denied, is itself sensitive metadata, so the same rule that governs business
data governs the governance data. Every tool refreshes the lake from the
evidence files first, so answers are never stale, and the verdict hash chain
is re-verified on every load.

Configuration (environment):
  GOV_LAKE_DB    DuckDB file path (default data/lake/gov.duckdb)
  GOV_SPANS      OTel span export to ingest (default logs/otel-spans.jsonl)
  GOV_VERDICTS   compliance ledger to ingest (default logs/compliance-audit.jsonl)
"""
from __future__ import annotations

import os

import duckdb
from fastmcp import FastMCP

from gov_lake.etl import refresh

_DB = os.environ.get("GOV_LAKE_DB", "data/lake/gov.duckdb")
_SPANS = os.environ.get("GOV_SPANS", "logs/otel-spans.jsonl")
_VERDICTS = os.environ.get("GOV_VERDICTS", "logs/compliance-audit.jsonl")

mcp = FastMCP("gov-lake")


def _fresh() -> dict:
    return refresh(_DB, _SPANS, _VERDICTS)


def _query(sql: str, params: list) -> list[dict]:
    con = duckdb.connect(_DB, read_only=True)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


@mcp.tool()
def refresh_lake() -> dict:
    """Rebuild the lake from the evidence files. Returns row counts and whether
    the compliance verdict hash chain verified end to end."""
    return _fresh()


@mcp.tool()
def cache_hit_ratio(days: int = 7, server: str = "") -> dict:
    """Cache hit ratio for allowed tool calls over the last N days, optionally
    for one server. Counts hits, misses, and the ratio."""
    meta = _fresh()
    where = "decision = 'allow' AND cache IN ('hit','miss') AND ts >= now() - INTERVAL (?) DAY"
    params: list = [days]
    if server:
        where += " AND server = ?"
        params.append(server)
    rows = _query(f"SELECT cache, COUNT(*) AS n FROM tool_calls WHERE {where} GROUP BY cache", params)
    hits = sum(r["n"] for r in rows if r["cache"] == "hit")
    misses = sum(r["n"] for r in rows if r["cache"] == "miss")
    total = hits + misses
    return {"days": days, "server": server or "all", "hits": hits, "misses": misses,
            "hit_ratio": round(hits / total, 3) if total else None, "lake": meta}


@mcp.tool()
def decision_summary(days: int = 7) -> dict:
    """Allow/deny counts by role, server, and reason over the last N days --
    the shape of who was let in and who was refused."""
    meta = _fresh()
    rows = _query("""
        SELECT role, server, decision, reason, COUNT(*) AS n
        FROM tool_calls
        WHERE ts >= now() - INTERVAL (?) DAY
        GROUP BY role, server, decision, reason
        ORDER BY n DESC
    """, [days])
    return {"days": days, "rows": rows, "lake": meta}


@mcp.tool()
def compliance_trend(days: int = 30) -> dict:
    """Compliance verdicts per day over the last N days: total checked,
    non-compliant count, and the non-compliance rate."""
    meta = _fresh()
    rows = _query("""
        SELECT CAST(ts AS DATE) AS day,
               COUNT(*) AS checked,
               SUM(CASE WHEN compliant THEN 0 ELSE 1 END) AS non_compliant
        FROM verdicts
        WHERE ts >= now() - INTERVAL (?) DAY
        GROUP BY day ORDER BY day
    """, [days])
    for r in rows:
        r["day"] = str(r["day"])
        r["rate"] = round(r["non_compliant"] / r["checked"], 3) if r["checked"] else 0.0
    return {"days": days, "rows": rows, "verdict_chain_ok": meta["verdict_chain_ok"], "lake": meta}


@mcp.tool()
def export_parquet(directory: str = "data/lake/parquet") -> dict:
    """Export the lake tables as Parquet files for any external tool to read."""
    _fresh()
    os.makedirs(directory, exist_ok=True)
    con = duckdb.connect(_DB, read_only=True)
    try:
        out = {}
        for table in ("tool_calls", "verdicts"):
            path = os.path.join(directory, f"{table}.parquet").replace("\\", "/")
            con.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET)")
            out[table] = path
        return {"exported": out}
    finally:
        con.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
