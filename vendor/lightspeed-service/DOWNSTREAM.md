# Downstream snapshot and A2A integration

This directory is a tracked source snapshot of
[`openshift/lightspeed-service`](https://github.com/openshift/lightspeed-service)
at commit `690939861bf7888209b1c9187614af3efd3a5a26` (2026-09-30). Upstream's
`LICENSE` and copyright notices are retained. No nested `.git` directory or
local virtual environment is part of the snapshot. The pinned embedding model
artifacts are included because the upstream `Containerfile` copies and
validates them as part of its supported image build.

## Rebuild

Use Python 3.12 and the upstream build/test flow from this directory:

```bash
make install-deps
make test-unit
podman build -f Containerfile -t lightspeed-service:a2a-dev .
```

The upstream `Containerfile` is retained. This is build input, not a published
or digest-pinned image; the operator's supported mechanism for selecting a
custom image has not been verified in this workstream. Do not deploy this tag
or claim an image build/provenance without the operator/image feasibility work.

The local `podman build` attempt during this work was blocked while installing
builder packages: `cdn.redhat.com` returned HTTP 403 for the pinned RHEL 9
builder image's BaseOS repository. Unit tests pass, but no local service image
was produced. Resolve authorized build-repository access or approve a supported
builder-base change before claiming the image build complete; do not add
subscription credentials to the repository.

## Refreshing the snapshot

Review the new upstream commit first. Fetch it into a separate temporary clone,
verify the exact commit, and export a clean archive (not a checkout with Git
metadata):

```bash
git init /tmp/lightspeed-service-update
git -C /tmp/lightspeed-service-update remote add origin \
  https://github.com/openshift/lightspeed-service.git
git -C /tmp/lightspeed-service-update fetch --depth=1 origin <reviewed-commit>
git -C /tmp/lightspeed-service-update rev-parse FETCH_HEAD
git -C /tmp/lightspeed-service-update archive FETCH_HEAD \
  | tar -xf - -C /tmp/lightspeed-service-update/archive
```

Compare that clean export with this directory before replacing anything. Keep
the local A2A changes in `ols/app/endpoints/a2a.py`,
`ols/app/routers.py`, `ols/app/endpoints/ols.py`,
`ols/src/query_helpers/a2a_context.py`,
`ols/src/query_helpers/docs_summarizer.py`, and
`tests/unit/app/endpoints/test_a2a.py` as an explicit reviewed patch set; do
not overwrite them as part of an upstream refresh. Re-run the focused tests and
the upstream unit suite, then update this pin and the chart documentation.

## A2A interface and current security boundary

The native endpoint serves a cluster-neutral card at
`/.well-known/agent-card.json` and handles blocking JSON-RPC `message/send`
at `/`. It builds the query with the upstream `LLMRequest` and invokes
`ols.app.endpoints.ols.conversation_request`, retaining the existing redaction,
conversation, quota, LLM, and tool pipeline. The A2A adapter passes no
client-provided MCP headers and uses an invocation-local context to configure,
discover, and execute tools only from the built-in server named exactly
`openshift`. The strict path bypasses ToolsRAG/cached server metadata, never
resolves Token B for another configured server, and fails closed if the built-in
server is absent or has no loaded tools. Token B is passed through the existing
`user_token`/`Authorization: kubernetes` resolver for that server only.

The identity provider validates Token A against the configured Keycloak
discovery/JWKS endpoints (RS256, exact issuer, required audiences, expiry,
issued-at, subject, and `azp`/client allow-list), fetches a fresh SPIFFE
JWT-SVID, and performs a per-request RFC 8693 exchange without a `client_id`
form field. It validates Token B's issuer, `openshift-mcp` audience,
`lightspeed-mcp` client, inherited subject, expiry, and `acme-agent-lightspeed` group
before returning the token to the existing `Authorization: kubernetes`
resolver. Keycloak discovery/JWKS public keys are briefly cached; caller and
exchanged tokens are not cached or stored in global state, contexts, or logs.

The provider is installed at service initialization only when all identity
settings are valid. Configure `OLS_A2A_KEYCLOAK_ISSUER_URL`, optional
`OLS_A2A_KEYCLOAK_CA_BUNDLE`, `OLS_A2A_INBOUND_AUDIENCE`,
`OLS_A2A_ALLOWED_CALLER_CLIENT_ID`, `OLS_A2A_EXCHANGE_CLIENT_ID`,
`OLS_A2A_MCP_AUDIENCE`, `OLS_A2A_SPIFFE_JWT_AUDIENCE`, and
`SPIFFE_ENDPOINT_SOCKET`. The SPIFFE audience must equal the exact Keycloak
realm issuer; TLS verification cannot be disabled. Also configure
`OLS_A2A_CLUSTER_ID` to this instance's registered ID and
`OLS_A2A_PUBLIC_URL` to the fixed Praxis HTTPS origin. Missing or invalid
identity configuration leaves the provider uninstalled and A2A RPC returns
503; absent/malformed bearer, missing/malformed target, target mismatch, and
bad identity claims fail closed. Stock REST authentication remains unchanged.

The synchronous endpoint does not advertise streaming and does not implement
`tasks/get`, `tasks/cancel`, or push notifications. Those need a reviewed
task-ownership/persistence design before support is claimed. Upstream's stock
REST authentication dependency is not changed; `/v1/query` remains on its
configured authentication module (production must keep `k8s`). This integration
does not set `dev_config.disable_auth` or select a `noop` module.

The downstream operator source now generates OAuth-required MCP config and
prevents tokenless ServiceAccount fallback, but its changes are unavailable in
an installed operator until a newly versioned bundle/catalog is built and
published. The OLSConfig chart remains disabled by default. See
[`charts/all/openshift-lightspeed-config/README.md`](../../charts/all/openshift-lightspeed-config/README.md).
