# Governed stack demo

One self-hosted agent that answers questions over a SQL database, a KQL cluster,
and a document corpus, where every answer goes through a governance gate. Unsafe
queries are refused, PII is blocked or redacted, document access is scoped by
role, and every read is audited. Nothing leaves the machine.

It composes three MCP servers that each enforce the same pattern on a different
surface:

| Server | Surface | Guarantee it enforces |
| --- | --- | --- |
| [sql-steward](https://github.com/Pawansingh3889/sql-steward) | SQL | No `run_sql` tool exists. The agent reads only what a semantic layer permits, queries are compiled from definitions, and PII-tagged fields are refused. |
| kql-sop | KQL | A gatekeeper lints before it runs. A query that mutates data or schema, or otherwise trips a blocking rule, is never executed. |
| doc-steward | Documents | Retrieval returns only chunks the caller's role may see, with PII redacted in the text before the model reads it. |

The three are wired into [Open WebUI](https://github.com/open-webui/open-webui)
through [mcpo](https://github.com/open-webui/mcpo), which exposes each MCP server
as an OpenAPI tool the chat model can call.

```
Open WebUI  ->  mcpo  ->  sql-steward  ->  SQLite (semantic layer + PII policy)
 (chat)        (proxy)    kql-sop       ->  KQL linter / gatekeeper
                          doc-steward   ->  in-memory RAG (role ACLs + redaction)
```

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

`stack.py up` prints the three tool URLs. To see governance immediately without
the chat UI, open any of them in a browser and use "Try it out":

- `http://localhost:8765/sql-steward/docs`
- `http://localhost:8765/kql-sop/docs`
- `http://localhost:8765/doc-steward/docs`

For the chat UI, `stack.py up --webui` serves Open WebUI on `http://localhost:8080`.
Under Settings, Tools, add the three servers as OpenAPI tool servers
(`http://localhost:8765/sql-steward`, `/kql-sop`, `/doc-steward`), pick an Ollama
model, and chat with the tools enabled.

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
