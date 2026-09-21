"""A2A HTTP entrypoint."""

import os

from google.adk.a2a.utils.agent_to_a2a import to_a2a

from .agent import root_agent
from .identity import A2AAuthenticationMiddleware, KeycloakTokenValidator, WorkloadIdentityProvider

_a2a_app = to_a2a(root_agent, port=int(os.getenv("PORT", "8000")))
a2a_app = A2AAuthenticationMiddleware(
    _a2a_app,
    keycloak=KeycloakTokenValidator(
        issuer_url=os.getenv("KEYCLOAK_ISSUER_URL", ""),
        audience=os.getenv("KEYCLOAK_AUDIENCE", "rca-agent"),
        ca_bundle=os.getenv("KEYCLOAK_CA_BUNDLE"),
        timeout_seconds=float(os.getenv("KEYCLOAK_TIMEOUT_SECONDS", "5")),
    ),
    workload=WorkloadIdentityProvider(
        audience=os.getenv("SPIFFE_JWT_AUDIENCE", ""),
        timeout_seconds=float(os.getenv("SPIFFE_TIMEOUT_SECONDS", "5")),
    ),
)
