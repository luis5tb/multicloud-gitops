# Authentication flow: Ericsson UI to root-cause analysis

This documents the full per-request authentication path from a user typing a
message in the Ericsson agent's UI through to `rca_agent` triggering an
`AgenticRun` and returning a diagnosis. There is no single token used
end-to-end -- three separate Keycloak grants, two independent LLM
credentials, and one passthrough hop happen per request, each with its own
identity and failure mode.

## Two unrelated credential systems

It is easy to conflate these because both eventually show up as an
`Authorization` header somewhere, but they answer completely different
questions and must not be confused when debugging:

1. **LLM inference credentials (LiteLLM).** `LITELLM_API_KEY`/
   `LITELLM_API_BASE` (or provider-specific equivalents such as
   `OPENAI_API_KEY`). This is a **static, standing credential per
   deployment** -- every request from every user goes through the *same*
   key, and ericsson-agent and rca-agent each hold their *own*, independent
   key/Secret (`litellm.credentialsSecretName` in each chart) even though
   both talk to the same LiteLLM proxy. It answers "how does this agent's
   own reasoning step (the ADK `LlmAgent`/`LiteLlm` call) authenticate to the
   model backend", and has nothing to do with who is asking or what they're
   allowed to do. If this is broken, the agent can't think at all -- it
   fails before ever deciding whether to call a tool or a downstream agent
   (see the `litellm.InternalServerError` / `Model ... not found` failures in
   Troubleshooting below, both entirely independent of Keycloak/SPIFFE). A
   third, separate static LLM credential exists one hop further out:
   `lightspeed-agentic-operator` runs the actual analysis against its own
   configured `llmProvider.*` (e.g. Vertex Anthropic), which neither agent
   ever sees or authenticates to.
2. **Workload/caller identity tokens (SPIFFE JWT-SVID + Keycloak).**
   Everything else in this document. This is **dynamic and per-request**: it
   answers "which identity authorizes this specific tool call or
   downstream-agent call, on behalf of whom". ericsson-agent authenticates
   to Keycloak as *itself* (its own SPIFFE identity) to call rca-agent;
   rca-agent then authenticates to Keycloak as *itself* again, but the token
   it requests carries the *original caller's* identity as the exchange
   subject, so the eventual `AgenticRun` is attributable to the caller, not
   to rca-agent's own service identity. This is the delegation chain that
   makes "on behalf of X" authorization possible, and it is what steps 3-6
   of the sequence below implement.

In short: the LiteLLM keys decide *whether an agent can talk to its LLM at
all*; the SPIFFE/Keycloak chain decides *what the agent is allowed to do to
the cluster, and as whom*. A failure in one never explains a failure in the
other -- that's also why the diagram below draws the LiteLLM proxy as its own
participant instead of folding it into a `Note`.

## Sequence

