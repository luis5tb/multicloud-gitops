# ACME A2A agent

This is the first agent under `agents/`. It is a small Google Agent Development
Kit (ADK) agent that exposes its own A2A endpoint and routes each request to one
of several configurable downstream A2A sub-agents. It also serves a browser UI
from the same HTTP service.

The exposed `acme_agent` is a local ADK coordinator agent. Its configured
`RemoteA2aAgent` children are downstream peers, not the public entrypoint. In
the standalone deployment, ACME delegates Red Hat-related questions to the MAO
workflow, which selects Ask Red Hat for product/project/documentation guidance,
OpenShift Lightspeed (OLS) for cluster investigation, or both. ACME must not
reject a question just because it is not explicitly about OpenShift or does not
include a cluster URL; MAO owns specialist selection and cluster validation.

Each peer is attached via `sub_agents=` (not wrapped in `AgentTool`). The
root LLM delegates with `transfer_to_agent`; ADK's `RemoteA2aAgent` then
calls the remote with `message/stream` and yields each remote event
(`TaskStatusUpdate` / `TaskArtifactUpdate` / `Message`) incrementally. As a
`sub_agent`, those events propagate through this agent's own A2A
`message/stream` response, so the downstream workflow's progress and final
answer stream straight to the caller.

`AgentTool` was used here before, but `AgentTool.run_async` buffers the
sub-agent down to a single return value -- under `message/stream` that emits
nothing until the entire investigation finishes, starving long runs. We do
**not** set `mode="task"` (that needs ADK's `finish_task` handshake, which
the downstream OpenShift Lightspeed A2A endpoint does not implement);
the default mode streams events and treats stream end /
`TASK_STATE_COMPLETED` as completion. See `agents/AUTHENTICATION.md` for
the identity chain on each delegated call.

The current ACME → MAO deployment does not validate a cluster URL at ACME's
entrypoint. MAO selects and validates the target cluster and adds
`X-OLS-Cluster` on its call to Praxis. The old `ClusterRoutingMiddleware` and
cluster registry remain in the source for the direct ACME → Praxis → OLS path,
but are intentionally not wired into the deployed app.

## Project layout

```text
agents/acme_agent/
├── src/acme_agent/
│   ├── agent.py            # Local ADK router, remote sub-agents, A2A/HTTP app
│   ├── auth.py             # Keycloak client_credentials + SPIFFE/ZTO + audit
│   ├── cluster_context.py  # Per-invocation X-OLS-Cluster ContextVar
│   ├── cluster_registry.py # Allow-list + cluster-id derivation
│   ├── config.py           # Environment configuration helpers
│   ├── routing.py          # ClusterRoutingMiddleware (pre-ADK gate)
│   └── ui.py               # Small browser client
├── Containerfile
├── pyproject.toml
└── tests/
```

The Helm chart is at [`charts/all/acme-agent`](../../charts/all/acme-agent),
alongside every other component in this pattern.

## Local setup

Python 3.11 or newer is recommended.

