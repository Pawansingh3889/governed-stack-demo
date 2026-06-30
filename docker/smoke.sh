#!/usr/bin/env bash
# Smoke-test the containerized governed stack through the gateway.
# Usage: bash docker/smoke.sh [base_url]
set -u
B="${1:-http://localhost:8765}"
JSON="Content-Type: application/json"
pass=0; fail=0

# Wait until a forwarded tool call actually works -- this only succeeds once mcpo
# has mounted all its sub-apps, so it avoids the root-answers-before-mount race.
for _ in $(seq 1 45); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/sql-steward/run_checks" \
        -H "Authorization: Bearer manager-tok" -H "$JSON" -d '{}')
  [ "$c" = "200" ] && break
  sleep 2
done

ck() { if [ "$2" = "$3" ]; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1 (got $2 want $3)"; fail=$((fail+1)); fi; }
has() { if echo "$2" | grep -q "$3"; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1: $2"; fail=$((fail+1)); fi; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

ck "no token -> 401" \
   "$(code -X POST "$B/sql-steward/get_metric" -d '{}')" 401
ck "viewer get_metric -> 403 (OPA deny)" \
   "$(code -X POST "$B/sql-steward/get_metric" -H 'Authorization: Bearer viewer-tok' -H "$JSON" -d '{"metric":"mrr_total"}')" 403
ck "analyst .drop -> 403 (manager only)" \
   "$(code -X POST "$B/kql-sop/run_kql" -H 'Authorization: Bearer analyst-tok' -H "$JSON" -d '{"query":".drop table T"}')" 403
ck "manager .drop -> 200 (gateway allows)" \
   "$(code -X POST "$B/kql-sop/run_kql" -H 'Authorization: Bearer manager-tok' -H "$JSON" -d '{"query":".drop table T"}')" 200

has "manager email -> pii_blocked (in-tool gate)" \
    "$(curl -s -X POST "$B/sql-steward/get_records" -H 'Authorization: Bearer manager-tok' -H "$JSON" -d '{"entity":"customers","fields":["id","email"]}')" pii_blocked
has "kql-sop still blocks .drop underneath" \
    "$(curl -s -X POST "$B/kql-sop/run_kql" -H 'Authorization: Bearer manager-tok' -H "$JSON" -d '{"query":".drop table T"}')" '"blocked":true'
has "data-quality readiness ok" \
    "$(curl -s -X POST "$B/sql-steward/run_checks" -H 'Authorization: Bearer manager-tok' -H "$JSON" -d '{}')" '"status":"ok"'
has "doc-steward redacts PII" \
    "$(curl -s -X POST "$B/doc-steward/search_docs" -H 'Authorization: Bearer manager-tok' -H "$JSON" -d '{"query":"IT support","role":"viewer","k":1}')" REDACTED

echo "$pass passed, $fail failed"