```mermaid
sequenceDiagram
    participant User as Browser (UI)
    participant Eric as ericsson-agent
    participant LLM as LiteLLM proxy
    participant KC as Keycloak (rca realm)
    participant RCA as rca-agent
    participant MCP as openshift-mcp-server
    participant API as OpenShift API server
    participant Op as lightspeed-agentic-operator

    Note over KC,API: Pre-provisioned, done once, not per request:<br/>1) Authentication/cluster (oidcProviders) trusts KC as an<br/>   OIDC issuer and caches KC's JWKS for signature checks.<br/>2) claimMappings.groups reads KC's "groups" claim, prefixed<br/>   "keycloak:" -- emitted by a groups protocol mapper<br/>   attached to each Keycloak client (see table below).<br/>3) RBAC (e.g. ClusterRoleBinding rca-admins-cluster-admin)<br/>   targets Group:keycloak:&lt;kc-group&gt; directly -- there is no<br/>   separate OpenShift User object to pre-create.

    User->>Eric: POST / (A2A JSON-RPC message/send)

    Note over Eric,LLM: Static, standing credential (LITELLM_API_KEY) --<br/>same key for every request, carries no caller identity
    Eric->>LLM: chat completion (Authorization: Bearer LITELLM_API_KEY)
    LLM-->>Eric: tool-call decision: route to rca_agent

    Eric->>Eric: Fetch own JWT-SVID from ZTWIM/SPIRE<br/>(spiffe://.../ns/ericsson-agent/sa/ericsson-agent)
    Eric->>KC: POST /token, client_assertion=JWT-SVID<br/>(client_id=ericsson-agent, jwt-spiffe grant)
    KC-->>Eric: access_token

    Eric->>RCA: POST / (A2A), Authorization: Bearer <access_token>

    Note over RCA: A2AAuthenticationMiddleware
    RCA->>KC: GET /.well-known/openid-configuration + JWKS
    RCA->>RCA: Validate caller token (sig, iss, aud, exp)
    RCA->>RCA: Fetch own JWT-SVID from ZTWIM/SPIRE<br/>(spiffe://.../ns/lightspeed-agentic-operator/sa/rca-agent)

    Note over RCA,LLM: A different static LITELLM_API_KEY (own Secret) --<br/>same kind of credential as ericsson-agent's, still unrelated to the caller
    RCA->>LLM: chat completion (Authorization: Bearer LITELLM_API_KEY)
    LLM-->>RCA: tool-call decision: create_and_wait_for_analysis

    RCA->>KC: POST /token (RFC 8693 token exchange)<br/>subject_token=caller token<br/>client_assertion=RCA's own JWT-SVID<br/>requested aud=openshift-mcp
    KC-->>RCA: MCP-scoped access_token

    RCA->>MCP: resources_create_or_update(AgenticRun)<br/>Authorization: Bearer <MCP-scoped token>
    Note over MCP: cluster_auth_mode=passthrough:<br/>forwards the bearer token as-is
    MCP->>API: create AgenticRun CR, Authorization: Bearer <token>
    API->>KC: (cached JWKS, not fetched per request)<br/>verify signature -- check iss/aud against oidcProviders
    API->>API: Apply claimMappings to username/groups,<br/>then RBAC against the pre-provisioned bindings above
    API-->>MCP: 201 Created
    MCP-->>RCA: AgenticRun created

    Op->>Op: Watches AgenticRuns in its namespace
    Note over Op: Runs analysis using its own configured LLM<br/>provider (llmProvider.*, e.g. Vertex Anthropic) --<br/>a third static credential, independent of LiteLLM or Keycloak
    Op->>Op: Writes AnalysisResult

    loop poll until Analyzed or timeout
        RCA->>MCP: resources_get(AgenticRun status)
        MCP->>API: get AgenticRun, Authorization: Bearer <token>
        API-->>MCP: status
        MCP-->>RCA: status
    end

    RCA->>MCP: resources_get(AnalysisResult)
    MCP->>API: get AnalysisResult
    API-->>MCP: diagnosis + remediation proposals
    MCP-->>RCA: AnalysisResult

    RCA-->>Eric: A2A response: diagnosis + proposals
    Eric-->>User: Rendered response in UI
```

## Keycloak <-> OpenShift group mapping (pre-provisioned, not per-request)

The API server never calls Keycloak "live" to ask whether a token is
authorized -- everything below is configured once by `charts/all/keycloak-oidc`
and then only read from cache on the hot path:

1. **Trust**: `Authentication/cluster`'s `spec.oidcProviders[0]` (rendered by
   `templates/openshift-authentication.yaml`) names Keycloak's realm issuer
   URL and fetches/caches its JWKS for signature verification. Any audience a
   token was issued for must be listed in `issuer.audiences`
   (`keycloak.clientId`, `keycloak.consoleClientId`, plus
   `openshiftOIDC.extraAudiences` -- this is exactly the `openshift-mcp` gap
   covered in Troubleshooting/step 5 below).
2. **Claim mapping**: `claimMappings.groups.claim` (`openshiftOIDC.groupsClaim`,
   default `groups`) tells the API server which token claim carries group
   membership, and `claimMappings.groups.prefix` (`openshiftOIDC.groupsPrefix`,
   default `keycloak:`) is prepended to every value. The `groups` claim itself
   only appears in a token if the *client that issued it* has a `groups`
   protocol mapper attached -- there is no realm-wide default, which is
   exactly the console/CLI gotcha in Troubleshooting below.