```bash
cd agents/acme_agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Set the downstream A2A agent(s) via `REMOTE_A2A_AGENTS_JSON`, a JSON list of
`{name, description, endpoint}` objects -- there is no single-agent shorthand,
a one-item list is the single-agent case. Each `endpoint` can be either the
agent's base URL or its agent-card URL; when a base URL is supplied, the
agent appends ADK's well-known agent-card path automatically.

The local coordinator's model always goes through LiteLLM, never Google's
Gemini API directly:

```bash
export REMOTE_A2A_AGENTS_JSON='[{"name":"downstream_agent","description":"The configured downstream A2A agent.","endpoint":"http://localhost:8001"}]'
export A2A_AUTH_MODE=none
export ADK_MODEL='openai/acme-agent'
export LITELLM_API_BASE='http://localhost:4000'
export LITELLM_API_KEY='replace-me'
uvicorn acme_agent.agent:a2a_app --host 0.0.0.0 --port 8080
```

For multiple downstream agents, add more entries to the same list:

```bash
export REMOTE_A2A_AGENTS_JSON='[
  {"name":"inventory_agent","description":"Answers inventory questions","endpoint":"http://localhost:8001"},
  {"name":"orders_agent","description":"Answers order questions","endpoint":"http://localhost:8002"}
]'
```

Open <http://localhost:8080/ui> for the UI. The local service also publishes:

| URL | Purpose |
| --- | --- |
| `/ui` | Browser chat UI |
| `/healthz` | Kubernetes/OpenShift health endpoint |
| `/` | A2A JSON-RPC endpoint (POST) and UI redirect (GET) |
| `/.well-known/agent.json` or `/.well-known/agent-card.json` | A2A agent card, depending on the installed A2A SDK version |

The A2A endpoint can be called directly, for example:

```bash
curl -sS http://localhost:8080/ \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "local-test-1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "local-test-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "Hello from ACME"}]
      }
    }
  }'
