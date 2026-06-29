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
- Docker (for Open WebUI)
- Ollama running on the host, with a chat model pulled, for example `ollama pull llama3.1`
- The three server repos checked out locally (paths are set near the top of `setup.ps1`)

## Run it

```powershell
# 1. venv, install the three servers + mcpo, seed the sample data, render the config
.\setup.ps1

# 2. prove the governance holds end to end (starts mcpo, asserts, tears down)
.\.venv\Scripts\python.exe verify.py

# 3. start the gateway (leave it running)
.\.venv\Scripts\mcpo.exe --config mcpo.config.json --port 8765

# 4. start Open WebUI
docker compose up -d
```

Open `http://localhost:3000`. Under Settings, Tools, add three OpenAPI tool
servers:

- `http://host.docker.internal:8765/sql-steward`
- `http://host.docker.internal:8765/kql-sop`
- `http://host.docker.internal:8765/doc-steward`

Pick an Ollama model and start a chat with the tools enabled.

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
PII drive the role and redaction behaviour. `mcpo.config.example.json` is the
template for the gateway; `render_mcpo_config.py` stamps in absolute paths.
