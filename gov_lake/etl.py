"""Ingest the stack's governance evidence into a local DuckDB lakehouse.

Two sources, both already on disk:
  - the gateway's OpenTelemetry span export (logs/otel-spans.jsonl): one record
    per tool call with role, decision, reason, cache outcome, and transport;
  - the compliance verdict ledger (logs/compliance-audit.jsonl): hash-chained,
    one record per verdict.

The refresh is a full idempotent rebuild -- these logs are small, and rebuild-
on-read keeps the lake trivially consistent with the evidence files. The
verdict chain is re-verified on every load, so a tampered ledger line turns up
as chain_ok=false rather than silently feeding analytics. Export to Parquet is
one call away (DuckDB COPY TO); swap the file for a DuckLake catalog when the
volume ever justifies it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb


def _parse_spans(path: str) -> list[tuple]:
    rows = []
    p = Path(path)
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            s = json.loads(line)
        except Exception:
            continue
        a = s.get("attributes") or {}
        if "gov.decision" not in a:
            continue
        rows.append((
            s.get("start_time"),
            a.get("gov.server"),
            a.get("gov.tool"),
            a.get("gov.role"),
            a.get("gov.decision"),
            a.get("gov.reason", ""),
            a.get("gov.cache", ""),
            a.get("gov.transport", "openapi"),
            a.get("http.status_code"),
        ))
    return rows


def _parse_verdicts(path: str) -> tuple[list[tuple], bool]:
    rows: list[tuple] = []
    p = Path(path)
    if not p.exists():
        return rows, True
    prev, chain_ok = "", True
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            chain_ok = False
            continue
        h = e.pop("hash", "")
        expect = hashlib.sha256((prev + json.dumps(e, sort_keys=True)).encode()).hexdigest()
        if e.get("prev") != prev or h != expect:
            chain_ok = False
        prev = h
        rows.append((e.get("ts"), e.get("batch_id"), bool(e.get("compliant")), e.get("policy", "")))
    return rows, chain_ok


def refresh(db_path: str, spans_path: str, verdicts_path: str) -> dict:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    calls = _parse_spans(spans_path)
    verdicts, chain_ok = _parse_verdicts(verdicts_path)

    con = duckdb.connect(db_path)
    try:
        con.execute("""
            CREATE OR REPLACE TABLE tool_calls(
                ts TIMESTAMP, server TEXT, tool TEXT, role TEXT, decision TEXT,
                reason TEXT, cache TEXT, transport TEXT, status INT)
        """)
        if calls:
            con.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?)", calls)
        con.execute("""
            CREATE OR REPLACE TABLE verdicts(
                ts TIMESTAMP, batch_id TEXT, compliant BOOLEAN, policy TEXT)
        """)
        if verdicts:
            con.executemany("INSERT INTO verdicts VALUES (?,?,?,?)", verdicts)
    finally:
        con.close()
    return {"tool_calls": len(calls), "verdicts": len(verdicts), "verdict_chain_ok": chain_ok}
