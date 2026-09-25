"""Inbound caller authorization via a standalone OPA server.

This decides "may this caller invoke rca-agent at all" -- a question
independent of Keycloak token validation (identity.py's KeycloakTokenValidator):
a token can be cryptographically valid, issued by Keycloak, and carry the
right audience, and still belong to a caller with no business calling
rca-agent. Without this, "which callers are trusted" is an implicit side
effect of which Keycloak clients happen to share an audience with rca-agent,
rather than an explicit, reviewable allow-list a new caller has to be added
to on purpose (see charts/all/opa/values.yaml's policy.allowedCallers).

This is not a Kubernetes admission decision -- the request being gated is an
A2A HTTP call that never reaches the Kubernetes API server, so
ValidatingAdmissionPolicy/Gatekeeper (used for the namespace-scoping check in
charts/all/keycloak-oidc) cannot apply here. Hence a plain, self-run OPA
server queried directly over HTTP, same as charts/all/opa deploys.

This lives here, instead of in a proxy in front of rca-agent, only because
nothing currently sits in front of it -- see charts/all/opa/README.md's "Why
this lives in rca-agent's own code, and what would let it not" for the
proxy-based alternative this should move to if/when it matures.
"""

from __future__ import annotations

from typing import Any

import httpx


class OpaAuthorizationError(Exception):
    """Raised when OPA denies, or cannot be reached to make, a decision."""


class OpaAuthorizer:
    """Queries a standalone OPA server's Data API for one decision: allow."""

    def __init__(
        self,
        url: str,
        policy_path: str = "v1/data/rca/authorization/allow",
        timeout_seconds: float = 5.0,
    ) -> None:
        if not url:
            raise ValueError("OPA_URL must be configured")
        self.url = url.rstrip("/")
        self.policy_path = policy_path.lstrip("/")
        self.timeout_seconds = timeout_seconds

    def authorize(self, claims: dict[str, Any]) -> None:
        """Raise OpaAuthorizationError unless OPA explicitly allows this caller.

        Fails closed: a transport error, non-2xx response, or a missing/false
        "result" all deny -- the same posture as every other check in
        A2AAuthenticationMiddleware. `azp` (authorized party -- the
        confidential Keycloak client that requested the token, e.g.
        "ericsson-agent") is what identifies the caller here, not `sub`
        (an opaque per-service-account id -- see agents/AUTHENTICATION.md
        step 3).
        """

        azp = claims.get("azp")
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.post(
                    f"{self.url}/{self.policy_path}",
                    json={"input": {"azp": azp}},
                )
                response.raise_for_status()
                allowed = response.json().get("result") is True
        except Exception as error:
            raise OpaAuthorizationError(
                "OPA is unreachable or returned an invalid response"
            ) from error
        if not allowed:
            raise OpaAuthorizationError(f"OPA denied caller azp={azp!r}")

    def check_available(self) -> None:
        """Raise OpaAuthorizationError if the OPA server itself is unreachable."""

        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.get(f"{self.url}/health")
                response.raise_for_status()
        except Exception as error:
            raise OpaAuthorizationError("OPA is unreachable") from error
