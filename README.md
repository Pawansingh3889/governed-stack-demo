# Governed stack demo

One self-hosted agent that answers questions over a SQL database, a KQL cluster,
and a document corpus, where every answer goes through a governance gate. Unsafe
queries are refused, PII is blocked or redacted, document access is scoped by
role, and every read is audited. Nothing leaves the machine.

It composes six MCP servers that each enforce the same pattern on a different
surface, in the order an agent works through a question:

| Server | Surface | Guarantee it enforces |
| --- | --- | --- |
| schema-scout | Discover | Read-only catalog tools (list/describe tables, find join paths) over a generated catalog, with PII flagged per column. |
| [sql-steward](https://github.com/Pawansingh3889/sql-steward) | Query | No `run_sql` tool exists. The agent reads only what a semantic layer permits, queries are compiled from definitions, and PII-tagged fields are refused. Declared data-quality checks (`run_checks`) report a readiness score. |
| kql-sop | KQL | A gatekeeper lints before it runs. A query that mutates data or schema, or otherwise trips a blocking rule, is never executed. |
| doc-steward | Documents | Retrieval returns only chunks the caller's role may see, with PII redacted before the model reads it. |
| thread-recall | Remember | Thread-scoped remember/recall. PII is masked before anything is stored, so long-term memory never retains raw PII, and one thread never recalls another's. |
| compliance-check | Decide | A composite verdict tool: the multi-step validation (cold-chain temperatures crossed with an allergen declaration check) runs deterministically in Python before the model sees anything. Verdicts fail closed and land in a hash-chained audit log; the model can only narrate a decision the code already made. |

The six are wired into [Open WebUI](https://github.com/open-webui/open-webui)
through [mcpo](https://github.com/open-webui/mcpo), which exposes each MCP server
as an OpenAPI tool the chat model can call. (schema-scout, thread-recall, and
compliance-check are optional, enabled by config.)

```mermaid
flowchart TD
    A["Agent — Open WebUI · Ollama (local)"] --> GW{{"governance gateway :8765<br/>auth · OPA/Rego policy · budget · audit"}}
    GW --> G["mcpo (internal :8766)"]
    G --> S1["schema-scout<br/>discover · PII flagged"]
    G --> S2["sql-steward<br/>query · no run_sql, PII blocked"]
    G --> S3["kql-sop<br/>KQL · mutations refused"]
    G --> S4["doc-steward<br/>docs · role-scoped, redacted"]
    G --> S5["thread-recall<br/>remember · masked on write"]
    G --> S6["compliance-check<br/>decide · verdict computed pre-LLM"]
    S1 --> B1[("catalog")]
    S2 --> B2[("SQLite / Postgres")]
    S3 --> B3[("KQL cluster")]
    S4 --> B4[("doc corpus")]
    S5 --> B5[("memory db")]
    S6 --> B6[("batch evidence")]
```

Two gates per call, on purpose. The gateway authenticates the caller (token to
role), asks OPA whether that role may call that tool with those arguments, counts
a per-role budget, writes a tamper-evident audit record, and exports the decision
as an OpenTelemetry span following the GenAI semantic conventions (`execute_tool`
spans with `gen_ai.*` attributes, so agent-observability backends like Jaeger,
Grafana, or Datadog group them natively, with the `gov.*` governance decision
carried on the same span). Repeat calls can be served from a governed cache:
exact-match on role, tool, and arguments (never embedding similarity), with a
TTL you set to your pipeline's refresh cadence — and a hit still passes auth,
policy, budget, and audit, so a revoked permission cuts off cached data too. Then the in-tool
gates run underneath (no run_sql, mutations refused, PII redacted), so even a
policy mistake cannot let a tool misbehave. The loop is discover → query → KQL →
docs → remember → decide. Nothing leaves the machine, and any backend swaps in
`stack.env` without changing the gates. Rendered diagrams are in [docs/](docs/).

## Prerequisites

- Python 3.11
- The three server repos checked out locally (paths are set near the top of `setup.ps1`)
- For the chat UI: Ollama running with a tool-capable model pulled (for example `ollama pull qwen3`). No Docker required.

## Run it

```powershell
# One-time: create the venv and install the three servers + mcpo
.\setup.ps1

# Everything else goes through stack.py
.\.venv\Scripts\python.exe stack.py up        # render config, seed demo data, start the gateway
.\.venv\Scripts\python.exe stack.py verify    # assert the governance holds
.\.venv\Scripts\python.exe stack.py status    # what's running and each tool's backend
.\.venv\Scripts\python.exe stack.py up --webui # also install and start Open WebUI (native, no Docker)
.\.venv\Scripts\python.exe stack.py down       # stop the gateway
```

`stack.py up` starts three processes (mcpo, OPA, the gateway) and prints the tool
URLs. The gateway requires a token: every call carries `Authorization: Bearer
<token>` (or `X-API-Key`), and the token's role drives the policy. The demo
tokens and the role each maps to are in `stack.env` (`GATEWAY_TOKENS`), and the
allow-lists are in `policy/roles.json`.

`stack.py verify` exercises both layers: it confirms an unauthenticated call is
rejected, that OPA denies a viewer the metric tool and denies an analyst a KQL
control command (only a manager may run one), and that the in-tool gates still
hold underneath.

For the chat UI, `stack.py up --webui` serves Open WebUI on `http://localhost:8080`.
Under Settings, Tools, add each server as an OpenAPI tool server
(`http://localhost:8765/sql-steward`, and so on) with a gateway token as the API
key, pick an Ollama model, and chat with the tools enabled.

## Run in containers

`stack.py` runs the stack as host processes. To run the governed backend (OPA +
mcpo + gateway) as containers instead, see [docker/](docker/): build the wheels,
`docker compose -f docker/compose.yaml up --build`, then `bash docker/smoke.sh`.
Built and verified with Docker running natively in WSL2.

## Adapt it to real infrastructure

Every backend is a line in `stack.env` (copy `stack.env.example`). Switching from
the offline demo to real on-prem infrastructure is a config change, not a code
change, and the governance applies identically either way:

| Change | Edit in `stack.env` |
| --- | --- |
| SQLite to Postgres | `SQL_STEWARD_DB_URL=postgresql+psycopg://user:pass@host:5432/db` |
| Hashing to real embeddings | `DOC_STEWARD_EMBED=ollama` |
| Validate-only to a live KQL cluster | set `KQL_SOP_CLUSTER` and `KQL_SOP_DATABASE` |
| A different corpus or semantic layer | `DOC_STEWARD_CORPUS` / `SQL_STEWARD_LAYER` |
| Schema discovery over your own schema | point `SCHEMA_SCOUT_CATALOG` at a catalog (the demo one is `schema-scout demo`; for a real SQL Server schema, generate it with `schema-scout run`) |

Re-run `stack.py up` and the gateway is rewired. `stack.py status` shows which
backend each tool is using.

## What to try, and what you should see

- "What is our total MRR by plan?" sql-steward compiles an approved metric and
  returns it. Ask it for customer email addresses and it refuses, because the
  field is tagged as PII in the semantic layer.
- "Run `.drop table StormEvents`" or a query with no time filter. kql-sop refuses
  the control command outright and flags the unbounded scan, returning the reason
  instead of executing.
- "What is the bonus pool?" doc-steward answers from the finance documents only
  if the role you pass is `finance`. A `viewer` is told nothing about them. Ask
  for the IT helpdesk contact and the email and phone come back redacted.

`verify.py` checks all of this without the UI and prints a pass or fail per
guarantee. It is the fastest way to confirm the stack is wired correctly.

## How it is put together

The demo adds no governance logic of its own. Each guarantee lives in the server
that owns it; this repo is the wiring and the sample data that make the three run
as one governed agent. The pieces are deliberately swappable: point sql-steward at
Postgres instead of the bundled SQLite, give kql-sop a real cluster, or back
doc-steward with pgvector and Ollama embeddings, and the same gates apply.

The sample data lives in `data/`: a semantic layer and a seeded SQLite database
for sql-steward, and a document corpus for doc-steward whose access scopes and
PII drive the role and redaction behaviour. `stack.py` reads `stack.env`, renders
`mcpo.config.json` (absolute paths, correct per-OS interpreter), seeds the demo
database, and manages the gateway lifecycle. `verify.py` is a self-contained
version of the checks that starts its own gateway and tears it down, for CI.
