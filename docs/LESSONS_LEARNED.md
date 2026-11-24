# Lessons learned

Every finding that took a live cluster (or, where labelled, a local render) to
discover, consolidated in one place. Each entry says what was hit, why it was
missed, and what the repo now does about it. Reference material -- routes,
values, contract tables, verification commands -- stays in the owning README;
code comments keep the one-line invariant.

How to use it: when debugging, search this file first for the symptom. When you
find a new bug by running something, add an entry here (newest last within a
section) using the same "what was hit / why nothing caught it" shape.

Labels: **live** = reproduced on a deployed cluster; **pinned image** =
reproduced against the exact pinned binary/image, locally; **render** =
caught by rendering/validation, not at runtime.

---

## 1. Rendering, linting and `values-standalone.yaml`

- **Apply the per-application `overrides` when you render.** Loading
  `values-standalone.yaml` as plain Helm values does not enable its Application
  settings, so a chart-only render with synthetic values can miss a broken
  standalone file. Always render the actual variant after editing it.
- **A stray leading space in a list item breaks the whole file.** An extra
  space on an `olsClusters` list item made the standalone YAML unparseable
  while every chart-only test passed. `make show` / `make validate-schema` are
  the cheap checks. *(render)*
- **Quote any value that could be all digits.** `value: 34256970` (a git short
  SHA) parses as a number; through Helm/Argo it came out as `3.425697e+07`,
  which failed image parsing (`InvalidImageName`) and hung the ArgoCD sync on
  the hook Job using that image with no obviously related Application error.
  Always quote `image.tag`-style overrides. *(live)*
- **Violating NetworkPolicy port semantics.** A pod-port that differs from the
  Service port (dns-default: Service 53, pod 5353) is matched *after* DNAT by
  OVN-Kubernetes; allowing only 53 silently blackholed every DNS lookup. Allow
  both 53 and 5353. *(live, spoke gateway)*
- **Praxis multi-OIDC output is YAML nested inside a ConfigMap string.** Helm
  whitespace trimming once joined the last external JWKS URL to the local
  issuer item, producing invalid embedded YAML while the outer manifest parsed.
  Parse `data.policy.yaml` in tests (`tests/chart/test_praxis_multi_oidc.py`),
  not only the outer Helm output. *(render)*
- **Validate real rendering, not just syntax.** A Mermaid plan page passed HTML
  nesting checks but rendered three error diagrams in Chrome because labels
  contained statement-separating semicolons; check the SVG with the pinned
  Mermaid runtime. *(render/browser)*
- **Praxis routing-era header evidence.** With `global.olsClusters` routing the
  pinned binary forwarded the public discovery/readiness paths to the primary
  backend, routed a valid token by `X-OLS-Cluster`, answered 404 for a missing
  or unregistered id, rejected a missing token with 401 before routing, and
  rejected a non-allow-listed `azp` with 403 -- but routed a request carrying
  one valid and one bogus duplicate header (see the multi-valued-header entry
  in §5). *(pinned image, 2026-10-01)*

## 2. Keycloak: realm import and clients

- **`KeycloakRealmImport` gets one shot.** On a fresh install it can race the
  `Keycloak` CR (`keycloaks.k8s.keycloak.org "keycloak" not found`) and then
  sits in `HasErrors` forever -- it does not retry even once the CR is Ready.
  Delete it and let ArgoCD recreate it (never patch its status, never re-import
  a live realm to fix a client). Now automated by the `realm-import-healer`
  CronJob (`realmImport.selfHeal.enabled`). *(live)*
- **Realm-import placeholders are `${NAME}`, not `$(NAME)`.** Older chart
  versions used `$(...)` (the syntax people expected from the docs), and
  Keycloak stored the literal string as the real credential while still
  reporting `Done: True, HasErrors: False`. With shell-style syntax *plus*
  the one-shot import, a startup race against the referenced Secret produces
  the same literal value. The PostSync `realm-secrets-reconciler` re-asserts
  the console secret and the bootstrap admin password on every sync; since
  2026-11-24 it also repairs **exchanger** client secrets.
  *(live 2026-11-24)*
