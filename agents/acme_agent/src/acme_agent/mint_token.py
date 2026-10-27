"""Print a freshly minted downstream Token A (testing aid).

Run inside the ACME pod, where the Keycloak / Red Hat SSO client settings and
the SPIFFE Workload API socket are available::

    python -m acme_agent.mint_token                      # Keycloak
    python -m acme_agent.mint_token --provider redhat --environment stage

The token is written to stdout only, so it can be captured by a test driver
(see ``scripts/a2a-renewal-scenarios.py``). It is never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .auth import AuthSelection, DownstreamAuth


async def _mint(provider: str, environment: str) -> str:
    auth = DownstreamAuth()
    try:
        selection = AuthSelection(provider, environment, "agent")
        return await auth.bearer_token(selection, force_refresh=True)
    finally:
        await auth.close()


def main(argv: list[str] | None = None) -> int:
    """Mint one token and print it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=["keycloak", "redhat"], default="keycloak")
    parser.add_argument(
        "--environment", choices=["production", "stage"], default="production"
    )
    args = parser.parse_args(argv)
    environment = args.environment if args.provider == "redhat" else ""
    token = asyncio.run(_mint(args.provider, environment))
    if not token:
        print("No token minted: A2A_AUTH_MODE is none", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
