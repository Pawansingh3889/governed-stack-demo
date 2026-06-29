# Roadmap and positioning

## Where this sits

MCP gateways already exist and are popular. mcp-proxy (~2.6k stars) bridges
transports; MCPJungle (~1.1k stars) manages and connects servers; mcpo (used
here) exposes them as OpenAPI. They are plumbing. None of them enforce
per-call governance.

Searching for the composed governance pattern (RBAC, PII handling, and audit
applied across SQL, KQL, documents, and memory) turns up adjacent pieces only:
single-surface SQL gateways and semantic layers, or tiny experiments with a
handful of stars. The gap is the governance layer itself.

So this is not another gateway. It is governance that lives inside each tool,
which a gateway cannot bypass: there is no way to add a tool that skips the
gate, because the gate is the tool. That framing matters when describing the
project. It plugs in alongside whatever gateway people standardise on, rather
than competing with it.

## Gateway landscape (reference)

| Gateway | What it is | Notes |
| --- | --- | --- |
| mcpo | MCP servers exposed as OpenAPI/REST | What this demo uses, because Open WebUI consumes OpenAPI tool servers directly. |
| mcp-proxy | Streamable HTTP / SSE to stdio bridge | Lower-level transport bridge; mcpo already covers our need. |
| MCPJungle | Self-hosted gateway plus registry | More capable management than mcpo. Worth tracking: if it becomes the default gateway, the pitch becomes "governance layer that plugs into MCPJungle." It manages, it does not govern. |

## Ideas

### Tier 1 — fills a real gap

- **Auth at the gateway.** mcpo currently has no key and no identity. Add an
  API key or OIDC check at the boundary (the on-prem equivalent of Entra).
- **A gateway policy layer (OPA/Rego).** Uniform RBAC, a context and cost
  budget, and one audit envelope around every call, before and after execution.
  The per-tool gates stay; this adds the cross-cutting concerns that should not
  be re-implemented in each tool. Matches the governance rail in the
  system-design diagram.
- **Data-quality rules in the semantic layer.** `semantic.yaml` already
  declares entities, metrics, and PII. Add assertions (not-null, ranges,
  freshness) that run and feed schema-scout's readiness score.

### Tier 2 — strengthens, already planned

- **pgvector for doc-steward and thread-recall.** Move off the hashing and
  in-memory defaults to persistent semantic backends. Reuses Postgres.
- **Context and cost budget across tools.** Extend sql-steward's per-role query
  budget to a gateway-wide token and cost cap.
- **An ingestion leg.** dlt to dbt to drt, or Dagster, so the stack covers
  load, model, and serve rather than serve only. A separate, larger demo.

### Tier 3 — cheap credibility

- **Map the audit to a named standard** (CoSAI, NIST AI RMF, or the EU AI Act
  audit requirements). A documented mapping is half a day and a strong
  positioning signal.
- **One-command distribution.** A Docker image or a pipx install so the stack
  runs without the editable-install setup.

### Out of scope for now

Broad warehouse connectors (ClickHouse, Dremio, Databricks), an agent interop
protocol, a full BI and visualisation leg, and the text-to-speech / image /
search extras that general local-LLM setups carry. Breadth without traction.

## Notes

- **Inference speed.** Ollama running qwen3:14b is slow on a machine without a
  GPU, which makes the chat demo drag. The fix is the inference engine, not the
  stack: vLLM, or llama-swap with a smaller tool-capable model. The governance
  proof (`stack.py verify`, `demo_proof.py`) does not depend on the chat and is
  the faster thing to show.
- **Deployment reference.** When packaging for a real on-prem host, systemd
  services and container hardening are the model (see varunvasudeva1/llm-server-docs
  for a worked Debian example), rather than running `stack.py` by hand.