- **Exchanger secrets stored as literal placeholders block the whole chain.**
  All three exchanger clients (`mas-a2a`, `lightspeed-hub-a2a`,
  `praxis-spoke-gateway`) had `$(...)`-style literals registered as their
  secrets. The Kubernetes Secrets and the Praxis environment matched each
  other, so nothing looked wrong until Praxis answered `POST /a2a` with 403
  `delegation.idp_rejected: token exchange rejected: unauthorized_client`,
  and Keycloak logged `TOKEN_EXCHANGE_ERROR ... invalid_client_credentials`.
  Restarting Praxis or Keycloak cannot fix a literal value stored in the
  realm. Fix: align each registered client secret with the existing
  Vault-backed Secret via the Admin API (`PUT` a `{"secret": ...}` body, then
  read it back), without rotating secrets or re-importing the realm. The
  earlier "verified live" 2026-11-20 run had configured those clients by hand
  through the Admin API, so it never proved a fresh import set them
  correctly -- chart tests had checked rendering, not the imported realm.
  *(live 2026-11-24)*
- **Console login fails with `invalid_redirect_uri` after enabling OIDC.** The
  realm was imported while `openshiftOIDC.enabled=false`, so `redirectUris`
  never rendered; updating the one-shot import CR later does not update the
  existing client. Check the *live* client, not the import manifest; the
  reconciler now re-asserts `redirectUris` on every sync. Never delete the
  realm to repair a client. *(live)*
- **Keycloak 26 standard token exchange needs the `audience` to be an existing
  client.** Exchanging to `audience=lightspeed-hub-a2a` (a mapper-only string)
  returned `400 invalid_client "Audience not found"`, which reads like a bad
  client secret -- the secret was fine. Exchange targets are now rendered as
  audience-only clients (all flows off). Tests only compared mapper strings;
  the adapter's tests use a fake token endpoint, so neither saw it. *(live)*
- **The exchanging client must be in the subject token's `aud`.** The
  `audience` request parameter only narrows. That is why every token's
  audience names the client that exchanges it next (A -> `mas-a2a`, H ->
  `lightspeed-hub-a2a`, R -> `praxis-spoke-gateway`).
- **Exchanged tokens inherit the exchanging client's lifespan**, even past the
  subject token's expiry. This is the fix for runs dying mid-step: mint the
  run token R by exchange (1800 s) instead of storing the caller's 5-minute
  token A. *(live)*
- **`fullScopeAllowed: true` leaks kube API audiences into Token A.** Keycloak's
  "audience resolve" adds the audience of every client whose roles the token
  holds, so A was accepted by the kube APIs. `acme-agent` now has
  `fullScopeAllowed: false`; A gets 401 at every kube API. *(live)*
- **An exchanged token's `azp` is the exchanger, not the caller.** After the
  hub-gateway cutover every sandbox kube call got 403 "not an allow-listed
  caller" at the spoke gateway, which still inherited the hub gateway's
  `allowedCallers` (`acme-agent`) while R carried `azp=lightspeed-hub-a2a`.
  Fixed with `spokeGatewayConfig.allowedCallers`. The Keycloak replay test had
  checked aud and groups, never the gateway's azp gate. *(live)*
- **Kubernetes RBAC binds groups, not audiences.** With a plain group mapper, H
  (hub API) and U (spoke API) carried the same `groups`, so on a hub that is
  its own spoke, U could create AgenticRuns. Exchangers now project only their
  audience's client roles into `groups` (`keycloak.exchangeRoles`), and RBAC
  binds those role groups; verified with `SelfSubjectReview`. *(live)*
- **Red Hat SSO `client_credentials` requires service accounts enabled on the
  client.** Surfaced only as an ADK agent-card 401; the real failure was
  `unauthorized_client: Client not enabled to retrieve service account` at the
  SSO token endpoint. Mocked token tests and Helm secret checks cannot see an
  external IdP's client policy; ACME now surfaces the OAuth
  `error_description`. *(live)*
