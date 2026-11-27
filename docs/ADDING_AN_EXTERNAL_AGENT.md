# Adding an external agent to MAS

How to connect MAS to a new A2A agent that lives outside the cluster and is
protected by its own identity provider (IdP), such as Ask Red Hat behind Red
Hat SSO. The agent is reached through a praxis-proxy **egress gateway**; the
design and its live verification are in
[`EXTERNAL_IDP_EGRESS.md`](EXTERNAL_IDP_EGRESS.md).

## How it fits together

```
ACME --Token A (local Keycloak)--> MAS
MAS (a2a_agent_node, base_url = gateway) --Token A--> praxis-egress-<key>
praxis-egress-<key>:
  1. validates Token A (local Keycloak only) + azp allow-list
  2. serves the agent card from config, URL rewritten to itself
  3. mints a token at the agent's IdP with ITS OWN client (client_credentials)
  4. forwards to the real agent A2A endpoint, with that token
```

What you configure, and where:

| Piece | Where | What |
|---|---|---|
| Client ID + secret | Vault | one record per environment, properties `client-id` and `client-secret` (alphanumeric) |
| Agent card | `variants/standalone/praxis-egress-<key>-card.yaml` | a copy of the agent's A2A card; the chart rewrites its URL |
| Gateway | `variants/standalone/praxis-egress-<key>-<env>.yaml` | real A2A endpoint, IdP token URL, audience, scopes, the Vault record's path (no credentials) |
| Wiring | `variants/standalone/values-standalone.yaml` | both files under the praxis-proxy app's `extraValueFiles` |
| MAS | blueprint (MAS API) | an `a2a_agent_node` with `base_url` = the gateway, wired into the orchestrator |

Nothing changes in ACME, in the MAS charts (service CA trust is already on),
or in Keycloak.

## Identity: the agent sees one account, not your users

**Every call the gateway makes is authenticated as the gateway's IdP client:
one shared account (that client's service account), for every MAS user and
every request.** The `client_credentials` grant has no user in it. The agent
can't tell users apart, and nothing user-specific is sent to it.

What that means in practice:

- **Authorization is per account.** Whatever the client is allowed to see or
  do at the agent, every caller that passes the gateway's allow-list can use.
  Treat the client's grants as granted to all of them. Users can't reach
  their *own* data or entitlements at that agent, only the account's.
- **Who may use the account is decided before the agent.** The gateway's
  `azp` allow-list (default: the hub gateway's `policy.allowedCallers`,
  i.e. `acme-agent`) and whoever may talk to ACME and MAS. On the pinned image
  only `claim.azp` predicates are proven to work
  (`LESSONS_LEARNED.md` section 5).
- **Shared quotas and audit.** The agent's rate limits, quotas and audit log
  all see the one account. Attribution of *which* user asked lives upstream
  of the gateway (ACME's `acme_audit` lines, MAS's task records), not at the
  agent. Today even Token A is a `client_credentials` token of `acme-agent`,
  so there is no end-user identity anywhere in this chain unless ACME is
  given a user's token.
- **Revocation is all or nothing.** Rotating or revoking the client secret
  cuts every caller at once.

This is the right model for agents that answer from shared, non-personal
knowledge (documentation, product data, public CVE information); it is what
Ask Red Hat already saw when ACME minted the token itself. It is the **wrong**
model when the agent must enforce the end user's own permissions (customer
data, per-account entitlements). That needs a user-login token from that IdP
at the edge, carried only on that leg (Praxis passthrough). This is not
implemented.

## Prerequisites

- The agent speaks A2A over JSON-RPC and publishes a card at
  `<endpoint>/.well-known/agent-card.json`.
- Its IdP issues tokens with the **`client_credentials`** grant, accepts
  HTTP Basic client authentication, and takes an `audience` parameter (the
  pinned `delegate()` always sends one).
- You have a client for that IdP whose secret is **alphanumeric**. The
  delegator sends HTTP Basic without URL-encoding, so `%`, `+`, `/`, `:` and
  similar break it.
