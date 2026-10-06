"""`mandate` -- the command line over the ledger.

`replay` is the one that matters. It re-runs stored decisions through the live
engine and fails loudly on any divergence, which is what makes the ledger a
record rather than a diary. Run it in CI and an engine change that silently
alters past verdicts stops being something you find out about later.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .engine.policy import Outcome
from .ledger.records import Ledger, ReplayMismatch, SignatureInvalid, WrongLedgerKey, key_id

DEFAULT_LEDGER = Path("var/decisions.jsonl")


def _key() -> bytes:
    key = os.environ.get("MANDATE_LEDGER_KEY", "")
    if not key:
        print(
            "MANDATE_LEDGER_KEY is not set. The ledger is HMAC-signed and cannot be\n"
            "read or written without it. See .env.example.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return key.encode("utf-8")


def _retired_keys() -> list[bytes]:
    raw = os.environ.get("MANDATE_LEDGER_RETIRED_KEYS", "")
    return [part.strip().encode("utf-8") for part in raw.split(",") if part.strip()]


def _ledger(path: Path) -> Ledger:
    return Ledger(path, _key(), retired_keys=_retired_keys())


def cmd_verify(args: argparse.Namespace) -> int:
    """Check every record, and keep going after the first failure.

    It used to stop at the first. That was wrong in a way worth recording: four
    records in this repository's own ledger had been signed with a rotated key, and
    stopping at the first reported one problem and hid three. "How much of the
    ledger is affected" is the first question anyone asks, and an early return
    cannot answer it.
    """
    ledger = _ledger(args.ledger)
    checked = 0
    by_key: dict[str, int] = {}
    rotated: list[str] = []
    broken: list[str] = []

    for record in ledger:
        checked += 1
        try:
            signed_by = ledger.check(record)
        except WrongLedgerKey as exc:
            rotated.append(str(exc))
        except SignatureInvalid as exc:
            broken.append(str(exc))
        else:
            by_key[signed_by] = by_key.get(signed_by, 0) + 1

    current = key_id(ledger.key)
    for signed_by, count in sorted(by_key.items()):
        where = "the configured key" if signed_by == current else "a retired key"
        print(f"{count} record(s) verified under {where} ({signed_by})")

    # stdout and stderr are buffered independently, so without this the summary
    # lands above the counts it summarises.
    sys.stdout.flush()

    for message in rotated:
        # Named apart from tampering because the remedy is different: this one is
        # fixed by putting the old key in MANDATE_LEDGER_RETIRED_KEYS, and nothing
        # about the records needs touching.
        print(f"UNVERIFIED (names another key): {message}", file=sys.stderr)
    for message in broken:
        print(f"TAMPERED: {message}", file=sys.stderr)

    if rotated or broken:
        print(
            f"\n{len(rotated) + len(broken)} of {checked} record(s) did not verify.",
            file=sys.stderr,
        )
        if rotated:
            print(
                "Records naming another key verify again once that key is listed in\n"
                "MANDATE_LEDGER_RETIRED_KEYS. Until then they are unverified, not excused:\n"
                "the key id is written outside the signature and can be forged.",
                file=sys.stderr,
            )
        return 1

    print(f"{checked} record(s) verified")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    ledger = _ledger(args.ledger)
    checked = 0
    failures = 0
    for record in ledger:
        if args.decision_id and record.decision_id != args.decision_id:
            continue
        checked += 1
        try:
            ledger.check(record)
            record.assert_replays()
        except (SignatureInvalid, ReplayMismatch) as exc:
            failures += 1
            print(f"FAIL {record.decision_id}: {exc}", file=sys.stderr)
            continue
        if args.verbose:
            print(f"ok   {record.decision_id}  {record.evaluation.outcome.value}")
    if args.decision_id and checked == 0:
        print(f"no record with id {args.decision_id}", file=sys.stderr)
        return 2
    print(f"{checked} record(s) replayed, {failures} divergence(s)")
    return 1 if failures else 0


def cmd_show(args: argparse.Namespace) -> int:
    ledger = _ledger(args.ledger)
    for record in ledger:
        if record.decision_id != args.decision_id:
            continue
        quote = record.quote
        print(f"decision   {record.decision_id}")
        print(f"evaluated  {record.evaluated_at.isoformat()}")
        print(f"merchant   {quote.merchant_id}  ({quote.merchant_name})")
        print(f"amount     {quote.declared_total}")
        print(f"outcome    {record.evaluation.outcome.value.upper()}")
        print(f"policy     {record.policy.policy_id} v{record.policy.version}")
        print("\nrule trace:")
        for result in record.evaluation.results:
            mark = {
                Outcome.ALLOW: "  ok  ",
                Outcome.HOLD_FOR_APPROVAL: " hold ",
                Outcome.DENY: " DENY ",
            }[result.outcome]
            if not result.applicable:
                mark = " n/a  "
            print(f"  [{mark}] {result.rule_id:<32} {result.message}")
        return 0
    print(f"no record with id {args.decision_id}", file=sys.stderr)
    return 2


def cmd_list(args: argparse.Namespace) -> int:
    ledger = _ledger(args.ledger)
    for record in ledger:
        print(
            f"{record.decision_id}  {record.evaluated_at.isoformat(timespec='seconds')}  "
            f"{record.evaluation.outcome.value:<18} {record.quote.declared_total!s:>14}  "
            f"{record.quote.merchant_id}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mandate", description=__doc__)
    parser.add_argument(
        "--ledger", type=Path, default=DEFAULT_LEDGER, help="path to the decision ledger JSONL"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify", help="check every record's signature")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("replay", help="re-run stored decisions through the engine")
    p.add_argument("decision_id", nargs="?", help="replay one decision instead of all")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("show", help="print one decision and its full rule trace")
    p.add_argument("decision_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="one line per decision")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