- **External-token exchange needs `openid` in the scope (Keycloak 26.4).**
  Validation calls both introspection and userinfo; with only `api.ask_red_hat`
  the Red Hat userinfo endpoint returns 403 and the caller sees only
  `Keycloak token exchange failed`. Request `openid api.ask_red_hat`. *(live)*
- **External IdP introspection needs its own client secret.** Valid SPIFFE
  client auth still gave `Introspection endpoint call failure ... 401` because
  the live IdP had no `config.clientSecret`; the chart only rendered the client
  ID. The chart now reuses ACME's Vault record and reconciles complete IdP
  endpoints and credentials. *(live)*
- **A successful external-token login can still fail RBAC as
  `User "keycloak:<uuid>"`.** Without a `groups` claim OpenShift authorizes the
  brokered subject as an individual, while RBAC targets
  `Group:keycloak:acme-agent-rca`. Each external provider needs a Hardcoded
  Group mapper (`hardcodedGroups`), and an already-issued token cannot gain the
  claim retroactively. *(live)*
- **Keycloak 26.4.16's SPIFFE IdP reads `config.issuer`, not
  `config.trustDomain`.** Creating it with `trustDomain` returns
  `Invalid trust domain name`; upstream renamed the field, this build predates
  that. *(live)*
- **Federated-JWT client auth: omit `client_id`.** Keycloak's generic JWT
  validator rejects a request whose `client_id` form parameter differs from the
  assertion's `sub`, and a SPIFFE `sub` is always a SPIFFE ID. The client is
  resolved from the assertion's `sub`. The JWT-SVID itself must be requested
  with the *realm issuer URL* as audience (`SPIFFE_JWT_AUDIENCE`), not the
  workload name. *(live)*
- **Never `vault kv put` a single field into a multi-field secret.** KV v2
  `put` replaces the whole secret; a one-field `put` dropped Keycloak's
  `db-password` and `console-client-secret`, `make load-secrets` regenerated
  them, and Keycloak (which applies the bootstrap admin password once and keeps
  one-shot import secrets) kept the old values -- admin login broke with
  `invalid_grant` and console OIDC broke. Use `vault kv patch`; rotating a
  Keycloak credential means changing it in Keycloak too. *(live)*
- **Praxis's oauth delegator sends Basic auth without URL-encoding** (pinned
  ai:0.4.1). A generated secret containing `%` made Keycloak fail client auth
  (`unauthorized_client`, `clientId="null"`). Every secret Praxis presents uses
  `alphaNumericPolicy` in `values-secret.yaml.template`; the upstream bug is
  still open. *(live)*

## 3. OpenShift Native OIDC and RBAC

- **Flipping `type: OIDC` is cluster-wide and not fail-safe.** It replaces the
  internal OAuth server for console and `oc login` at once; a mistake locks out
  everyone. Always have a working break-glass kubeconfig (`make
  admin-break-glass-kubeconfig`) first. Revert with
  `spec.type: ""` + `oidcProviders: null`.
- **Console users need their own `groups` mapper.** The console client is
  separate from the CLI client with its own protocol mappers; adding the mapper
  only to `openshift-cli` lets users authenticate but land in no RBAC groups.
  Keep the `keycloak:` prefix -- an empty prefix could collide with a reserved
  `system:` group such as `system:masters`.
- **Check an operator's `installModes` before setting `targetNamespaces`.**
  `targetNamespaces: []` means AllNamespaces, which some operators reject.
  `lightspeed-operator` supports `OwnNamespace` only; an AllNamespaces
  OperatorGroup made OLM fail permanently with `UnsupportedOperatorGroup`.
  *(live)*

## 4. Identity chain and token propagation

- **Red Hat SSO's principal is `sub`, not the client UUID.** MAS A2A
  authorizes the validated token's `sub`; populating
  `a2a.allowedPrincipals` with the OAuth client UUID fails closed with 403
  *after* signature and audience validation succeed. Decode a real test token.
  *(live)*