- The agent and the IdP are reachable from the cluster over HTTPS (port 443,
  or the endpoint's own port), and their certificates chain to a public root
  (the gateways verify with `policy.caBundleConfigMap`, which carries public
  roots).

## Steps

The example uses an agent "Foo" at `https://foo.example.com/a2a` and the
gateway key `foo`.

### 1. Prove the IdP and the agent by hand

From a pod in the cluster (some APIs only accept allow-listed source IPs, for
example Ask Red Hat stage), mint a token exactly as the gateway will:

```bash
curl -s -u "$CLIENT_ID:$CLIENT_SECRET" "$TOKEN_URL" \
  -d grant_type=client_credentials -d audience="$AUDIENCE" \
  --data-urlencode "scope=$SCOPES"
```

- The response's `scope` must contain **every** scope you will list. The
  pinned delegator denies with `delegation.scope_too_broad` otherwise.
- Read the card and note its `url`: that is the endpoint that answers
  JSON-RPC. It may differ from the base URL (Ask Red Hat's stage card names a
  different host, and its endpoint only answers with a trailing slash).
- Send a `message/send` to that URL with the token and check you get an
  answer.

### 2. Store the client credentials in Vault

Create one Vault record per environment, for example
`secret/data/global/foo-agent-stage`, with two properties:

- `client-id`: must match `[A-Za-z0-9._-]` (the gateway's init container
  refuses anything else);
- `client-secret`: must be **alphanumeric**.

Document the record in `values-secret.yaml.template`. The gateway reads both
through one ExternalSecret: the secret as an env var, and the client ID
written into its config by an init container at pod start (the delegator's
`client_id` is a plain config string). Nothing credential-related goes into
git. Both are read at pod start, so after rotating them in Vault, restart the
gateway once the ExternalSecret has synced
(`oc -n praxis-proxy rollout restart deploy/praxis-egress-foo`).

### 3. Copy the agent card

Save the agent's card as YAML in
`variants/standalone/praxis-egress-foo-card.yaml`:

```yaml
egressGateways:
  foo:
    agentCard:
      name: Foo Agent
      description: ...
      protocolVersion: 0.3.0
      preferredTransport: JSONRPC
      capabilities: {streaming: true}
      skills: [...]
      # url: leave it or drop it -- the chart always replaces it
```

The gateway serves this card at `<path>/.well-known/agent-card.json`. The
chart sets `url` (and any `supportedInterfaces[].url`) to the gateway's own
address and drops `additionalInterfaces`. That redirection is what keeps MAS
pointed at the gateway: the a2a-sdk sends to the card's URL, never to
`base_url`. The card is a copy, so refresh it when the agent changes skills or
capabilities (see "Keeping the card current" in `EXTERNAL_IDP_EGRESS.md`).

### 4. Configure the gateway: real endpoint, IdP and client

Create `variants/standalone/praxis-egress-foo-stage.yaml` (one file per
environment):

```yaml
egressGateways:
  foo:                                         # DNS label, at most 40 chars
    # The agent's real A2A endpoint, including its path. The gateway serves
    # that same path and forwards it here, with Host set to this host.
    upstreamURL: https://foo.example.com/a2a
    tokenUrl: https://idp.example.com/oauth2/token
    audience: <audience the agent expects>
    scopes: [<scope>, ...]                      # each must be granted
    clientSecret:
      # The shared account: client-id AND client-secret both come from this
      # record (rendered as an ExternalSecret).
      vaultKey: secret/data/global/foo-agent-stage
      # vaultProperty: client-secret           # default
      # vaultClientIdProperty: client-id       # default
    # clientId: <literal>                      # only to override the Vault value
    # Optional:
    # allowedCallers: [acme-agent]             # default: policy.allowedCallers
    # inboundAudience: mas-a2a                 # default: identity.keycloak.audience
    # replicas: 1
```

The gateway is reachable at
`https://praxis-egress-foo.praxis-proxy.svc:8443/a2a` (the upstream's path on
the gateway's host). Only that path prefix is routed; anything else is a 404.

### 5. Wire the files into the variant

In `variants/standalone/values-standalone.yaml`, add both files to the
praxis-proxy app:

```yaml
    praxis-proxy:
      ...
      extraValueFiles:
        - /variants/standalone/praxis-egress-ask-red-hat-card.yaml
        - /variants/standalone/praxis-egress-ask-red-hat-stage.yaml
        - /variants/standalone/praxis-egress-foo-card.yaml
        - /variants/standalone/praxis-egress-foo-stage.yaml
```

The clustergroup chart ignores missing value files, so **a typo silently
renders no gateway**. Extend `test_variant_selects_card_and_one_environment`
in `tests/chart/test_praxis_egress_gateways.py` to check the new paths, and
run `make run-ci-tests`.

### 6. Sync and smoke-test the gateway

After the sync, from a pod the gateway's NetworkPolicy admits (`mas-a2a`,
`mas-worker` or `mas-api` in `mao`, with a bundle that includes the service
CA):

1. `GET https://praxis-egress-foo.praxis-proxy.svc:8443/a2a/.well-known/agent-card.json`
   returns your card with `url` = the gateway.
2. A `message/send` to the card's URL with a local Token A
   (`oc -n acme-agent exec deploy/acme-agent -- python -m acme_agent.mint_token --provider keycloak`)
   is answered.
3. The same Token A sent **directly** to the agent is refused. That proves the
   gateway replaced it.
4. `bash scripts/monitor.sh --egress` shows an `EGRESS MINT + FORWARD` card
   for `foo` per call, and an `EGRESS REJECT` card for each refused one.

### 7. Add the agent to the MAS blueprint

MAS forwards its execution-context credential (Token A) to every
`a2a_agent_node` whose `bearer_token` is empty, so the node needs no
credentials of its own. Use `setup-mas-blueprint.sh` as the template; the Ask
Red Hat node is the one to copy.

1. **Register the node** in the MAS inventory (`resource.save`; the script's
   `mas_api_upsert_resource` updates an existing one):

   ```json
   {
     "category": "nodes",
     "type": "a2a_agent_node",
     "name": "Foo",
     "config": {
       "type": "a2a_agent_node",
       "base_url": "https://praxis-egress-foo.praxis-proxy.svc:8443/a2a",
       "bearer_token": "",
       "verify_ssl": true
     }
   }
   ```

   - `base_url` is the **gateway**, never the agent.
   - `bearer_token` stays empty. MAS would send a static token *instead of*
     the forwarded Token A: a credential stored in the blueprint, which
     expires and isn't the caller's.
   - Do **not** set `agent_card`; the gateway serves it.
   - mas-api validates the node on save by fetching the card without a token.
     The gateway answers that from config.
2. **Add it to the blueprint YAML** with the returned `rid`, in four places:
   - `nodes:`: the node, with the **same** inline `config` as above (the
     blueprint's inline config and the inventory resource must agree);
   - `plan:`: a step, e.g. `- uid: foo` with `node: <rid>`;
   - the orchestrator step: add `foo` to its `after:` list (so it loops back
     to the orchestrator) and `foo: foo` to its `branches:`;
   - the orchestrator `system_message`: add Foo under AVAILABLE AGENTS and a
     ROUTING rule. The orchestrator only delegates to agents it is told
     about.
3. **Save and pin.** `blueprint.save` returns a `blueprint_id`; set it as
   `mao-a2a`'s `a2a.blueprintId` in `values-standalone.yaml` and sync.
4. **Test end to end:** ask ACME a question that should route to Foo.

### 8. Write it down

Add the agent to `EXTERNAL_IDP_EGRESS.md`, and record anything you had to
discover live in `LESSONS_LEARNED.md`.

## Switching environments

The blueprint never changes: `base_url` is the gateway in every environment.
To move Foo from stage to production, write
`praxis-egress-foo-production.yaml` (endpoint, token URL, the production
Vault record) and swap it for the stage file in `extraValueFiles`. If the production card
differs, give it its own card file too.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No `praxis-egress-<key>` objects after the sync | wrong `extraValueFiles` path (silently ignored), or a render error in the praxis-proxy app |
| Pod stuck in `CreateContainerConfigError` | the client secret's Secret doesn't exist: ExternalSecret not synced (Vault key or property wrong) |
| `render-config` init container fails | the Vault record's `client-id` is empty or contains characters outside `[A-Za-z0-9._-]` |
| Old credentials still used after a Vault change | they are read at pod start: `oc rollout restart` the gateway |
| MAS: TLS / certificate error to the gateway | mas-a2a, mas-worker or mas-api without `identity.serviceCaTrust.enabled` |
| mao-a2a: `Network communication error fetching agent card` from the gateway URL | the gateway's NetworkPolicy does not admit the calling pod (mas-a2a fetches the card itself when it builds the plan) |
| MAS: connection timeout to the gateway | caller not admitted by the gateway's NetworkPolicy (`egressGatewayConfig.networkPolicy`) |
| `401 auth.malformed_header` / `auth.token_expired` | no or expired Token A reached the gateway |
| `403 ... not an allow-listed caller` | Token A's `azp` isn't in the gateway's allow-list |
| `403 delegation.idp_rejected: token exchange rejected: invalid_client` | wrong client ID or secret, or the secret isn't alphanumeric |
| `403 delegation.scope_too_broad` | the IdP granted fewer scopes than `scopes` lists |
| `400 Invalid URL` from the agent's edge | `Host` not rewritten (the chart does this; check `upstreamURL`'s host) |
| The agent answers 401 | it wants a different `audience` or `scopes`, or the token is valid but the account lacks access |
| MAS calls the agent directly (gateway bypassed) | the node has a stored `agent_card`, or the gateway has no `agentCard` and passes the upstream's card through |
| 404 from the gateway | path outside the `upstreamURL` prefix |
| `EGRESS REJECT 403` in the monitor, reason unknown | the gateway doesn't log it: read the 403 body MAS received (`delegation.idp_rejected`, `delegation.scope_too_broad`, or `not an allow-listed caller`) |
