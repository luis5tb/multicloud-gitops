"""A2A HTTP entrypoint."""

import os

from google.adk.a2a.utils.agent_to_a2a import to_a2a

from .agent import root_agent
from .identity import A2AAuthenticationMiddleware, KeycloakTokenValidator, WorkloadIdentityProvider
from .opa import OpaAuthorizer

_a2a_app = to_a2a(
    root_agent,
    host=os.getenv("A2A_PUBLIC_HOST", "localhost"),
    port=int(os.getenv("A2A_PUBLIC_PORT", os.getenv("PORT", "8000"))),
    protocol=os.getenv("A2A_PUBLIC_PROTOCOL", "http"),
)
a2a_app = A2AAuthenticationMiddleware(
    _a2a_app,
    keycloak=KeycloakTokenValidator(
        issuer_url=os.getenv("KEYCLOAK_ISSUER_URL", ""),
        audience=[
            item.strip()
            for item in os.getenv(
                "KEYCLOAK_AUDIENCES", os.getenv("KEYCLOAK_AUDIENCE", "rca-agent")
            ).split(",")
            if item.strip()
        ],
        ca_bundle=os.getenv("KEYCLOAK_CA_BUNDLE"),
        timeout_seconds=float(os.getenv("KEYCLOAK_TIMEOUT_SECONDS", "5")),
    ),
    workload=WorkloadIdentityProvider(
        audience=os.getenv("SPIFFE_JWT_AUDIENCE", ""),
        timeout_seconds=float(os.getenv("SPIFFE_TIMEOUT_SECONDS", "5")),
    ),
    # Optional: unset OPA_URL preserves this middleware's pre-OPA behavior of
    # trusting any caller Keycloak validates for the configured audience.
    opa=(
        OpaAuthorizer(
            url=os.getenv("OPA_URL", ""),
            timeout_seconds=float(os.getenv("OPA_TIMEOUT_SECONDS", "5")),
        )
        if os.getenv("OPA_URL")
        else None
    ),
)
