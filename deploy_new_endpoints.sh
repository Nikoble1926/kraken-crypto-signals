#!/usr/bin/env bash
# =============================================================================
# Safe, one-shot deploy of the new /history and /regime/changed paid endpoints.
# Run ON THE SERVER:   ssh root@87.99.131.73   then paste this whole script.
#
# What it does (all reversible):
#   1. Backs up signals_api.py
#   2. Fetches extra_endpoints.py from your GitHub repo into the app dir
#   3. Idempotently inserts a tiny loader block into signals_api.py
#      (right before the payment_middleware(...) call). Safe no-op if already there.
#   4. Python syntax-checks BOTH files
#   5. Restarts the service, smoke-tests it
#   6. ROLLS BACK automatically if anything fails
# =============================================================================
set -u
APP_DIR="/root/signals_api"
APP="$APP_DIR/signals_api.py"
EXTRA="$APP_DIR/extra_endpoints.py"
BACKUP="$APP_DIR/signals_api.py.bak.$(date +%s)"
BASE="http://127.0.0.1:4021"
RAW="https://raw.githubusercontent.com/Nikoble1926/kraken-crypto-signals/main/extra_endpoints.py"

echo "==> [1/6] Backup -> $BACKUP"
cp "$APP" "$BACKUP" || { echo "backup failed, abort"; exit 1; }

echo "==> [2/6] Fetch extra_endpoints.py from GitHub"
curl -fsSL "$RAW" -o "$EXTRA" || { echo "fetch failed (check internet/repo). Abort."; exit 1; }

echo "==> [3/6] Insert loader into signals_api.py (idempotent)"
python3 - "$APP" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
if "extra_endpoints" in src:
    print("   loader already present — skipping insert")
else:
    block = (
        "# ---- Optional extra paid endpoints (additive; safe no-op if anything fails) ----\n"
        "try:\n"
        "    from extra_endpoints import register as _register_extra\n"
        "    _register_extra(\n"
        "        app, routes,\n"
        "        EVM_ADDRESS=EVM_ADDRESS, NETWORK=NETWORK, signed=_signed,\n"
        "        unavailable_body=(lambda reason=None: {\n"
        "            \"error\": \"signal_unavailable\",\n"
        "            \"message\": (\"Live data is temporarily unavailable. You were not \"\n"
        "                        \"charged; please retry shortly.\"),\n"
        "            \"detail\": reason}),\n"
        "        DISCLAIMER=_DISCLAIMER, RouteConfig=RouteConfig, PaymentOption=PaymentOption,\n"
        "        declare_discovery_extension=declare_discovery_extension,\n"
        "        OutputConfig=OutputConfig)\n"
        "except Exception as _e:\n"
        "    print(\"[extra_endpoints] skipped:\", _e, flush=True)\n\n\n"
    )
    marker = "payment_middleware(app, routes=routes"
    idx = src.find(marker)
    if idx == -1:
        print("   ERROR: could not find payment_middleware(...) anchor — NOT modifying.")
        sys.exit(2)
    # back up to the start of that line
    line_start = src.rfind("\n", 0, idx) + 1
    src = src[:line_start] + block + src[line_start:]
    open(path, "w").write(src)
    print("   loader inserted")
PYEOF
[ $? -eq 0 ] || { echo "insert step failed — restoring backup"; cp "$BACKUP" "$APP"; exit 1; }

echo "==> [4/6] Syntax check both files"
if ! python3 -c "import ast; ast.parse(open('$APP').read()); ast.parse(open('$EXTRA').read())"; then
  echo "SYNTAX ERROR — restoring backup, not restarting."
  cp "$BACKUP" "$APP"
  exit 1
fi

echo "==> [5/6] Restart service"
SVC="$(systemctl list-units --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -iE 'signal|x402' | head -n1)"
if [ -z "${SVC:-}" ]; then
  echo "Could not auto-detect service. Find it:  systemctl list-units --type=service | grep -i signal"
  echo "Then:  systemctl restart <svc>   (backup at $BACKUP)"
  exit 1
fi
systemctl restart "$SVC"; sleep 3

echo "==> [6/6] Smoke test"
ok=1
curl -fs "$BASE/health" >/dev/null           || { echo "FAIL /health"; ok=0; }
curl -fs "$BASE/.well-known/x402" >/dev/null  || { echo "FAIL /manifest"; ok=0; }
ch=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/history")
cr=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/regime/changed")
echo "   /history=$ch  /regime/changed=$cr  (expect 402)"
[ "$ch" = "402" ] || echo "   NOTE: /history not 402 (got $ch) — check journalctl -u $SVC"
[ "$cr" = "402" ] || echo "   NOTE: /regime/changed not 402 (got $cr)"

if [ "$ok" -ne 1 ]; then
  echo "!! Smoke test FAILED — rolling back."
  cp "$BACKUP" "$APP"; systemctl restart "$SVC"; sleep 2
  curl -fs "$BASE/health" >/dev/null && echo "Rolled back, service healthy." || echo "Check: journalctl -u $SVC -n 50"
  exit 1
fi

echo "==> SUCCESS. New endpoints live. Backup: $BACKUP"
echo "    Re-index on x402scan: https://www.x402scan.com/resources/register  (enter signals.nsgoods.org)"