```

Run the tests with:

```bash
python -m pytest
```

## Authentication for OpenShift

ACME authenticates **as itself** to call OpenShift Lightspeed through Praxis.
That grant is Keycloak **client_credentials** (Token A). ACME does **not**
perform the RFC 8693 token exchange -- OLS's A2A endpoint does that later
(subject_token=Token A → Token B for MCP). See `agents/AUTHENTICATION.md`.

The application supports three downstream authentication modes:

* `none`: send no authentication header; useful only for local development.
* `static`: read `A2A_BEARER_TOKEN` from the environment.
* `keycloak`: obtain and cache Token A from `KEYCLOAK_TOKEN_URL` using the
  `client_credentials` grant (SPIFFE JWT-SVID or client secret as the client
  authentication method).

When `A2A_AUTH_MODE=keycloak`, the embedded UI can choose between **Keycloak**
and **Red Hat IdP** for each request. Red Hat IdP supports the production and
stage environments and currently mints an **agent token** with
`grant_type=client_credentials`. The UI also shows a **user token** selector,
but selecting it fails closed with a clear message; authorization-code/PKCE
login is intentionally documented as TODO rather than partially implemented.
Non-UI A2A clients can make the same per-request choice with:

* `X-ACME-Auth-Provider: keycloak` or `redhat`
* `X-ACME-Auth-Environment: production` or `stage` (Red Hat IdP only)
* `X-ACME-Auth-Token-Type: agent` (user is rejected until implemented)

The selection is bound to the inbound request with a `ContextVar`; it chooses
which configured ACME client credential mints Token A and is never forwarded as
an authorization claim. Downstream MAS, Praxis, and OLS still independently
validate the resulting JWT.

For the OpenShift deployment, configure `keycloak` and set
`identity.spiffe.enabled=true` (chart value) so the agent fetches its own
short-lived JWT-SVID directly from ZTWIM/SPIRE through the Workload API
(`csi.spiffe.io`) instead of reading a static token file. That SVID is the
client assertion (`KEYCLOAK_CLIENT_ASSERTION_TYPE`, default
`urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`) when calling
Keycloak with `KEYCLOAK_CLIENT_AUTH_METHOD=client_assertion_post`. With
SPIFFE assertions, the token form must **omit** `client_id` -- Keycloak
resolves the client from the assertion's `sub` (a SPIFFE ID). This requires
a matching `ClusterSPIFFEID` (`identity.clusterSpiffeID.enabled=true` with
the cluster's trust domain) and a Keycloak client configured for federated
client authentication against the SPIFFE identity provider (see
`charts/all/keycloak-oidc`).

A static Secret or plain projected ServiceAccount token
(`ztoIdentity.enabled=true`) remains available as a fallback for a "ZTO" that
is a separate system from ZTWIM/SPIRE; it is mutually exclusive with
`identity.spiffe.enabled`. A normal Keycloak client-secret method can also be
selected when appropriate for the target realm.

The following settings are supported:

| Environment variable | Description |
| --- | --- |
| `REMOTE_A2A_AGENTS_JSON` | Required JSON list of downstream `{name, description, endpoint}` objects (a single downstream agent is still a one-item list) |
| `ADK_MODEL` | LiteLLM model string used by the local coordinator to select a sub-agent, e.g. `openai/acme-agent` |
| `LITELLM_API_BASE` | LiteLLM proxy base URL |
| `LITELLM_API_KEY` | LiteLLM proxy API key |
| `A2A_AUTH_MODE` | `none`, `static`, or `keycloak` |
| `A2A_AUTH_PROVIDER` | Default request provider: `keycloak` or `redhat` |
| `KEYCLOAK_TOKEN_URL` | Keycloak token endpoint (e.g. `https://keycloak.example/realms/rca/protocol/openid-connect/token`) |
| `KEYCLOAK_CLIENT_ID` | Keycloak client ID |
| `KEYCLOAK_CLIENT_SECRET` | Optional secret for `client_secret_*` methods |
| `KEYCLOAK_SCOPE` | Optional space-separated OAuth scopes |
| `KEYCLOAK_CLIENT_AUTH_METHOD` | `client_assertion_post`, `client_secret_basic`, or `client_secret_post` |
| `KEYCLOAK_CLIENT_ASSERTION_TYPE` | OAuth client assertion type; defaults to the SPIFFE JWT-SVID type |
| `SPIFFE_ENABLED` | When `true`, fetch the client assertion fresh from ZTWIM/SPIRE instead of `ZTO_IDENTITY_TOKEN_FILE` |
| `SPIFFE_ENDPOINT_SOCKET` | SPIRE Workload API socket, e.g. `unix:///spiffe-workload-api/spire-agent.sock` (the SPIFFE CSI driver always names the file `spire-agent.sock`) |
| `SPIFFE_JWT_AUDIENCE` | JWT-SVID audience requested from SPIRE. Must be the Keycloak realm issuer URL (e.g. `https://keycloak.example/realms/rca`), not this workload's own name -- Keycloak's federated-jwt client validator checks the client_assertion's `aud` against the realm issuer by default |
| `ZTO_IDENTITY_TOKEN_FILE` | File containing the ZTO-issued identity token (ignored when `SPIFFE_ENABLED=true`) |
| `ZTO_IDENTITY_TOKEN` | Optional environment fallback for the identity token |
| `ZTO_FORWARD_IDENTITY` | Also forward the identity in `ZTO_IDENTITY_HEADER` |
| `A2A_TLS_VERIFY` | Set to `false` only for local development with test certificates |
| `A2A_CA_BUNDLE` | Optional path to a PEM CA bundle used to verify downstream A2A and Keycloak HTTPS connections |
| `REDHAT_SSO_DEFAULT_ENVIRONMENT` | Default Red Hat IdP environment: `production` or `stage` |
| `REDHAT_SSO_TOKEN_TYPE` | Must be `agent`; `user` is not implemented yet |
| `REDHAT_SSO_SCOPE` | OAuth scope requested from Red Hat SSO; the pattern default is `openid api.ask_red_hat` |
| `REDHAT_SSO_PRODUCTION_TOKEN_URL` / `REDHAT_SSO_STAGE_TOKEN_URL` | Red Hat SSO token endpoints |
| `REDHAT_SSO_PRODUCTION_CLIENT_ID` / `REDHAT_SSO_STAGE_CLIENT_ID` | Confidential Red Hat SSO client IDs, supplied from Kubernetes Secrets |
| `REDHAT_SSO_PRODUCTION_CLIENT_SECRET` / `REDHAT_SSO_STAGE_CLIENT_SECRET` | Red Hat SSO client secrets, supplied from Kubernetes Secrets |
| `AUTH_TOKEN_DEBUG_ENABLED` | Enables the UI's one-time, short-lived access-token disclosure section |
| `AUTH_TOKEN_DEBUG_TTL_SECONDS` | Lifetime of a one-time token-debug entry |