- **A synced Secret is not a refreshed process environment.** MAS A2A read its
  principal allow-list from a `secretKeyRef` env var at startup. After the
  Secret (and the chart values) moved to a new `sub`, the old pod kept the old
  list and answered 403 `Principal is not allowed to invoke this agent`
  before any delegation -- while ArgoCD was `Synced`/`Healthy`, because the
  Secret had converged and readiness never exercises caller authorization.
  A `rollout restart` of MAS A2A alone fixed it. The durable fix is a
  pod-template checksum over chart-managed allow-list values; an externally
  managed Secret still needs an explicit restart. Verify the process's
  effective list, not just the Secret. *(live 2026-11-24)*
- **`request_id` does not join across the orchestrated flow.** ACME's
  `X-Request-Id` never reappears downstream of MAS; each stage mints its own
  id. With MAS in the path, correlate by wall-clock time window or Keycloak
  realm events, not by id. *(live)*

## 5. Praxis gateway (pinned image quirks)

The pinned image is alpha; every quirk below was reproduced against it, so
re-check each when the image is bumped.

- **The allow-list used to fail open.** Every fail-open was silent: a
  validly-signed token from a non-allow-listed caller reached the backend.
  Two independent causes: `role: client` populates `client.*`, which is never
  visible to route predicates on this build, while `role: user` populates
  `subject.*`/`claim.*`, which are; and a bare predicate with no action is a
  no-op, while `require(...)` denies unconditionally. Only `role: user` plus
  an explicit `deny(...)` works. The `enforcement-check` PostSync Job guards
  against a silent regression.
- **Never serve a hand-maintained agent card.** The static card advertised A2A
  `protocolVersion: "1.0"`, so MAS (a2a-sdk 1.1.5) called
  `SendStreamingMessage` against the 0.3-only adapter (a2a-go v0.3.15
  `message/stream`/`message/send`) and every delegation failed with
  `method not found`; `streaming: false` just switched to `SendMessage`, which
  fails the same way. Nothing caught it: tests checked only the card's shape,
  and MAS logged it as a work-item note, not an error. Discovery now passes
  through the adapter's own card (whose `url` must be the public Praxis
  endpoint). *(live)*
- **`static_files` is not a cluster field.** Modelling card files as a
  `load_balancer.clusters[]` `static_files` entry crash-looped the pod: the
  embedded Core 0.7.0 rejects it as an unknown field.
- **A `router` filter with `routes: []` is fatal.** The "clean 404" guess
  refused to start:
  `fatal: router: 'routes' is empty; every request would fail with 404`.
  With no upstream, render no router filter at all and let Core's own 404
  answer. *(pinned image)*
- **The presence of `delegator/oauth` is only a `policy.yaml` plugin**, not a
  Core filter; both the jwt resolver and the delegator need `perform_http`, and
  `delegate()` without identity resolution fails. The TLS listener needs
  `tls.certificates[]`, not `tls.cert_path`/`key_path`. Header stripping is a
  Core `headers` filter (`request_remove`), not `strip_headers`.
- **Multi-valued target headers cannot be rejected at the router.** Router and
  guardrails both OR-match repeated values (`get_all(...).any/find_map`), and
  the `headers` filter has no reject form. A request smuggling one registered
  and one unregistered target still matches. The adapter must reject
  duplicates; admission policy and run-creator RBAC bound what gets through.
- **A hardcoded health path is a guess until tested against the backend.** The
  `/health/ready` route copied from a predecessor chart 404'd against the real
  service (actual route `GET /readiness`), which only surfaced once a real
  backend existed. Check path/port/label against source or `oc get` before
  trusting a comment. *(live)*
- **Same-cluster upstreams must use the internal Service, not the Route.**
  Pointing a same-cluster `olsClusters` entry at the public Route failed
  readiness with `UnknownIssuer` (ingress CA vs service-ca). Remote Routes need
  their own trusted ingress CA bundle; never disable verification.
- **Only the main Deployment got the IPv4-only DNS sidecar.** Per-spoke
  gateways (`praxis-spoke-<id>`) fell back to ClusterFirst DNS, could not
  resolve the Keycloak Route (`resolve ... deadline exceeded`), failed the JWKS
  fetch, and answered every sandbox request with 401 while the agent retried.
  Any new Praxis Deployment needs the same sidecar. *(live)*
