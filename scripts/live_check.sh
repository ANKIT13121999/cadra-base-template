#!/usr/bin/env bash
set -u

BASE_URL="${1:?usage: scripts/live_check.sh BASE_URL}"
BASE_URL="${BASE_URL%/}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; exit 1; }

code=$(curl -sS -o "$TMP/health" -w '%{http_code}' "$BASE_URL/health") || fail "GET /health"
[ "$code" = "200" ] || fail "GET /health"
pass "GET /health"

python - "inbox/public_tickets.json" > "$TMP/ticket.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    tickets = json.load(handle)
print(json.dumps(tickets[1]))
PY
code=$(curl -sS -o "$TMP/run.json" -w '%{http_code}' -H 'Content-Type: application/json' \
    -d @"$TMP/ticket.json" "$BASE_URL/tickets") || fail "POST /tickets"
[ "$code" = "200" ] || fail "POST /tickets"
pass "POST /tickets"

RUN_ID=$(python -c "import json,sys;print(json.load(sys.stdin)['run_id'])" < "$TMP/run.json")
PENDING=$(python -c "import json,sys;print(str(json.load(sys.stdin)['human_approval_required']).lower())" < "$TMP/run.json")
code=$(curl -sS -o "$TMP/get.json" -w '%{http_code}' "$BASE_URL/runs/$RUN_ID") || fail "GET /runs/{id}"
[ "$code" = "200" ] || fail "GET /runs/{id}"
pass "GET /runs/{id}"

[ "$PENDING" = "true" ] || fail "run is not pending approval"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d '{"decision":"approve","approver":"live-check"}' "$BASE_URL/runs/$RUN_ID/decision") || fail "approve #1"
[ "$code" = "200" ] || fail "approve #1"
pass "approve #1"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d '{"decision":"approve","approver":"live-check"}' "$BASE_URL/runs/$RUN_ID/decision") || fail "approve #2"
[ "$code" = "200" ] || fail "approve #2"
pass "approve #2"

code=$(curl -sS -o "$TMP/ledger.json" -w '%{http_code}' "$BASE_URL/ledger") || fail "GET /ledger"
[ "$code" = "200" ] || fail "GET /ledger"
ROWS=$(python -c "import json,sys; rows=json.load(sys.stdin); m=[r for r in rows if r['run_id']==sys.argv[1]]; print(len(m) if len(m)==1 and m[0]['status']=='APPROVED' else 0)" "$RUN_ID" < "$TMP/ledger.json")
[ "$ROWS" = "1" ] || fail "GET /ledger"
pass "GET /ledger"
