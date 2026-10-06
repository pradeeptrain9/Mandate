#!/usr/bin/env bash
# Create .env with fresh random secrets. Never overwrites an existing one.
#
# This exists because the repository ships no default ledger key and no default
# merchant secret, and that is a deliberate refusal rather than an omission. A
# committed HMAC key would mean every clone of this project signed its decision
# records with the same value, which would make "the ledger is signed" a claim
# worth nothing -- anyone could forge a record for anyone's deployment. So the
# gateway refuses to start without a key, and this generates one.
#
# Everything else is optional. The file it writes runs the refusal scenes and the
# human-approval path with no account anywhere; PayPal and Twilio credentials are
# added by hand afterwards and the README says what each one unlocks.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists; leaving it alone." >&2
  echo "Delete it yourself if you want fresh secrets -- rotating the ledger key" >&2
  echo "means moving the old one to MANDATE_LEDGER_RETIRED_KEYS, not dropping it," >&2
  echo "or every record written before now reports as unverifiable." >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "python3 is required to generate the secrets." >&2
  exit 2
fi

secret() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }

LEDGER_KEY="$(secret)"
MERCHANT_SECRET="$(secret)"

# Written from the example so the comments explaining every variable come too,
# rather than a bare list of names nobody can act on.
python3 - "$LEDGER_KEY" "$MERCHANT_SECRET" <<'PY'
import sys
from pathlib import Path

ledger_key, merchant_secret = sys.argv[1], sys.argv[2]
text = Path(".env.example").read_text()
for name, value in (
    ("MANDATE_LEDGER_KEY", ledger_key),
    ("MANDATE_MERCHANT_SECRET", merchant_secret),
):
    needle = f"{name}=\n"
    if needle not in text:
        raise SystemExit(f".env.example no longer has a blank {name}; not guessing where to put it")
    text = text.replace(needle, f"{name}={value}\n", 1)
Path(".env").write_text(text)
PY

# 600 before anything is in it would be better, but the file is written in one go.
# Same reasoning as the gateway's: the key is the only thing standing between the
# ledger and a forged record.
chmod 600 .env

echo "Wrote .env with a fresh ledger key and merchant secret (mode 600)."
echo
echo "Runs now, with no account anywhere:"
echo "  every refusal scene, the human-approval path, the dashboard, the ledger"
echo
echo "Needs credentials you add by hand:"
echo "  PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET  -- the scene where money actually moves"
echo "  PAYPAL_WEBHOOK_ID                        -- webhook ingestion (it refuses all without)"
echo "  TWILIO_* / MANDATE_APPROVER_NUMBER       -- approval by SMS instead of on screen"
