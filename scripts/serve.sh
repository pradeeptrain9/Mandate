#!/usr/bin/env bash
# Start the merchant stub and the gateway, and stop both on Ctrl-C.
#
# Two processes on purpose. The agent is a client like any other and the merchant
# is an untrusted third party; running them in one process would make it too easy
# to hand the gateway something an outside caller could not reach.
set -euo pipefail
cd "$(dirname "$0")/.."

# --with-proxy also starts the compromised tool server the hostile-proxy scene
# needs. Off by default: it is an attacker, and a demo rig that runs one without
# being asked is a demo rig nobody should copy into anything.
WITH_PROXY=0
for arg in "$@"; do
  case "$arg" in
    --with-proxy) WITH_PROXY=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -f .env ]]; then
  echo "No .env. Copy .env.example and fill it in." >&2
  exit 2
fi
set -a; . ./.env; set +a

for required in MANDATE_LEDGER_KEY MANDATE_MERCHANT_SECRET; do
  if [[ -z "${!required:-}" ]]; then
    echo "$required is empty in .env; the gateway cannot start without it." >&2
    exit 2
  fi
done

PY=.venv/bin/python
mkdir -p var

"$PY" -m uvicorn mandate.merchant.app:app --port 8001 --log-level warning &
MERCHANT=$!
"$PY" -m uvicorn mandate.gateway.api:create_app --factory --port 8000 --log-level warning &
GATEWAY=$!

PROXY=
if [[ "$WITH_PROXY" == 1 ]]; then
  MANDATE_MERCHANT_URL=http://localhost:8001 \
  MANDATE_TAMPER_MODE="${MANDATE_TAMPER_MODE:-inject}" \
  "$PY" -m uvicorn mandate.demo.hostile_proxy:build --factory --port 8002 --log-level warning &
  PROXY=$!
fi

cleanup() { kill "$MERCHANT" "$GATEWAY" ${PROXY:+"$PROXY"} 2>/dev/null || true; }
trap cleanup EXIT INT TERM

sleep 2
echo "merchant  http://localhost:8001/merchants"
echo "gateway   http://localhost:8000/health"
echo "docs      http://localhost:8000/docs"
if [[ "$WITH_PROXY" == 1 ]]; then
  echo "proxy     http://localhost:8002/_tamper   (compromised tool server)"
fi
echo
echo "A product page with the injection in it:"
echo "  http://localhost:8001/merchants/m_acme/products/SKU-PAPER-A4/page"
echo
echo "Ctrl-C to stop."
wait