3. **RBAC**: bindings such as `admin-rbac.yaml`'s
   `<adminGroupName>-cluster-admin` `ClusterRoleBinding` target
   `Group:keycloak:<adminGroupName>` directly. There is no separate OpenShift
   `User`/`Group` object to provision -- the prefixed claim value *is* the
   RBAC subject the moment a valid token presents it.

The one manual, one-time step this doesn't template (Keycloak-version-specific
UI, see `charts/all/keycloak-oidc/README.md`): creating the actual Keycloak
group (e.g. `rca-admins`) and adding users to it. Everything downstream of
that -- the claim appearing in tokens, the API server trusting it, and RBAC
resolving it -- is what steps 1-3 above wire up automatically.

## The five Keycloak clients

Defined by `charts/all/keycloak-oidc`, in the `rca` realm. Conflating any of
these breaks the flow -- see that chart's README for the exact rationale.

| Client | Type | Used by | Purpose |
| --- | --- | --- | --- |
| `rca-agent` | public | Browser / `oc login` | Native OIDC login only. Also registered as the `cli` OIDC platform client. Cannot authenticate itself. |
| `openshift-console` | confidential | Web console | Registered as the `console` OIDC platform client. Needs the `groups` protocol mapper (see Troubleshooting) or RBAC group membership never reaches the console. |
| `ericsson-agent` | confidential | ericsson-agent | Client-credentials-style grant authenticated with ericsson-agent's own SPIFFE JWT-SVID (`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`) instead of a static secret. |
| `rca-agent-mcp` | confidential | rca-agent | RFC 8693 token exchange: takes the caller's token as `subject_token`, authenticates itself with its own SPIFFE JWT-SVID, and requests a token scoped to the `openshift-mcp` audience. |
| `openshift-mcp` | confidential | (never authenticates) | Exists only so `rca-agent-mcp`'s exchange has a real `client_id` to name as its `audience` -- Keycloak's standard token exchange requires that parameter to be an actual client. |

`ericsson-agent` and `rca-agent-mcp` authenticate via Keycloak's federated
client authentication feature against the `spiffe` identity provider this
chart also creates -- fully declarative
(`keycloak.spiffeIdentityProvider.*`/`clientAuthenticatorType: federated-jwt`
in `charts/all/keycloak-oidc`), no manual Admin Console step required. See
that chart's README for the exact mechanics, including two non-obvious
requirements this doc's Troubleshooting section below also covers: the
`client_id` form parameter must be omitted from these requests, and the
SPIFFE JWT-SVID used as `client_assertion` must be requested with the
Keycloak realm issuer URL as its audience, not the workload's own name.

## Per-hop identity and validation

Every JWT sample below is illustrative -- field names and the exact claim set
depend on this realm's protocol mappers, not a spec all Keycloak realms share
-- but the shapes match what this repo's charts actually configure. Watch
`sub`/`azp`/`aud` change hop to hop; that's the whole "who, calling what, on
whose behalf" story in one column.

1. **User to ericsson-agent**: no authentication by default (`auth.mode` on
   the caller-facing side is out of scope here -- this doc covers the
   *downstream* auth ericsson-agent performs, not who's allowed to open the
   UI). No token exists yet, which matters below: nothing upstream of step 3
   ever identifies the human at the keyboard.
2. **ericsson-agent to LiteLLM** (`agents/ericsson_agent/src/ericsson_agent/agent.py`,
   `_model()`): every reasoning step of the local ADK coordinator (including
   the decision to route to `rca_agent`) goes through `LiteLlm(base_url=...,
   api_key=...)`, populated from `LITELLM_API_BASE`/`LITELLM_API_KEY` -- a
   static credential from ericsson-agent's own `litellm.credentialsSecretName`
   Secret, unrelated to the caller and to steps 3-8 below.

   ```
   Authorization: Bearer sk-litellm-REDACTED
   ```

   Opaque, not a JWT -- there is nothing to decode. This key identifies the
   *deployment*, not any caller; the same value is sent for every request.
