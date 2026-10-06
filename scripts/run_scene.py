#!/usr/bin/env python
"""Run a demo scene against the real gateway, the real merchant, and the real sandbox.

    ./scripts/serve.sh --with-proxy          # in one terminal
    python scripts/run_scene.py --list       # in another
    python scripts/run_scene.py injection

Eight scenes, eight different endings. Five of them need no deceived model and one
needs no attacker at all, which is the distribution the whole project argues
about: a control that only catches prompt injection catches one of the eight.

Nothing here decides anything. The scenes print what the gateway said, and where
an outcome depends on the model they report what the model actually did --
including when that is the inconvenient answer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from mandate.agent.backends import choose, configured_providers  # noqa: E402
from mandate.agent.budget import BudgetReached, SpendLedger  # noqa: E402
from mandate.demo.scenes import SCENES, SceneContext  # noqa: E402
from mandate.demo.show import BOLD, DIM, GREEN, RED, RESET, YELLOW, note, rule  # noqa: E402


def list_scenes() -> int:
    print(f"\n{BOLD}scenes{RESET}")
    width = max(len(name) for name in SCENES)
    for name, scene in SCENES.items():
        mark = f"{DIM}model{RESET}" if scene.uses_model else f"{GREEN}no model{RESET}"
        print(f"  {name:<{width}}  [{mark}]  {scene.headline}")
        print(f"  {' ' * width}            {DIM}{scene.expectation}{RESET}")
    print()
    return 0


async def preflight(gateway_url: str, merchant_url: str) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as http:
        for name, url in (
            ("gateway", f"{gateway_url}/health"),
            ("merchant", f"{merchant_url}/merchants"),
        ):
            try:
                (await http.get(url)).raise_for_status()
            except httpx.HTTPError as exc:
                print(f"{RED}{name} is not reachable at {url}{RESET}\n  {exc}", file=sys.stderr)
                print("\nStart both with ./scripts/serve.sh", file=sys.stderr)
                return False
        health = (await http.get(f"{gateway_url}/health")).json()
        if health.get("paypal") != "configured":
            print(
                f"{YELLOW}The gateway has no PayPal credentials, so an allowed purchase\n"
                f"will fail at order creation. Check .env.{RESET}",
                file=sys.stderr,
            )
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scene", choices=sorted(SCENES), nargs="?", default="injection")
    parser.add_argument("--list", action="store_true", help="describe every scene and exit")
    parser.add_argument("--instruction", help="override the scene's instruction to the agent")
    parser.add_argument("--gateway", default=os.environ.get("MANDATE_GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument("--merchant", default=os.environ.get("MANDATE_MERCHANT_URL", "http://localhost:8001"))
    parser.add_argument(
        "--proxy",
        default=os.environ.get("MANDATE_PROXY_URL", "http://localhost:8002"),
        help="the compromised tool server, for the hostile-proxy scene",
    )
    parser.add_argument("--cap", type=float, default=float(os.environ.get("MANDATE_LLM_CAP_USD", "5.00")))
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="how many times the duplicate scene's retry loop posts the same basket",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="seconds the non-delivery scene waits for a human to approve at PayPal",
    )
    parser.add_argument(
        "--provider",
        choices=("gemini", "claude"),
        help="which provider drives the agent. Defaults to MANDATE_PROVIDER, else the "
        "first configured key. The firewall behaves identically either way, which is "
        "the point of being able to choose.",
    )
    parser.add_argument(
        "--model",
        help="override the provider's default model; whether a model resists an "
        "injection is a property of that model, so it is worth measuring",
    )
    args = parser.parse_args()

    # The backend pauses to stay inside the free tier's five-requests-a-minute
    # window, and a pause nobody is told about is a hang. Shown at INFO, dimmed,
    # because it is the rig talking rather than the demo.
    logging.basicConfig(
        level=logging.INFO,
        format=f"{DIM}  %(message)s{RESET}",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.list:
        return list_scenes()

    scene = SCENES[args.scene]

    backend = None
    if scene.uses_model:
        try:
            backend = choose(args.provider, model=args.model)
        except RuntimeError as exc:
            print(f"{RED}{exc}{RESET}", file=sys.stderr)
            return 2

    if not await preflight(args.gateway, args.merchant):
        return 2

    rule(f"Scene: {args.scene} — {scene.headline}")
    note(scene.expectation)
    if backend is not None:
        note(f"provider: {backend.provider} · model: {backend.model}")
        note(f"configured: {', '.join(configured_providers())}")
    else:
        note("no model is involved in this scene")

    ctx = SceneContext(
        gateway_url=args.gateway,
        merchant_url=args.merchant,
        spend=SpendLedger(path=Path("var/llm_spend.jsonl"), cap_usd=args.cap),
        backend=backend,
        proxy_url=args.proxy,
        wait_seconds=args.wait,
        retries=args.retries,
        instruction_override=args.instruction,
    )

    try:
        code = await scene.run(ctx)
    except BudgetReached as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 1
    print()
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