For OpenShift routes signed by the cluster's managed ingress CA, the Helm
chart can sync that CA into the agent namespace (`auth.caBundleSync.enabled`),
mount it, and pass its path through `A2A_CA_BUNDLE`. A periodic sync refreshes
the bundle and rolls the Deployment when it changes, so the long-lived HTTPX
client reloads its TLS context. Keep `auth.tlsVerify=true`; disabling TLS
verification also affects the Keycloak token client.

`A2A_CA_BUNDLE` is added to the system's default trust store, not used in
place of it (`auth.py`'s `_tls_context_trusting`) -- confirmed necessary on a
live cluster whose ingress certificate chains to a public CA (ZeroSSL):
the synced managed-ingress-CA bundle only contains the leaf's intermediate
certificates, not the actual trusted root, so verifying against that bundle
alone (what `httpx`'s `verify=<path>` does by default) fails with
`CERTIFICATE_VERIFY_FAILED: unable to get issuer certificate` even though the
certificate itself is perfectly valid.

The exact Keycloak client policy is deployment-specific, so the chart exposes
the token URL, client-auth method, and SPIFFE/ZTO settings as values rather
than assuming a fixed installation.

### Red Hat IdP deployment notes

Enable `auth.redhatSso` only after registering confidential clients in both
Red Hat SSO environments (or disabling the unused environment). Set each
environment's `tokenUrl` in the deployment-specific values layer; the chart
creates an `ExternalSecret` named `acme-agent-redhat-sso` and injects both the
production/stage client IDs and client secrets from Vault. See
`values-secret.yaml.template` for the corresponding optional Vault entries.

The Red Hat SSO client must be allowed to obtain the downstream audience used
by MAS/OLS (`api.ask_red_hat` by default). Add the resulting
client-credentials principal (`sub` or `user_id`, not the client ID) to MAS
A2A's `a2a.allowedPrincipals`; MAS remains the inbound allow-list boundary.

**Live finding (2026-10-23):** selecting Red Hat SSO stage in the UI made ADK
report an agent-card fetch failure ending in `HTTP 401`, but the real failing
request was ACME's call to `sso.stage.redhat.com/.../token`. Reproducing that
one request with the synced Secret returned
`unauthorized_client: Client not enabled to retrieve service account`. The
stage client registration must enable service accounts (the
`client_credentials` grant); a confidential client secret alone is not enough.
Nothing caught this sooner because the unit tests mock a successful token
response and Helm can verify Secret syncing but not the external IdP's client
policy. ACME now surfaces the OAuth `error_description` in its exception and
audit line.

With `auth.tokenDebug.enabled=true`, the UI generates a private nonce, ACME
stores the outbound token under that nonce, and the UI retrieves it once from
`/ui/auth/token/<nonce>` within `AUTH_TOKEN_DEBUG_TTL_SECONDS`. The nonce is
not the logged `X-Request-Id`. Disable this value when token inspection is no
longer needed.

### Conversations

The UI keeps one conversation per browser tab: it sends the A2A `contextId`
returned by the first answer with every later message. ACME's ADK server uses
the `contextId` as its session ID, and ADK's `RemoteA2aAgent` then continues
the same downstream MAS context, so follow-up questions can refer to earlier
answers (MAS also passes its thread to OpenShift Lightspeed as one context).
"New conversation" starts over, and so does switching the identity provider or
Red Hat SSO environment, because a downstream conversation belongs to one
principal. ACME's sessions are in memory: a restarted ACME pod starts a fresh
session even when the browser still sends the old `contextId`, while MAS keeps
its side of the conversation. A turn that is still running (or paused) in MAS
makes the next message in that conversation fail as busy until it finishes.

### Token renewal

Downstream work can outlive Token A. A downstream A2A agent then either
answers HTTP 401, or pauses its task in `TASK_STATE_AUTH_REQUIRED` and waits
for a follow-up message on that task carrying a fresh token. MAS and OpenShift
Lightspeed both behave this way. ACME's HTTP client renews transparently
(`renewal.py`): it mints a new Token A for the same identity provider and
environment the request started with, resends the request (401) or sends the
follow-up on the paused task, and splices the new response in place of the
paused one. ADK never sees the pause. A Red Hat SSO stage task is therefore
always renewed with a stage token: MAS scopes tasks by the token's subject, so
a token from another provider would not match the task.

`auth.renewal.maxRenewals` (default 2) bounds renewals per downstream call;
after that, or if no token can be minted, the auth-required state reaches the
caller unchanged. Keep `auth.keycloak.tokenRefreshSkew` (default 90) above the
MAS worker's `credentialRenewal.minValiditySeconds` (60): a cached token with
less validity left would be refused as a renewal.

### Pasted tokens (testing)

With `auth.tokenOverride.enabled=true`, the UI offers "Use a pasted token for
the next request". The pasted token replaces Token A for that request's first
downstream call only; renewals mint fresh tokens with the selected provider,
so paste a token from that same provider. Combined with the token debug view,
this lets you reuse an earlier, now expired token (MAS answers 401) or a token
that is about to expire. Anyone with UI access could send arbitrary tokens
through ACME while this is on, so keep it off outside test environments.

**Seeing the paste and the renewal in the logs.** An expired paste is healed
silently by the renewal transport (MAS 401 → fresh mint → resend), and the
token debug view then shows the *renewed* token, not the pasted one — from the
UI alone you cannot tell whether the paste was ever sent (that exact confusion
cost a live debugging session on 2026-10-29). The `acme_audit` lines settle
it: the dispatch line carries `token=fp:<sha256-12>` plus
`source=pasted_override` or `source=minted` (fingerprints, never token
values) and `exp=<iso>` — the token's expiry decoded unverified, display
only — and a renewal logs `"renew Token A"` with `renewed_from=fp:…`
→ `token=fp:…` and `override=retired`. `bash scripts/monitor.sh` renders
these as **ACME DISPATCH** (with an EXPIRED marker on the expires line)
and **ACME TOKEN RENEWAL** cards, and MAS's
answering 401 as **MAS TOKEN REJECTED**.

`python -m acme_agent.mint_token [--provider redhat --environment stage]`
prints a freshly minted Token A inside the pod. It is used by
`scripts/a2a-renewal-scenarios.py`.

### Audit logging

`acme_audit` lines are INFO-level on the `acme_agent.*` loggers. Uvicorn's
default log config only configures the `uvicorn.*` loggers, so the app
installs its own stdout handler for `acme_agent` at import time
(`agent.py::_configure_logging`) — without it every INFO record is dropped by
Python's last-resort handler and the pod log shows *zero* audit lines even
for flows that clearly logged them. `ACME_LOG_LEVEL` (default `INFO`)
overrides the level.

**TODO: Red Hat IdP user tokens.** Use authorization-code flow with PKCE, a
per-user token cache keyed by the browser session, redirect URI registration,
CSRF/state validation, and the same downstream issuer/audience checks. The
confidential client will still be needed for the redirect, but its secret must
not be exposed to the browser.

## Build the image

The Helm chart deploys a pre-built image; it does not build an image in the
cluster. Choose a registry reachable by OpenShift, log in to it, then use the
same repository and tag for both `podman push` and Helm:

```bash
export IMAGE_REPOSITORY=quay.io/your-org/acme-agent
export IMAGE_TAG=0.1.0

podman login quay.io
podman build \
  --file Containerfile \
  --tag "${IMAGE_REPOSITORY}:${IMAGE_TAG}" \
  .
podman push "${IMAGE_REPOSITORY}:${IMAGE_TAG}"
```

For a local-only test, omit `podman push` and use a registry available to the
cluster, or load the image into the cluster’s development registry. The build
uses the UBI Python base image and installs the dependencies from
`pyproject.toml`.

## Deploy with Helm

The chart lives at `charts/all/acme-agent` and is wired into
`variants/standalone/values-standalone.yaml` as an Argo CD application. To
deploy it standalone (outside the pattern) instead:

```bash
helm upgrade --install acme-agent ../../charts/all/acme-agent \
  --namespace acme-agent --create-namespace \
  --set image.repository="${IMAGE_REPOSITORY}" \
  --set image.tag="${IMAGE_TAG}" \
  --set a2a.remoteAgents[0].name=downstream_agent \
  --set a2a.remoteAgents[0].description="The configured downstream A2A agent." \
  --set a2a.remoteAgents[0].endpoint=https://downstream.example.com/a2a \
  --set a2a.publicHost=acme-agent-acme-agent.apps.example.com
```

The important check is that the rendered Deployment contains the same image
reference that was pushed:

```bash
helm get manifest acme-agent -n acme-agent \
  | grep -A1 'image:'
oc get deployment acme-agent -n acme-agent \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

For more than one downstream agent, put the routing configuration in a values
file:

```yaml
a2a:
  remoteAgents:
    - name: inventory_agent
      description: Answers inventory questions
      endpoint: https://inventory.example.com/a2a
    - name: orders_agent
      description: Answers order questions
      endpoint: https://orders.example.com/a2a
```

For a cluster deployment authenticating with a static client secret, put it
in a Kubernetes Secret and configure `auth.keycloak.clientSecretSecret`. Do
not put the secret value in Git or in Helm values committed to the
repository. Prefer `identity.spiffe.enabled=true` (see above) so no secret is
needed at all. This repo wires the application into
`variants/standalone/values-standalone.yaml` as:

```yaml
acme-agent:
  name: acme-agent
  namespace: acme-agent
  argoProject: hub
  path: charts/all/acme-agent
  overrides:
    - name: image.repository
      value: quay.io/<your-quay-org>/acme-agent
    - name: image.tag
      value: <immutable-tag>
    # The MAO A2A Route. MAO chooses Ask Red Hat and/or OLS for each request;
    # google-adk's RemoteA2aAgent requires https (or loopback) for any agent
    # card it fetches. A single downstream agent is still a one-item list.
    - name: a2a.remoteAgents[0].name
      value: mas_orchestrator
    - name: a2a.remoteAgents[0].description
      value: MAO workflow for Red Hat questions, including product/project/documentation guidance through Ask Red Hat and OpenShift cluster investigation through OpenShift Lightspeed (OLS). Use for all Red Hat-related requests, including non-OpenShift questions; MAO selects the appropriate specialist(s).
    - name: a2a.remoteAgents[0].endpoint
      value: https://<mao-a2a-route>
    - name: auth.mode
      value: keycloak
    - name: auth.keycloak.issuerUrl
      value: https://<keycloak-route>/realms/rca
    - name: auth.keycloak.tokenUrl
      value: https://<keycloak-route>/realms/rca/protocol/openid-connect/token
    - name: auth.keycloak.clientId
      value: acme-agent
    - name: identity.spiffe.enabled
      value: "true"
    - name: identity.clusterSpiffeID.enabled
      value: "true"
    - name: identity.clusterSpiffeID.trustDomain
      value: <cluster trust domain>
```

The chart creates an `ExternalSecret` for `litellm.vaultKey` (default
`secret/data/global/acme-agent-litellm`).
To use a pre-existing Secret instead, set `litellm.existingSecret.name` and
leave `litellm.vaultKey` empty. No raw API key value is accepted by the
chart.

See `charts/all/acme-agent/values.yaml` for all deployment options, including
route settings, resource limits, ZTO/SPIFFE identity, and Keycloak settings.