3. **ericsson-agent to rca-agent** (`agents/ericsson_agent/src/ericsson_agent/auth.py`,
   `DownstreamAuth`): fetches a JWT-SVID from the SPIFFE Workload API for
   audience `identity.keycloak.issuerUrl` (the Keycloak realm issuer --
   *not* `ericsson-agent`; see Troubleshooting), uses it as `client_assertion`
   in a Keycloak `client_credentials` token request for the confidential
   `ericsson-agent` client (with no `client_id` form parameter -- also see
   Troubleshooting), and attaches the resulting access token as
   `Authorization: Bearer` on every outbound A2A request (`httpx`
   `event_hooks`).

   Real captured shape (irrelevant claims trimmed):

   ```json
   {
     "iss": "https://keycloak.apps.<cluster-domain>/realms/rca",
     "sub": "0e844946-0456-475b-9265-e17532b362c9",
     "azp": "ericsson-agent",
     "aud": ["rca-agent", "rca-agent-mcp", "account"],
     "preferred_username": "service-account-ericsson-agent",
     "scope": "email profile",
     "exp": 1790256558,
     "iat": 1790256258
   }
   ```

   **This is the token that ends up as "on behalf of" for the rest of the
   chain.** Because step 1 authenticates no one, `sub` here is
   ericsson-agent's own service account, not the human using the UI --
   everything downstream is attributable to ericsson-agent-as-caller, not to
   an end user. Note `sub` is an opaque internal user id, not the readable
   `service-account-ericsson-agent` string (that only appears in
   `preferred_username`) -- `identity.py`'s `sub -> client_id -> azp ->
   preferred_username` fallback picks `sub` first, so
   `agentic.openshift.io/on-behalf-of` on the AgenticRun will actually read
   that opaque id, not a human-readable name. If per-user attribution is
   ever needed, a user identity has to be captured before this hop and
   folded into this token (e.g. a second token exchange).
   `rca-agent`/`rca-agent-mcp` both appear in `aud` because rca-agent's own
   inbound check needs `rca-agent`, and Keycloak's standard token exchange
   (step 6) separately requires the subject_token to already carry the
   exchanging client (`rca-agent-mcp`) as an audience -- both are protocol
   mappers on the `ericsson-agent` client, see `charts/all/keycloak-oidc`.
4. **rca-agent validates the inbound token** (`agents/rca_agent/rca_agent/identity.py`,
   `A2AAuthenticationMiddleware`): every path except `/.well-known/agent-card.json`
   and `/health/ready` requires both (a) `KeycloakTokenValidator.validate` --
   signature via JWKS, issuer, and audience against `KEYCLOAK_ISSUER_URL`/
   `KEYCLOAK_AUDIENCES` -- and (b) `WorkloadIdentityProvider.get_identity` --
   RCA's own JWT-SVID must be obtainable at all, independent of the caller's
   token. Either failing returns a generic `401` (the specific cause is only
   in the pod logs, deliberately -- see Troubleshooting).

   ```
   Checks run against the step-3 token by KeycloakTokenValidator.validate():
     iss == KEYCLOAK_ISSUER_URL        (https://keycloak.../realms/rca)
     aud ∩ KEYCLOAK_AUDIENCES != {}    ({"rca-agent", "openshift"})
     exp/iat within tolerance, signature verified via JWKS
     sub required (identity.py falls back sub -> client_id -> azp ->
                   preferred_username to build on_behalf_of)
   ```

   RCA's own JWT-SVID, fetched independently of the caller token. `aud` is
   the Keycloak realm issuer, same reasoning as step 3 -- this JWT-SVID's
   only consumer is the `client_assertion` on the exchange in step 6 below:

   ```json
   {
     "sub": "spiffe://apps.<cluster-domain>/ns/lightspeed-agentic-operator/sa/rca-agent",
     "aud": ["https://keycloak.apps.<cluster-domain>/realms/rca"],
     "exp": 1732000300
   }
   ```
5. **rca-agent to LiteLLM** (`agents/rca_agent/rca_agent/agent.py`, `_model()`):
   same pattern as step 2, a *different* static credential from rca-agent's
   own `litellm.credentialsSecretName` Secret, used when its `LlmAgent`
   decides whether/how to call the `create_and_wait_for_analysis` tool. Never
   the caller's Keycloak token.

   ```
   Authorization: Bearer sk-litellm-REDACTED   (rca-agent's own key, different
                                                 value from step 2's)
   ```
6. **rca-agent to OpenShift MCP** (`agents/rca_agent/rca_agent/mcp_agentic_run.py`
   + `identity.py`'s `KeycloakTokenExchanger`): the caller's *validated*
   token is used only as the `subject_token` of an RFC 8693 exchange -- it is
   never forwarded to MCP itself. RCA authenticates the exchange call with
   its own JWT-SVID as `client_assertion` on the confidential `rca-agent-mcp`
   client, requesting the `openshift-mcp` audience
   (`identity.keycloak.tokenExchange.audience`). The resulting MCP-scoped
   token is what actually gets sent to `openshift-mcp-server`.

   ```
   Token-exchange request (KeycloakTokenExchanger.exchange):
     grant_type=urn:ietf:params:oauth:grant-type:token-exchange
     subject_token=<step 3's access token>
     subject_token_type=urn:ietf:params:oauth:token-type:access_token
     requested_token_type=urn:ietf:params:oauth:token-type:access_token
     client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe
     client_assertion=<rca-agent's own JWT-SVID from step 4>
     audience=openshift-mcp
   ```

   No `client_id` here either, same reason as step 3. Real captured shape:

   ```json
   {
     "iss": "https://keycloak.apps.<cluster-domain>/realms/rca",
     "sub": "0e844946-0456-475b-9265-e17532b362c9",
     "aud": ["openshift-mcp"],
     "azp": "rca-agent-mcp",
     "preferred_username": "service-account-ericsson-agent",
     "exp": 1790256632
   }
   ```

   - **`sub` -- whose authority is this?** Carried over unchanged from the
     subject token (step 3): ericsson-agent's service account (the same
     opaque id, not rca-agent's). `sub` is what `identity.py` reads (with the
     `sub -> client_id -> azp -> preferred_username` fallback from step 4)
     into `on_behalf_of`, annotated on the AgenticRun as
     `agentic.openshift.io/on-behalf-of`. So "on behalf of X" means **X is
     the subject being represented**, not the agent doing the representing --
     the naming is backwards from how it reads at first glance.
   - **`azp` -- who is this specific token issued to / allowed to present
     it?** `rca-agent-mcp`: the client that called the token endpoint, so it
     is the party the resulting token is handed to.
   - **No `act` claim.** RFC 8693 defines an `act` (actor) claim for exactly
     this "X's authority, exercised by Y" case, and an earlier version of
     this doc described one -- but Keycloak's *standard* (V2) token exchange,
     which is what's enabled on this cluster, does not populate it. Keycloak
     has a separate, additional "Token Exchange Delegation" feature
     (`delegation:client` client scope, its own Fine-Grained Admin
     Permissions v2 grant) that adds an `act`/`may_act` claim -- this repo
     does not enable it, so `identity.py`'s `actor` property will not find
     one and `agentic.openshift.io/previous-actor` will not be set on the
     AgenticRun. The only actor information available in practice is `azp`.
7. **openshift-mcp-server to the API server**: `cluster_auth_mode=passthrough`
   means MCP does no authorization decision itself -- it forwards the
   MCP-scoped bearer token straight through to the Kubernetes API server as
   the caller's own credential. The API server's own OIDC authenticator (via
   `Authentication/cluster`) makes the actual RBAC decision by validating the
   token against its cached Keycloak JWKS and then applying `claimMappings`
   (see "Keycloak <-> OpenShift group mapping" above) -- it does not call
   Keycloak per request. This means `openshift-mcp` **must** be one of
   `Authentication.spec.oidcProviders[].issuer.audiences`
   (`openshiftOIDC.extraAudiences` in `charts/all/keycloak-oidc`) or every
   MCP call is rejected at this hop even though the token exchange in step 6
   succeeded cleanly.

   ```
   claimMappings applied to the step-6 token:
     username: claim "sub"    -> "keycloak:service-account-ericsson-agent"
     groups:   claim "groups" -> [] (absent -- the ericsson-agent client has
                                     no groups protocol mapper configured)
   ```

   No group means RBAC has to grant that exact prefixed username, not just a
   group, or this hop 403s even after authentication succeeds cleanly -- the
   same class of gap as the console client's missing `groups` mapper in
   Troubleshooting, just on the service-account side instead of the
   human-login side.
8. **AgenticRun -> AnalysisResult**: `lightspeed-agentic-operator` watches
   `AgenticRun` resources in its own namespace, runs the analysis through its
   own separately configured `llmProvider.*` (a third, unrelated static LLM
   credential), and writes `AnalysisResult` once analysis completes; RCA
   polls via the same MCP-scoped token from step 6 (short-lived -- the
   exchange happens once per request, not once per poll, so a very
   long-running analysis can outlive it).

## Tracing a live request

Each hop fails independently and mostly silently (generic `401`s are
deliberate), so trace hop-by-hop rather than end-to-end:

```bash
# 1. ericsson-agent's outbound call and its own SPIFFE fetch
oc logs -n ericsson-agent -l app.kubernetes.io/name=ericsson-agent -f

# 2. rca-agent's inbound validation, its own SPIFFE fetch, and the
#    token-exchange call
oc logs -n lightspeed-agentic-operator -l app.kubernetes.io/name=rca-agent -f

# 3. Keycloak's own record of every grant/exchange against a client --
#    enable this once: Admin Console -> Realm Settings -> Events -> Save Events
#    then Realm -> Sessions / Events, filtered by client (ericsson-agent,
#    rca-agent-mcp)

# 4. The passthrough hop -- MCP does no authz itself, so any 401/403 here
#    is really the API server rejecting the forwarded token
oc logs -n openshift-mcp-server -l app.kubernetes.io/name=openshift-mcp-server -f

# 5. What the API server currently trusts
oc get authentication.config.openshift.io cluster -o jsonpath='{.spec.oidcProviders[0].issuer.audiences}'

# 6. The operator side
oc get agenticrun -n lightspeed-agentic-operator
oc logs -n lightspeed-agentic-operator -l app.kubernetes.io/name=lightspeed-agentic-operator -f
```

## Troubleshooting (bugs actually hit building this)

- **`litellm.InternalServerError: ... Missing credentials`, or `Model
  openai/... not found`**: this is the *LLM inference* credential (see
  above), not the SPIFFE/Keycloak chain -- don't go looking in Keycloak
  Events for this one. `LiteLlm` forwards `**kwargs` straight to
  `litellm.completion()`, which does not read `LITELLM_API_BASE`/
  `LITELLM_API_KEY` on its own; those are this repo's own env var names, not
  something litellm auto-detects for the `openai/` model prefix (it only
  auto-reads `OPENAI_API_KEY`). Both `agent.py`s must pass them explicitly as
  `base_url=`/`api_key=` kwargs to `LiteLlm(...)` -- note the kwarg is
  `base_url`, not `api_base`.
- **RCA calls to MCP get 401 despite a successful token exchange**: check
  step 7 above -- `openshift-mcp` must be in the trusted audiences list, not
  just the two OIDC platform client IDs.
- **Console/CLI users authenticate but land in no RBAC groups**: the
  `openshift-console` client needs its own `groups` protocol mapper. It is
  easy to add the mapper only to the public login client (`rca-agent`) and
  forget the console has a separate client with its own, independent set of
  protocol mappers.
- **`/health/ready` stays `503` forever on either agent**: almost always the
  SPIFFE Workload API socket. The CSI driver always names the file
  `spire-agent.sock`, not `socket` -- check `identity.workloadApiSocket` /
  `identity.spiffe.workloadApiSocket` matches exactly what's mounted.
- **ericsson-agent logs `Failed to resolve remote A2A agent rca_agent: Agent
  card URL must use https, or http on a loopback host: http://rca-agent...`**:
  this is neither Keycloak nor SPIFFE -- google-adk's `RemoteA2aAgent` refuses
  to fetch an agent card (and, separately, refuses to trust the RPC url
  *inside* a card it did fetch) over plain http on a non-loopback host. The
  in-cluster Service DNS name is non-loopback plain http, so it no longer
  qualifies once a real caller in a different pod resolves it. Fix: put
  rca-agent behind its Route (`route.enabled`, edge TLS) and set
  `a2a.publicHost`/`a2a.publicPort`/`a2a.publicProtocol` (which control the
  RPC url the agent card itself advertises, in `rca_agent/main.py`) to that
  route's https origin, then point ericsson-agent's `a2a.downstreamEndpoint`
  at the same https origin instead of the in-cluster Service DNS name. The
  chart's `deployment.yaml` fails the template if `route.enabled` is true
  while `a2a.publicProtocol` is left at `http` to catch this early.
- **ericsson-agent logs a 401 from Keycloak's `/token` endpoint while fetching
  rca-agent's agent card** (`Agent card URL must use https...` is fixed, but
  the fetch itself then 401s): the agent card fetch is unauthenticated, but
  ericsson-agent's httpx client attaches its Keycloak bearer token to *every*
  outbound request via an event hook -- so this is actually the
  client_credentials + jwt-spiffe grant failing, not the card fetch. Reproduce
  directly against Keycloak's token endpoint from inside the pod (fetch a
  JWT-SVID via `spiffe.WorkloadApiClient`, POST it as `client_assertion`) to
  see the real Keycloak error body instead of a generic httpx exception. In
  order encountered, debugging this surfaced three separate, unrelated causes
  -- all now fixed in the charts, but worth knowing if this ever regresses:
  - `{"errorMessage":"Invalid trust domain name"}` when creating the `spiffe`
    identity provider via the Admin REST API with `config.trustDomain` set:
    on Keycloak 26.4.16 (RHBK), `SpiffeIdentityProviderConfig.getTrustDomain()`
    actually reads the generic `config.issuer` key, not `config.trustDomain`
    -- upstream `main` renamed this field, but this repo's Keycloak build
    predates that rename. `charts/all/keycloak-oidc` now emits `issuer`.
  - `{"error":"unauthorized_client","error_description":"Invalid client or
    Invalid client credentials"}`: the `spiffe` identity provider referenced
    by `jwt.credential.issuer` didn't exist in the realm at all -- it was
    never actually templated anywhere (the `keycloak` application's
    `spiffeIdentityProvider` override targeted the `rhbk` chart, which
    doesn't consume it for realm-level identity providers; `rca` realm's
    `KeycloakRealmImport` is entirely owned by `charts/all/keycloak-oidc`,
    which didn't declare one). Now templated in
    `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml`'s
    `identityProviders` list.
  - `{"error":"invalid_client","error_description":"client_id parameter does
    not match sub claim"}`: both agents' code sent a `client_id` form
    parameter alongside `client_assertion`. Keycloak's generic JWT client
    validator (`AbstractJWTClientValidator.validateClient`) rejects the
    request outright whenever `client_id` is present and differs from the
    assertion's `sub` -- and for a SPIFFE assertion `sub` is always a SPIFFE
    ID, never the Keycloak client_id, by design. Fixed by omitting `client_id`
    entirely in `ericsson_agent/auth.py`'s `_keycloak_token()` and
    `rca_agent/identity.py`'s `KeycloakTokenExchanger.exchange()` -- the
    client is resolved from the assertion's `sub` instead.
  - Also relevant once the above three are fixed: the SPIFFE JWT-SVID used as
    `client_assertion` must be requested with **the Keycloak realm issuer
    URL** as its audience (`SPIFFE_JWT_AUDIENCE` in both charts) --
    `FederatedJWTClientValidator.getExpectedAudiences()` defaults to
    `Urls.realmIssuer(...)` when no explicit audience list is configured.
    Requesting an audience like `ericsson-agent` (the workload's own name,
    what both charts defaulted to) fails this check.
- **RCA's token exchange succeeds but the resulting token's `aud` doesn't
  contain the requested audience** (or the exchange is rejected as
  unavailable): Keycloak's *standard* (V2) token exchange audience parameter
  only **filters** audiences the exchanging client's own protocol mappers
  already resolve -- it never adds one that wasn't already resolvable. Three
  things must all be true, and this repo's chart templates them together so
  they don't drift apart: (1) `rca-agent-mcp` needs the client attribute
  `standard.token.exchange.enabled: "true"` (V2 exchange is opt-in per
  client), (2) `rca-agent-mcp` needs its own protocol mapper adding
  `openshift-mcp` as an audience (`included.client.audience: openshift-mcp`),
  and (3) `openshift-mcp` must exist as an actual client in the realm -- the
  `audience` request parameter must name a real `client_id`, it cannot be an
  arbitrary string. Separately, (4) the *subject_token* being exchanged
  (ericsson-agent's token from step 3) must already carry `rca-agent-mcp` as
  an audience, or the exchange is rejected outright regardless of the above.
