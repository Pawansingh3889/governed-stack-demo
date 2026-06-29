"""Governance gateway: the authenticated, policy-enforced front door to the stack.

It sits in front of mcpo. Every tool call is authenticated (token to role),
checked against an OPA/Rego policy (allow or deny for this role, tool, and
arguments), counted against a per-role budget, forwarded to mcpo only if allowed,
and recorded in a tamper-evident audit ledger. The in-tool gates still run
underneath, so this is defense in depth, not a replacement.

Run with: uvicorn gateway:app --port 8765   (stack.py does this for you)

Configuration (environment):
  MCPO_INTERNAL_URL   where mcpo listens (default http://localhost:8766)
  OPA_URL             OPA decision endpoint (default http://localhost:8181/v1/data/governed/decision)
  GATEWAY_TOKENS      comma list of token:role pairs
  GATEWAY_BUDGET      max allowed calls per role (default 500)
  GATEWAY_AUDIT_DB    audit ledger path (default logs/gateway-audit.db)
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCPO = os.environ.get("MCPO_INTERNAL_URL", "http://localhost:8766").rstrip("/")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181/v1/data/governed/decision")
BUDGET = int(os.environ.get("GATEWAY_BUDGET", "500"))
AUDIT_DB = os.environ.get("GATEWAY_AUDIT_DB", "logs/gateway-audit.db")


def _load_tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in os.environ.get("GATEWAY_TOKENS", "").split(","):
        if ":" in pair:
            tok, role = pair.split(":", 1)
            out[tok.strip()] = role.strip()
    return out


TOKENS = _load_tokens()
_spent: dict[str, int] = defaultdict(int)


def _setup_tracing():
    """Export every governance decision as an OpenTelemetry span.

    With OTEL_EXPORTER_OTLP_ENDPOINT set, spans go to a real collector (Jaeger,
    Grafana Tempo, an OTLP gateway). Without it, they are written as JSON lines to
    OTEL_SPAN_FILE so the export is visible with no collector to stand up. This is
    the interoperable trail; agent-blackbox remains the tamper-evident one.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "governed-gateway"}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    else:
        path = os.environ.get("OTEL_SPAN_FILE", "logs/otel-spans.jsonl")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        out = open(path, "a", encoding="utf-8")
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter(out=out, formatter=lambda s: s.to_json(indent=None) + "\n"))
        )
    trace.set_tracer_provider(provider)
    return trace.get_tracer("governed-gateway")


tracer = _setup_tracing()

app = FastAPI(title="governance-gateway")
client = httpx.AsyncClient(timeout=30.0)


def _role(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    return TOKENS.get(token.strip())


def _audit(actor: str, server: str, tool: str, outcome: str, reason: str, status=None) -> None:
    # Open the ledger per call so it is safe from any worker thread.
    try:
        from agent_blackbox import Ledger

        directory = os.path.dirname(AUDIT_DB)
        if directory:
            os.makedirs(directory, exist_ok=True)
        Ledger(AUDIT_DB).record(
            actor=actor,
            action=f"{server}/{tool}",
            target=server,
            meta={"tool": tool, "reason": reason, "status": status},
            outcome=outcome,
        )
    except Exception:
        pass


async def _decide(role: str, server: str, tool: str, args: dict) -> dict:
    payload = {"input": {"role": role, "server": server, "tool": tool, "args": args}}
    try:
        resp = await client.post(OPA_URL, json=payload)
        return resp.json().get("result", {"allow": False, "reason": "no policy result"})
    except Exception as exc:  # OPA down -> fail closed
        return {"allow": False, "reason": f"policy engine unreachable: {exc}"}


@app.post("/{server}/{tool}")
async def call_tool(server: str, tool: str, request: Request):
    with tracer.start_as_current_span(f"{server}/{tool}") as span:
        span.set_attribute("gov.server", server)
        span.set_attribute("gov.tool", tool)

        role = _role(request)
        span.set_attribute("gov.role", role or "anonymous")
        if role is None:
            span.set_attribute("gov.decision", "unauthenticated")
            return JSONResponse({"detail": "unauthenticated: provide a valid token"}, status_code=401)

        body = await request.body()
        try:
            args = json.loads(body) if body else {}
        except Exception:
            args = {}

        if _spent[role] >= BUDGET:
            span.set_attribute("gov.decision", "deny")
            span.set_attribute("gov.reason", "budget exceeded")
            _audit(role, server, tool, "deny", "budget exceeded")
            return JSONResponse({"detail": f"role '{role}' budget exceeded"}, status_code=429)

        decision = await _decide(role, server, tool, args)
        if not decision.get("allow"):
            span.set_attribute("gov.decision", "deny")
            span.set_attribute("gov.reason", decision.get("reason", ""))
            _audit(role, server, tool, "deny", decision.get("reason", ""))
            return JSONResponse(
                {"detail": f"denied by policy: {decision.get('reason')}", "policy": decision},
                status_code=403,
            )

        _spent[role] += 1
        resp = await client.post(
            f"{MCPO}/{server}/{tool}", content=body, headers={"content-type": "application/json"}
        )
        span.set_attribute("gov.decision", "allow")
        span.set_attribute("http.status_code", resp.status_code)
        _audit(role, server, tool, "allow", "forwarded", status=resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )


@app.get("/{path:path}")
async def proxy_get(path: str, request: Request):
    # Discovery (openapi.json, docs). Authenticated, but no policy/budget.
    if _role(request) is None:
        return JSONResponse({"detail": "unauthenticated: provide a valid token"}, status_code=401)
    resp = await client.get(f"{MCPO}/{path}")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
