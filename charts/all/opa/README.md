# opa

A plain, self-run Open Policy Agent server (`openpolicyagent/opa run --server`)
answering one question: is the caller identified by an inbound A2A request's
`azp` claim allowed to invoke `rca-agent` at all.

This is deliberately not the Gatekeeper Operator (which is Kubernetes
admission control -- it only evaluates objects hitting the API server) and
not a Kubernetes object at all: the request this gates is an A2A HTTP call
from `ericsson-agent` (or a future second caller) to `rca-agent`, which never
touches the Kubernetes API server, so there is no admission event for
Gatekeeper or `ValidatingAdmissionPolicy` to intercept. See
`charts/all/keycloak-oidc/templates/agentic-vap-namespace-scope.yaml` for the
check that *is* a Kubernetes admission decision (which namespaces a caller
may target on an `AgenticRun`) -- these two checks are complementary, not
redundant: one gates "may this caller talk to rca-agent at all" before
anything happens, the other gates "may this caller's request touch this
namespace" once rca-agent tries to act on it.

Queried by `agents/rca_agent/rca_agent/opa.py` from `identity.py`'s
`A2AAuthenticationMiddleware`, after Keycloak token validation succeeds and
before the request reaches the agent's own code -- unreachable/erroring OPA
denies (fail closed), same posture as every other check in that middleware.

`policy.allowedCallers` is the actual policy: the `azp` (authorized party --
the confidential Keycloak client that requested the inbound token) values
permitted to call `rca-agent`. Onboarding a new upstream caller (e.g. a
future `company-b-agent`) means adding it here and to
`charts/all/keycloak-oidc` (its own confidential client, group, and
`namespaceAllowlist` entry) -- not editing `rca_agent`'s code.

Verify it's up and serving the expected policy:

```bash
oc get pods -n lightspeed-agentic-operator -l app.kubernetes.io/name=opa
oc exec -n lightspeed-agentic-operator deploy/rca-agent-opa -- \
  curl -s localhost:8181/v1/data/rca/authorization/allowed_callers
```

## Why this lives in rca-agent's own code, and what would let it not

This check exists because rca-agent's Python process is the only thing that
ever sees the inbound A2A request -- there is no proxy or admission hook in
front of it today, so `identity.py`'s `A2AAuthenticationMiddleware` has to
make the call itself (see `agents/rca_agent/rca_agent/opa.py`). That's a
real cost: it couples this authorization check to the agent's own source and
release process, unlike the namespace-scoping check in `charts/all/keycloak-oidc`
(a `ValidatingAdmissionPolicy`, enforced by the API server with zero agent
code involved).

The ideal fix is a proxy in front of rca-agent that terminates SPIFFE mTLS,
evaluates the policy itself (or calls out to this same OPA server), and only
forwards allowed requests -- so `identity.py` never has to. That's exactly
the "Envoy ext_authz + OPA" pattern this project deliberately avoided
earlier (see `agents/AUTHENTICATION.md`), because a full Envoy/service-mesh
sidecar was heavier infrastructure than a two-caller setup justified.

**[Praxis](https://github.com/praxis-proxy/praxis)** (CNCF, a lightweight
Rust proxy positioned as a lower-resource Envoy alternative for sidecar
deployments) is the closest real candidate for this, and worth revisiting --
its policy engine, **[Praxis Policy Engine / PPE](https://github.com/praxis-proxy/policy)**,
advertises almost exactly this feature set: identity resolution (JWT/SPIFFE),
an `opa` feature flag for Rego-based authorization, and -- notably -- "RFC
8693 credential exchange, giving each upstream service a token scoped to
that service," which is the same delegation pattern `identity.py`'s
`KeycloakTokenExchanger` implements by hand today. If PPE matures, a single
Praxis sidecar could plausibly replace both this OPA server *and* some of
`identity.py`'s own token-exchange code, not just the inbound check.

Checked as of this writing (2026-09-25): **not ready to build on.** `praxis`
itself has no stable release yet (latest is `v0.7.0`, marked prerelease --
every release to date has been a prerelease); `praxis-proxy/policy` (PPE) was
only created about two months ago and its own README states outright that
"the public API will move between minor versions while the shape settles" --
an explicit pre-stable-API warning from the maintainers, not a guess on our
part. ext_authz support in the proxy itself is tracked as an open feature
request (`praxis-proxy/praxis#223`), not something released and documented.
This is a "watch, don't adopt yet" situation -- revisit once `praxis` cuts a
stable release and there's a documented, working example combining the `opa`
feature, JWT/SPIFFE identity, and RFC 8693 delegation together (not just
each as a separate, individually-tracked issue).