- **Re-check the audience whenever a gateway's backend changes.** After the
  hub-adapter cutover the main gateway still validated local-Keycloak tokens
  for `mas-a2a`, left over from when ACME called Praxis directly. *(live)*
- **A large response can exceed the gateway body limit.** A 12.4 MB
  `kubectl logs` response failed with 500 "streamed response body exceeds
  global body limit" (default 10 MiB); raise
  `spokeGatewayConfig.maxResponseBytes`.
- **Spoke gateway NetworkPolicy egress must include DNS 53 *and* 5353** and the
  spoke's real remote API port (typically 6443, not 443). Plain
  NetworkPolicies cannot match FQDNs; FQDN-aware enforcement needs CNI egress
  features.
- **A source ConfigMap read by a Sync hook must have an earlier sync-wave.**
  The spoke-gateway source ConfigMap (which the `spoke-ca-bootstrap` hook reads)
  rendered at the default wave 0, i.e. *after* the wave -1 hook, so a
  greenfield install 404'd in the hook and failed the entire sync with every
  wave-0+ resource Missing. It now renders at `argocd.argoproj.io/sync-wave:
  "-2"`, same as the sync script ConfigMap. The service-ca operator fills
  `service-ca.crt` asynchronously, which the Job's pod backoff covers. The old
  cluster never hit it because its ConfigMap pre-dated the bridge.
  *(live 2026-11-23)*
- **The service-serving CA rotates.** A ConfigMap->Secret copy of it will go
  stale and sandbox kubeconfigs will reject the gateway cert after rotation;
  that is why the spoke CA sync is a CronJob, not a one-shot Job.

## 6. TLS trust

- **Extend the default trust store; never replace it.**
  `ssl.create_default_context(cafile=X)` and httpx's `verify=<path>` replace
  the system roots with only `X`. Fine when the only peer is the in-cluster API
  server (`sync_keycloak_ca.py`, `sync_ingress_ca.py`), but against a
  publicly-CA-signed Route it fails with `CERTIFICATE_VERIFY_FAILED: unable to
  get issuer certificate` because the synced ingress bundle holds only
  intermediates. Fix: `ctx = ssl.create_default_context();
  ctx.load_verify_locations(cafile=bundle)` -- already applied in
  `agents/acme_agent/src/acme_agent/auth.py`,
  `realm-secrets-reconciler-job.yaml` and the removed OLS fork. *(live)*
- **Each HTTPS hop has its own trust store -- the adapter's inbound serving cert
  proves nothing about its outbound Keycloak trust.** The hub adapter mounted
  only its serving cert and the ServiceAccount projection. Its H->R exchange
  against the public Keycloak Route failed inside the container with
  `x509: certificate signed by unknown authority` (`curl` showed
  `self-signed certificate in certificate chain`); the adapter's transport
  error surfaced only as a generic `request_failed`. Because the adapter
  creates the AgenticRun *before* exchanging and writing
  `ls-run-token-<run UID>`, the operator paused each new run on
  `CredentialsExpired=True` / `Secret not found` -- evidence of the failed
  handoff, not of a working exchange. Fix: `adapter.keycloak.caTrust` combines
  the adapter image's public roots with the namespace-local issuer CA in an
  init container and points `SSL_CERT_FILE` at the result; never a
  private-CA-only bundle, never disabled verification. Supplying the same CA
  to a manual probe made discovery and the full A->H->R->U replay succeed.
  *(live 2026-11-24)*
- **MAS outbound agent-card fetches need ingress-CA trust too**, independent of
  Keycloak identity mode. API/worker pods in `providerMode=dev` did not mount
  it and failed with `self-signed certificate in certificate chain`; a local
  setup script's CA setting does not configure trust inside pods.
- **Praxis policy JWKS fetch needs public roots on IPv4-only clusters.**
  `policy.caBundleSync.includeSystemRoots=true`, plus
  `policy.allowPrivateIdp` only for a static private issuer. *(live)*
