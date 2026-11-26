# External-IdP egress gateways (Ask Red Hat via Praxis)

*Status: implemented in `charts/all/praxis-proxy` (`egressGateways`), enabled
for **stage** in the standalone variant. Every hop below was verified live on
2026-11-26 on bm-cluster against Red Hat SSO stage and Ask Red Hat stage, with
the pinned Praxis image (`ghcr.io/praxis-proxy/ai:0.4.1`, praxis-policy
0.3.1). The cutover itself (pointing the MAS node at the gateway) is a one-time
operator step, see [Rollout](#rollout).*

## Problem

ACME used to call MAS with a Red Hat SSO token whenever a request needed Ask
Red Hat, because Ask Red Hat only accepts SSO tokens. That same token then had
to be exchanged into the local realm (A -> H) for the hub leg, which needs
Keycloak's `token-exchange-external-internal:v2` and an explicit
`subject_issuer`. Neither works long term:

- No Praxis `delegator/oauth` config can send `subject_issuer`, the jwt-typed
  alternative is rejected by RHBK 26.4, and the `keycloak` claim-mapper never
  exposes `iss` to route predicates (`docs/LESSONS_LEARNED.md` sections 2 and 5).
- `subject: caller_workload` (RFC 7523) can't take Token A either. A
  federated-JWT client assertion must be addressed to *our* realm (its only
  `aud`), and an SSO token is addressed to `api.ask_red_hat`.
- `token-exchange-external-internal:v2` is removed in Keycloak 26.7, so any
  design that exchanges an outside token *into* the local realm stops working
  at that upgrade.

The key fact: ACME's SSO token was never a user's token. It's a
`client_credentials` token of the shared SSO client (`sub` = that client's
service account). So Ask Red Hat has always seen "the ACME client", never an
end user.

## Design

One internal identity in, a freshly minted external credential out:

```
ACME --SVID--> local Keycloak --Token A (local, aud mas-a2a)--> MAS
MAS --A--> praxis hub gateway   --H (internal exchange)-->        hub adapter      (unchanged)
MAS --A--> praxis-egress-ask-red-hat --mints SSO token (client_credentials)--> Ask Red Hat
```

The egress gateway is one Praxis instance per external destination
(`egressGateways.<key>`). It:

1. validates Token A against the local Keycloak **only** (external issuers are
   never trusted here) and enforces the `azp` allow-list, with the same
   `jwt-client` policy as the hub gateway;
2. runs `delegate(egress-delegator, ..., subject: this_workload)`: a
   `client_credentials` grant at the external IdP with the gateway's own
   client, which replaces the outbound `Authorization` header;
3. forwards to one fixed upstream, with `Host` rewritten to the upstream host;
4. serves the upstream's **agent card from its own configuration**, with the
   card's URL pointing back at itself.

The caller (MAS) therefore stores only one thing, `base_url` = the gateway,
and that value is the same for every environment. **Which upstream, which IdP,
which client and which card are all praxis-proxy configuration.**

What this buys:

- **Praxis does every exchange.** The hub leg stays an internal (Standard V2)
  exchange; the Ask Red Hat leg uses the IdP's ordinary client grant. Nothing
  depends on `subject_issuer` or the external-internal feature, so Keycloak
  can be upgraded past 26.6 and the feature disabled once no SSO caller
  remains.
- **MAS keeps its single-credential model.** It forwards one sealed
  credential to every node, as today. No multi-token support is needed in MAS
  or ACME.
- **Environments are a gitops switch.** Stage -> production swaps one
  values file; the blueprint is untouched.
- **Renewal is simpler.** There is only one chain (A -> H -> R). The SSO token
  is minted per call at the gateway, so it can't expire in the middle of a run.
- **More IdPs are cheap.** Each one is another map entry: its own instance,
  client, upstream and card. Agents don't change.

Trade-offs, accepted explicitly:

- The minted token speaks for the gateway's client, not the caller. That is
  identical to the caller-side `client_credentials` token it replaces. If Ask
  Red Hat ever needs the *end user's* Red Hat identity, a user-login SSO token
  has to come from the edge. It should then travel only on this leg (Praxis
  passthrough); the internal chain stays on Token A.
- The `azp` allow-list is the gate on who may use the shared client.
  Attribution of which user asked lives upstream of the gateway (ACME's
  `acme_audit` lines, MAS's task records); the external API only sees the
  account.
- The card is a **copy** of the upstream's (see
  [Keeping the card current](#keeping-the-card-current)).
- MAS no longer *receives* an SSO token. The goal (Ask Red Hat gets a valid
  SSO token) is kept; the old literal requirement is not.

## Three details that make it work

**Host header.** Praxis forwards the inbound `Host` (the Service name). Red
Hat's edge (Akamai) answers that with `400 Invalid URL "[no URL]"`. The
gateway sets `Host` to the upstream host with Core's `headers` filter.

**The card must come from the gateway.** The a2a-sdk sends JSON-RPC to the
**card's** interface URL, not to `base_url`, and Ask Red Hat's card advertises
its own host (`ifd-001-stage.apps.ext-waf...` on stage). Passing the upstream
card through would make MAS post Token A straight to Ask Red Hat (401), and
the pinned image can't rewrite it in flight: `json_body` rejects
`response_replace` because `Content-Length` is already committed. So the
gateway serves the card from `agentCard` with a `static_response` filter. The
chart sets `url` (and any `supportedInterfaces[].url`) to the gateway and drops
`additionalInterfaces`, so the copy can be pasted verbatim. MAS fetches it
whenever the node has no stored card, which is the case for nodes that
`setup-mas-blueprint.sh` saves.

**Card validation at save.** MAS's node validator fetches
`<base_url>/.well-known/agent-card.json` from mas-api without a token. The
gateway exempts exactly that GET from authentication; it is answered from
config and mints nothing.

## Transport

Internal ClusterIP Service, TLS on 8443 with an OpenShift service-serving
cert (the same scheme as the spoke gateways), and no Route. A NetworkPolicy
admits only `mas-worker` (runtime) and `mas-api` (the save-time validator)
pods from `mao`. MAS trusts the service CA through
`identity.serviceCaTrust.enabled` on both charts: the auto-injected
`openshift-service-ca.crt` ConfigMap is appended to the bundle the existing
`keycloakCaTrust` init container builds.

## Configuration layout (standalone variant)

The praxis-proxy app in `variants/standalone/values-standalone.yaml` loads two
values files through `extraValueFiles`:

| File | Content |
|---|---|
| `variants/standalone/praxis-egress-ask-red-hat-card.yaml` | `agentCard`: Ask Red Hat's card, shared by both environments (identical apart from `url`, which the chart replaces) |
| `variants/standalone/praxis-egress-ask-red-hat-stage.yaml` or `...-production.yaml` | `upstreamURL`, `tokenUrl`, `clientId`, `audience`, `scopes`, `clientSecret.vaultKey` |

The clustergroup chart renders these with `ignoreMissingValueFiles: true`, so
**a mistyped path silently renders no egress gateway**.
`tests/chart/test_praxis_egress_gateways.py` asserts that both paths exist.

## Stage -> production

No blueprint change: MAS's `base_url` is the gateway in both environments.

1. In `praxis-egress-ask-red-hat-production.yaml`, set `clientId` to the real
   production client. As of 2026-11-26 the production Vault record
   (`secret/data/global/acme-agent-redhat-sso-production`) still held a
   placeholder (`client-id: fake-client`). Load the real client secret there;
   it must be **alphanumeric**.
2. In the praxis-proxy app's `extraValueFiles`, replace
   `praxis-egress-ask-red-hat-stage.yaml` with
   `praxis-egress-ask-red-hat-production.yaml`. Commit and sync.
3. Check: `oc -n praxis-proxy get cm praxis-egress-ask-red-hat -o yaml | grep -A3 endpoints`
   shows `access.redhat.com:443`, then ask ACME a Red Hat question.

Verified 2026-11-26: the production upstream path
(`https://access.redhat.com/api/ask/v1/a2a/`) and that the production card
equals the stage card apart from `url`. Not verified: the production SSO
client (placeholder).

## Rollout

1. Sync: praxis-proxy renders `praxis-egress-ask-red-hat`; mas-worker and
   mas-api roll to pick up the service CA.
2. Point the MAS node at the gateway (one time, against the MAS API as an
   admin user):

   ```bash
   export ASK_REDHAT_EGRESS_URL=https://praxis-egress-ask-red-hat.praxis-proxy.svc:8443/api/ask/v1/a2a
   ./setup-mas-blueprint.sh --dry-run multi-cluster   # inspect first
   ./setup-mas-blueprint.sh multi-cluster
   ```

   This re-saves the blueprint, so pin the returned `blueprintId` as usual.
   The node then has `base_url` = the gateway and no stored card.
3. Make sure ACME sends the local Token A (`auth.defaultProvider: keycloak`,
   the chart default) so MAS forwards a local token.
4. Once no caller sends SSO tokens to MAS anymore: drop the SSO issuers from
   `mao-a2a identity.oidcIssuers` and the hub gateway's `identity.oidcIssuers`
   / `allowedCallers`, and disable `token-exchange-external-internal:v2`.

## Adding a new external agent (new IdP)

Step by step, including the blueprint wiring and what "one shared account"
means for authorization: [`ADDING_AN_EXTERNAL_AGENT.md`](ADDING_AN_EXTERNAL_AGENT.md).

## Keeping the card current

The card in `praxis-egress-ask-red-hat-card.yaml` is a copy. Re-copy it (minus
`url`) when Ask Red Hat changes skills, capabilities or protocol version. To
compare, from any pod that can reach the upstream:

```bash
curl -s https://access.stage.redhat.com/api/ask/v1/a2a/.well-known/agent-card.json \
  | jq -S 'del(.url)' > /tmp/upstream.json
yq -o json '.egressGateways."ask-red-hat".agentCard' \
  variants/standalone/praxis-egress-ask-red-hat-card.yaml | jq -S 'del(.url)' > /tmp/ours.json
diff /tmp/upstream.json /tmp/ours.json && echo "card is current"
```

A stale card mostly affects routing quality (the orchestrator's prompt cites
the card's skills) and streaming (`capabilities.streaming`). The call path
itself keeps working.

## Verification evidence (2026-11-26)

Run with MAS's own client code against the chart-rendered gateway:

| Check | Result |
|---|---|
| Red Hat SSO accepts the exact request the pinned delegator sends (`client_credentials`, `audience`, `scope`, HTTP Basic) | token identical to ACME's own (same `iss`/`aud`/`azp`/`sub`) |
| Ask Red Hat accepts that `client_credentials` token | `message/send` completed |
| Pinned delegator response handling | only `permissions` ⊆ granted `scope` is checked; no audience check on `this_workload` |
| Local Token A -> gateway -> Ask Red Hat, `message/send` and `message/stream` (SSE, incremental) | completed |
| Same Token A sent to Ask Red Hat directly | `401 Invalid credentials` (proves the bearer was replaced) |
| Node saved as `base_url` only: card served by Praxis (`application/json`, URL = gateway), SDK uses the gateway interface, send + stream | completed |
| MAS node validator on that node | `CONNECTION_OK` |
| No token / malformed token / path outside the upstream prefix | 401 / 401 / 404 |
| Pod outside the NetworkPolicy's allow-list | connection blocked |
| ExternalSecret from ACME's Vault record | `SecretSynced` |
| Rendered mas-worker init script, then TLS to the gateway with only its bundle | exit 0, TLS 1.3 verified |
| Production card path, and stage vs production card | exists; identical apart from `url` |

Not covered live: the `azp` deny on this gateway. No local client with a
service account and `aud: mas-a2a` was at hand. The allow-list is rendered by
the same template as the hub gateway's, which is verified live, but the
PostSync `enforcement-check` Job tests only the main gateway. Also not run:
the production SSO client (placeholder in Vault), steps 2-4 of the rollout,
and a full ACME -> MAS -> Ask Red Hat conversation through the gateway.
