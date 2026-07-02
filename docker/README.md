# Containerized governed stack

The governed backend as three containers: **opa** (policy engine, official image),
**mcpo** (spawns the five MCP servers), and **gateway** (the auth + policy front
door). Open WebUI is not included; run it natively and point it at the gateway.

```
Open WebUI (host) → gateway :8765 → mcpo (internal) → five MCP servers
                         │ auth · OPA decision · budget · audit
                         └→ opa (internal)
```

## Build and run

Five servers and their governance libraries are not on PyPI, so wheels are
built first, then the image installs them (compliance-check lives in this repo
and copies straight in):

```bash
# from the repo root
bash docker/build-wheels.sh                       # builds docker/wheels/*.whl
docker compose -f docker/compose.yaml up --build -d
bash docker/smoke.sh                              # 13 governance checks through the gateway
```

`smoke.sh` confirms both layers in the containers: an unauthenticated call is
rejected, OPA denies a viewer the metric tool and an analyst a KQL control
command (manager only), the in-tool gates still hold (PII blocked, mutations
refused, data-quality readiness, document redaction), compliance verdicts fail
closed, and the governed cache serves repeats without ever bypassing policy.

Call it like Open WebUI would, with a token as the API key:

```bash
curl -H "Authorization: Bearer manager-tok" http://localhost:8765/sql-steward/openapi.json
```

## Notes

- **This is built and tested with Docker running natively inside WSL2**, not
  Docker Desktop. On this machine Docker Desktop is broken by a corporate
  security filter driver; the WSL-native engine sidesteps it. From Windows, run
  these via `wsl -d Ubuntu docker ...`, or from inside the Ubuntu shell directly.
- **Persistence:** a WSL2 distro shuts down shortly after its last session ends,
  which stops the containers. To keep the stack running, keep a WSL session open
  (or use a keepalive). `restart: unless-stopped` brings the containers back
  whenever the distro and dockerd start again.
- **Tokens and policy** are the same as the native stack: `GATEWAY_TOKENS` in the
  compose file, allow-lists in `policy/roles.json`, rules in `policy/governed.rego`.
- The demo database and schema catalog are generated into the image at build time.
  Point the servers at real backends by changing the env in `compose.yaml` and
  `mcpo.docker.json`.
