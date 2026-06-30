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

- **Auth at the gateway. (done)** A governance gateway now sits in front of
  mcpo on :8765. Every call carries a token, the token maps to a role, and an
  unauthenticated call is rejected. Token-to-role pairs are in `stack.env`;
  swapping in OIDC is the production step.
- **A gateway policy layer (OPA/Rego). (done)** OPA runs as a policy engine and
  decides allow or deny for every call by role, tool, and arguments, with a
  per-role budget and a tamper-evident audit record (agent-blackbox), before and
  after execution. Default deny. The per-tool gates stay underneath as defense in
  depth. Policy lives in `policy/governed.rego` and `policy/roles.json`.
- **Data-quality rules in the semantic layer. (done)** A `checks:` block in
  sql-steward's semantic layer declares assertions (not_null, unique, range,
  accepted_values, row_count_min) with error or warn severity. `run_checks`
  compiles each to a read-only violation count and returns a readiness score and
  an ok/degraded/failing status. The agent runs the declared checks; it cannot
  invent new ones. Tier 1 is now complete.

### Tier 2 — infrastructure and distribution

- **OpenTelemetry audit export. (done)** The gateway emits every decision
  (allow / deny / unauthenticated) as an OTel span with role, tool, decision,
  and reason. With `OTEL_EXPORTER_OTLP_ENDPOINT` set, spans ship to a collector
  (Jaeger, Tempo); blank writes them to `logs/otel-spans.jsonl`. agent-blackbox
  stays the tamper-evident record; OTel is the interoperable export.
- **Docker packaging. (done)** `docker/` has a Dockerfile, a three-service
  compose (opa + mcpo + gateway), and a smoke test. Built and verified in real
  containers (8/8 governance checks) using Docker running natively in WSL2, since
  Docker Desktop is broken on this machine by a corporate security filter driver.
  See `docker/README.md`.
- **Inference speed.** Reframed: vLLM needs a GPU the dev laptop does not have,
  so it would not help. The real fix is a smaller tool-capable model (mistral) or
  llama-swap. Treat the chat as garnish; `stack.py verify` is the fast proof.

### Tier 2 backlog — strengthens, already planned

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