- **Praxis's delegator Basic auth is not URL-encoded** -- see section 2.

## 7. MAS (orchestrator, inbound A2A, worker)

- **An LLM must never spell a transport header.** The planning LLM emitted
  `X_OLS_Cluster` instead of `X-OLS-Cluster` in 3 of 5 delegations; Praxis has
  no default route, so each miss became a silent `no route matched` 404 and the
  delegation was lost intermittently. The cluster id value was always correct if
  Praxis logged the 404 as a routine decision. Temperature 0 plus prompt
  spelling rules only reduced it (12 of 41 POSTs still 404'd in 72h); the
  durable fix is deterministic header injection in the MAS image. Related trap:
  MAS `resource.save` rejects duplicates with 400 "exists for user", so
  re-running `setup-mas-blueprint.sh` silently kept a stale config until it
  switched to `resource.update`. *(live)*
- **A stream cap is not a failure.** "The stream time limit was reached" comes
  from MAS's inbound A2A server (`A2A_STREAM_MAX_DURATION_SECONDS`, default
  300 s), not the backend. ACME's ADK client does not poll afterwards, so long
  investigations look failed while the AgenticRun keeps running; size it to the
  agent's analysis timeout (1200 s in the variant). *(live)*
- **`AgentTool` buffers; `sub_agents=` streams.** `AgentTool.run_async` collapses
  a sub-agent to one return value, so `message/stream` emitted nothing until the
  whole investigation finished. Do not set `mode="task"`; the downstream A2A
  endpoint has no `finish_task` handshake.
