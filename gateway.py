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
  GATEWAY_CACHE_TTL   governed response cache TTL in seconds (default 0 = off)
  GATEWAY_CACHE_MAX   max cached responses (default 512)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCPO = os.environ.get("MCPO_INTERNAL_URL", "http://localhost:8766").rstrip("/")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181/v1/data/governed/decision")
AUDIT_DB = os.environ.get("GATEWAY_AUDIT_DB", "logs/gateway-audit.db")


def _load_tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in os.environ.get("GATEWAY_TOKENS", "").split(","):
        if ":" in pair:
            tok, role = pair.split(":", 1)
            out[tok.strip()] = role.strip()
    return out


TOKENS = _load_tokens()
# Cumulative response bytes per role; passed to OPA so the per-role data budget
# (policy.roles[role].budget_bytes) is enforced as policy, not hardcoded here.
_spent_bytes: dict[str, int] = defaultdict(int)

# Governed response cache. Exact-match only: the key is the role plus the
# canonical call, never an embedding ("similar question" is not "same answer",
# and serving product A's cached result for a product B question is exactly the
# failure a governed stack must not have). A hit still runs auth, policy,
# budget, and audit -- only the forward to mcpo is skipped -- so a revoked
# permission or an exhausted budget cuts off cached data too. Set the TTL to
# your ETL micro-batch interval and staleness is bounded by pipeline cadence.
CACHE_TTL = int(os.environ.get("GATEWAY_CACHE_TTL", "0") or 0)
CACHE_MAX = int(os.environ.get("GATEWAY_CACHE_MAX", "512") or 512)
_cache: dict[str, tuple[float, bytes, str]] = {}


def _cache_key(role: str, server: str, tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([role, server, tool, args], sort_keys=True).encode()).hexdigest()


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


async def _decide(role: str, server: str, tool: str, args: dict, spent: int) -> dict:
    payload = {"input": {"role": role, "server": server, "tool": tool, "args": args, "spent": spent}}
    try:
        resp = await client.post(OPA_URL, json=payload)
        return resp.json().get("result", {"allow": False, "reason": "no policy result"})
    except Exception as exc:  # OPA down -> fail closed
        return {"allow": False, "reason": f"policy engine unreachable: {exc}"}


@app.post("/{server}/{tool}")
async def call_tool(server: str, tool: str, request: Request):
    # Span shape follows the OpenTelemetry GenAI semantic conventions for tool
    # execution ("execute_tool {name}", gen_ai.* attributes), so any agent
    # observability backend groups these correctly. The gov.* attributes are the
    # custom governance namespace carried alongside: every span answers both
    # "what tool ran" and "who was allowed to run it, and why".
    with tracer.start_as_current_span(f"execute_tool {server}/{tool}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("gen_ai.tool.name", tool)
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

        span.set_attribute("gov.spent_bytes", _spent_bytes[role])
        decision = await _decide(role, server, tool, args, _spent_bytes[role])
        if not decision.get("allow"):
            span.set_attribute("gov.decision", "deny")
            span.set_attribute("gov.reason", decision.get("reason", ""))
            _audit(role, server, tool, "deny", decision.get("reason", ""))
            return JSONResponse(
                {"detail": f"denied by policy: {decision.get('reason')}", "policy": decision},
                status_code=403,
            )

        key = _cache_key(role, server, tool, args)
        if CACHE_TTL > 0:
            hit = _cache.get(key)
            if hit and hit[0] > time.time():
                _spent_bytes[role] += len(hit[1])  # cached egress is still egress
                span.set_attribute("gov.decision", "allow")
                span.set_attribute("gov.cache", "hit")
                _audit(role, server, tool, "allow", "served from governed cache", status=200)
                return Response(content=hit[1], status_code=200, media_type=hit[2],
                                headers={"x-gov-cache": "hit"})

        resp = await client.post(
            f"{MCPO}/{server}/{tool}", content=body, headers={"content-type": "application/json"}
        )
        _spent_bytes[role] += len(resp.content)  # data egress counts toward the budget
        span.set_attribute("gov.decision", "allow")
        span.set_attribute("gov.cache", "miss" if CACHE_TTL > 0 else "off")
        span.set_attribute("http.status_code", resp.status_code)
        if resp.status_code >= 400:
            span.set_attribute("error.type", str(resp.status_code))
        _audit(role, server, tool, "allow", "forwarded", status=resp.status_code)
        if CACHE_TTL > 0 and resp.status_code == 200:
            if len(_cache) >= CACHE_MAX:
                now = time.time()
                for stale in [k for k, v in _cache.items() if v[0] <= now]:
                    _cache.pop(stale, None)
                while len(_cache) >= CACHE_MAX:  # still full -> drop oldest entries
                    _cache.pop(next(iter(_cache)), None)
            _cache[key] = (time.time() + CACHE_TTL, resp.content,
                           resp.headers.get("content-type", "application/json"))
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={"x-gov-cache": "miss"} if CACHE_TTL > 0 else None,
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
