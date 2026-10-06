#!/usr/bin/env bash
# Clear the demo's decision history so the rolling envelopes start empty again.
#
# Why this is needed, and why it is a separate script rather than an endpoint.
#
# The policy's envelopes are rolling windows -- $200/hour, $600/day -- computed
# from the ledger. That is the correct behaviour and it has already produced a
# genuine finding: a scene was refused by `envelope:hour` because an earlier run
# in the same hour had committed $85, with no attacker and no fooled model
# anywhere. But it also means running several scenes back to back exhausts the
# hour, and the fourth scene gets refused for a reason that is true but is not the
# reason the scene is demonstrating.
#
# So the reset lives here, out of process, and not as a gateway route. A service
# that can erase its own audit log on request is not a service you would put in
# front of money, whatever the demo convenience.
#
# This deletes the decision ledger and the hold database. Anything already
# authorized at PayPal stays authorized -- the sandbox does not know this file
# exists -- so release open holds first if you care about them:
#
#   curl -s localhost:8000/v1/ops/holds?state=held
#
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--yes" ]]; then
  cat >&2 <<'MSG'
This deletes the demo's decision ledger and hold database:

  var/decisions.jsonl   every signed decision record
  var/state.db          every hold, and its event history

Holds already placed at PayPal are not released by this -- check for open ones
first, with:

  curl -s localhost:8000/v1/ops/holds?state=held

Re-run with --yes to go ahead, and restart ./scripts/serve.sh afterwards.
MSG
  exit 2
fi

stopped=0
for pattern in "mandate.gateway.api" "mandate.merchant.app" "mandate.demo.hostile_proxy"; do
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    stopped=1
  fi
done
if [[ "$stopped" == 1 ]]; then
  echo "The gateway is still running and holds var/state.db open." >&2
  echo "Stop ./scripts/serve.sh first, then re-run this." >&2
  exit 2
fi

rm -f var/decisions.jsonl var/state.db var/state.db-shm var/state.db-wal
echo "Cleared. var/llm_spend.jsonl was left alone -- model spend is a real cost"
echo "and forgetting it would defeat the point of capping it."