- **Uvicorn's default log config only configures `uvicorn.*`.** Every
  `acme_audit` INFO record was dropped by Python's last-resort handler -- a full
  token -> 401 -> renewal flow left zero audit lines. The app now installs its
  own handler, and audit lines carry token fingerprints (`fp:…`) plus
  `source=pasted_override|minted`. Related: MAS's `Failed to resolve signing
  key` ERROR is benign multi-issuer probe noise; the real rejection is
  `Token validation failed for all … issuers` + 401. *(live)*
- **`LiteLlm` forwards `**kwargs` to `litellm.completion()`.** It does not read
  `LITELLM_API_BASE`/`LITELLM_API_KEY` (repo-invented names) for the `openai/`
  prefix. Pass `base_url=`/`api_key=` explicitly -- note `base_url`, not
  `api_base`; a missing credential shows up as `Missing credentials`.
- **ADK refuses non-loopback plain HTTP for agent cards** ("Agent card URL must
  use https, or http on a loopback host"). An in-cluster Service DNS name no
  longer qualifies from another pod; route through the TLS Route.
- **A 401 on the agent-card fetch is often the Keycloak grant.** acme-agent's
  httpx hook attaches its bearer token to every outbound request, so a card
  fetch 401 can be the `client_credentials` + JWT-SVID grant; reproduce against
  Keycloak's token endpoint from inside the pod to see the real error body.
- **A run with no Agent/LLMProvider fails terminally with
  `Agent "default" not found`.** The chart had installed CRDs and operator but
  no Agent or LLMProvider; also, without an ApprovalPolicy only escalation is
  auto-approved, so Analysis waits for a human. No run had ever got past
  admission before, so nobody saw it. *(live)*
- **`cluster-reader` alone does not grant spoke reads.** Before
  `spokeReaderRBAC`, nothing granted them, so every investigation read would
  have been denied. *(live)*
- **Red Hat SSO callers fail the adapter's A->H exchange.** The adapter's
  exchanger never sends `subject_issuer`, which external-token exchange needs.
  Expected until the adapter sets it from the token's `iss`. *(open)*

## 8. lightspeed-hub / agentic operator

- **A successful CSV is not "done reconciling".** Pausing the operator as soon
  as the CSV reported `Succeeded` froze it before it created the postgres
  bootstrap Secret: postgres stuck `ContainerCreating`, the app-server init
  crash-looped, and `praxis-proxy` went `Degraded` -- while the Application
  stayed `Synced`/`Healthy` because ArgoCD only tracks owned resources. The
  bridge now waits for the postgres Deployment to be Available, and a stopped
  operator never self-heals, so it un-pauses one it finds frozen. General rule:
  before pausing an operator, wait on a concrete child resource.
  *(live)*
- **Creating a run does not prove its run credential was written.** The
  adapter's sequence is create AgenticRun as H -> exchange H for R -> write the
  Secret as its own ServiceAccount. A failure between steps leaves a real run
  with no credential; a brief initial missing-Secret pause can be a creation
  race, but persistent absence across reconciles points at the exchange or
  write path. Check `oc auth can-i create secrets` for the adapter SA and the
  exact expected Secret names before assuming RBAC. Do not widen Secret RBAC,
  disable TLS verification, or hand-inject tokens as a workaround; decide
  explicitly whether to renew stranded tasks or start a fresh request -- CA
  trust alone does not provision old runs. *(live 2026-11-24)*
- **An init-container-built CA bundle does not refresh on rotation.** A ConfigMap
  projection update neither reruns init nor reloads Go's cached system roots.
  After confirming the rotated CA in the namespace-local ConfigMap, replace the
  adapter pods (`oc rollout restart`), during the old/new CA overlap window.
  Restarting only the container inside an existing pod does not rerun a
  completed init container; a no-op Argo sync is not a trust reload.
- **A PR existing upstream does not mean the feature does.** Check
  `gh pr view <n> --json mergedAt,reviews` and whether its scope matches; e.g.
  `lightspeed-service` PR #2866 added A2A *transport* with no authentication,
  which does not address per-caller delegated identity.

## 9. Flow monitor (`scripts/monitor.sh`)

- **Observing the chain means reading Keycloak; a pod log is corroboration.**
  "Which token is this" cannot be derived from the hop a line was seen at:
  the same `sub` wears A, H, R and U, and each hop's log level and format
  differ. The monitor now reads Keycloak realm events (`--kc`) as the
  exchange record -- client that exchanged, principal, audience -- and matches
  tokens by ACME's `fp:` fingerprint across hops. *(used live 2026-11-23/24;
  the feed itself was exercised against fake `curl`/`oc`, not yet replayed
  from a real cluster by the author)*
- **A verification only counts if it checks the observed value.** An early
  version printed an identity line unconditionally and a spoke-gateway
  pre-scan labelled any bearer it saw, then reported it as consistent with the
  hop. A Token-A regression at the adapter therefore looked verified. Cards
  now derive the hop's expected token from the pod (`praxis-spoke-<id>` vs the
  hub gateway) and compare the bearer's *fingerprint*: `IDENTITY`/`VERIFY`
  print `matches`, `MISMATCH`, `UNIDENTIFIED` or `RETIRED`. If a check cannot
  see the value, it must say so. *(found while reviewing, not live)*
- **A one-shot `oc logs -f` goes dark when a pod is replaced** (every ArgoCD
  sync), and a follower loop that ignores SIGTERM outlives a `timeout` or a
  closed terminal. The monitor re-attaches to re-resolved pods and stops at
  its next wake-up on TERM/INT. Related: `timeout 25 kc_curl ...` never ran,
  because `timeout` execs a program, not a shell function. *(live while
  testing the Keycloak feed against fake `curl`/`oc`)*
- **Two classic parsing traps, both now pinned by replay tests.** An
  unanchored timestamp matcher stamped ACME's dispatch card with the token's
  `exp=` (an ISO date later in the line); a whitespace-IFS `read` collapsed
  empty Keycloak event fields and shifted `grant_type` into the audience slot.
  `tests/scripts/test_monitor.py` replays each hop's line shape through the
  same parser the live monitor uses. *(found while wiring the Keycloak feed)*

## 10. Documentation process

- **Docs are part of the architecture.** Several of the above were made worse
  by a stale or overconfident README -- the `/health/ready` path was copied
  from a predecessor's doc, and the agent card's protocol version was copied
  with it. When this file says "check the source", it includes READMEs and
  comments.
