# Remediation end-to-end lab runbook

**For a new deployment use the [deployment runbook](lightspeed-hub-deployment-runbook.md);
for an existing one, the [brownfield steps](A2A_REMEDIATION_BROWNFIELD.md).**
This page keeps the background, the alternative human-token tests and extra
diagnostics; its phased enablement predates the standalone values enabling
the execution path directly.

Source/configuration checkpoint: **2026-11-26**. These are instructions, not a
record of a successful deployment. No build, push, sync, or live validation was
performed while writing them. Use an isolated, disposable test environment.

The flow being tested is:

```text
human → [ACME →] MAS inbound A2A → MAS worker → Praxis /a2a
  → Hub A2A adapter → AgenticRun: Analysis
  ← input-required / Execution proposals ← AgenticRunApproval
human decision → same MAS task → same Hub task → update AgenticRunApproval
  → Execution with R_exec → Verification automatically with R
  → Result CRs → Hub A2A artifacts/status → MAS → [ACME →] human
```

The current credential chain is **A → H at Praxis** (exchanger `mas-a2a`),
**H → R/R_exec at the adapter** (exchanger `lightspeed-hub-a2a`), then
**R/R_exec → U/U_exec at the read/execution spoke gateway**. A's audience is
`mas-a2a`; H's is `lightspeed-hub-a2a`; the run audiences are
`praxis-spoke-gateway` and `praxis-spoke-gateway-exec`; U/U_exec use the target's
`praxis-spoke-<target>` audience. Older instructions putting A → H at the
adapter do not describe this deployment.

## 0. Know what is missing, and where to stop

**Do not equate fixed local source with a validated deployment.** See
[the review's remediation status](A2A_REMEDIATION_REVIEW.md#remediation-follow-up--2026-11-26)
for the code-fix checkpoint. Existing deployed images do not acquire those
fixes automatically; builds and all live acceptance gates remain separate.

| Missing prerequisite | Required action |
|---|---|
| Correct approval/state handling | Verify all 15 findings H1–H5, O1–O2, M1–M4, A1–A3, G1 are resolved in the intended build inputs, with passing regression tests and independent review. Check the status linked above rather than assuming old image pins include the fixes. |
| Compatible deployed software | Build operator, adapter, one shared MAS image, and ACME from the intended fixed source. Existing standalone pins are older builds. |
| Existing-realm migration | Add execution client, audiences, roles, group mappings, and exchange scopes through Keycloak administration. A chart sync does not migrate an already-imported realm. |
| Execution wiring | Enable the coordinated Keycloak/Praxis/Hub switches below, not just one flag. |
| Scoped spoke authorization | Grant writer only in the disposable workload namespace, on the actual target cluster. |
| Current persisted MAS blueprint | Reuse the existing multicluster blueprint if its persisted resources point through Praxis to Hub and use `hitl_mode: ask`; otherwise update it and verify the saved ID/configuration. |
| Test identity | Use the same principal for initiation and approval. A controlled shared-ACME-principal lab is supported as described below; personally attributed approval requires a human token and per-user propagation. A separate maker/checker is not implemented by adding RBAC alone. |
| ACME UI access | Restrict reachability of both chat and approval endpoints for a shared-principal lab. Task/context possession is approval authority; a new login component is not required for that explicitly limited test. |
| Approval identity webhook, if required | The chart serves webhook TLS but does not install Mutating/ValidatingWebhookConfigurations. The approver-stamping mutator is therefore inactive. Wire/review these before relying on that field; API audit evidence and RBAC are separate checks. |
| Safe management API exposure | Current standalone intent enables the MAS API Route with `providerMode=dev` and admin `dev`. Close that Route first, or configure proper API-specific auth. |

Steps 1–3 prepare artifacts/baseline. **Do not proceed to write-path enablement
with the unresolved unsafe approval defects.** An intentionally unfixed build is
useful only to reproduce defects in a disposable lab, not to certify remediation.

Expected policy: **Analysis Automatic, Execution Manual, Verification Automatic**.
All runs configure all three step references. Rejection and analysis
`NoActionRequired` deliberately skip Execution and Verification; they are not
three-stage successful remediations.

### Selected lab direction: shared ACME principal, `mario-ipi/payments`

The agreed lab uses ACME's existing credentials, with explicit creator/reader
and approver/writer grants for that **shared principal**, not an individually
authenticated person. Grant namespaced writer RBAC only in `payments` **on
`mario-ipi`**, never on the hub, and scope the Keycloak writer role mapping to
`praxis-spoke-mario-ipi` (do not copy the broader two-spoke group example).
Workload writes in that namespace can mount Secrets or use ServiceAccounts;
the namespace must be a disposable/trusted lab boundary. Restrict who can reach
ACME's chat and approval endpoints, record the shared identity in evidence, and
remove its temporary grants after testing. The standalone values now enable
this lab scope directly: deploy it with the greenfield
[deployment runbook](lightspeed-hub-deployment-runbook.md) (B3 has the token
gate, `mario-ipi` RBAC and ACME tests), or upgrade an existing environment
with the [brownfield steps](A2A_REMEDIATION_BROWNFIELD.md).

The detailed backend examples below retain the alternative **human-token,
hub/remediation-e2e** fixture. They are not instructions to grant hub write
access for the selected lab: adapt target, namespace, bindings, token source,
and prompts together before running them. Sections 4 and the PKCE appendix
are optional human-attribution tests, not prerequisites for shared-principal
ACME testing. Image builds and all identity/execution/RBAC changes are separate
later steps; this code-fix pass does not perform them.

## 1. Prepare the workstation and identify the actual ArgoCD apps

Use existing tools, a utility container, or a temporary virtual environment; do
not install tools into the host Python. Required: `oc`, `argocd`, `podman`, `make`,
`helm`, `jq`, Python 3, registry credentials, cluster administrative access, and
Keycloak administrative access. The adapter Makefile also invokes Go on the
builder; use a build container if an appropriate toolchain is not available.

```bash
export REPOS="$HOME/git_repos"
export PATTERN="$REPOS/multicloud-gitops"
export OPERATOR="$REPOS/lightspeed-agentic-operator"
export HUB="$REPOS/lightspeed-hub"
export MAS="$REPOS/uie-mas-hosted"
export REGISTRY=quay.io/YOUR_ORG
export BUILD_ID="e2e-$(date -u +%Y%m%d%H%M%S)"
export CA_BUNDLE='/absolute/path/to/trusted-public-and-cluster-ingress-ca.pem'
umask 077
mkdir -p /tmp/opencode
export EVIDENCE="$(mktemp -d /tmp/opencode/remediation-e2e.XXXXXX)"

oc whoami
oc whoami --show-server
oc get patterns.gitops.hybrid-cloud-patterns.io -A -o yaml
oc get applications.argoproj.io -A -o json | jq -r '
  .items[] | [.metadata.namespace, .metadata.name,
    ((.spec.sources // [.spec.source]) |
      map((.repoURL // "") + " " + (.path // .chart // "") +
          " @ " + (.targetRevision // "")) | join("; ")),
    (.status.sync.status // ""), (.status.health.status // "")]
  | @tsv'
```

Confirm the Pattern and Applications track **your pushed repository/branch**.
Changing local files or building images does not change ArgoCD's desired state.
Do not blindly change a Pattern's revision or run `make install` over an existing
installation. A genuinely fresh installation additionally needs the original
[deployment prerequisites](lightspeed-hub-deployment-runbook.md), secrets loader,
and OIDC preflight; this document is the remediation cutover for an existing base.

Log in to the correct ArgoCD instance. Set the following to actual app names from
the listing. Use `namespace/name` if your ArgoCD installation uses apps-in-any-
namespace addressing. The keys in the standalone file are the expected logical
names, but the listing is authoritative.

```bash
export PARENT_APP='<actual clustergroup app-of-apps>'
export KC_APP='<keycloak-oidc application>'
export PRAXIS_APP='<praxis-proxy application>'
export HUB_APP='<lightspeed-hub application>'
export API_APP='<mao-api application>'
export WORKER_APP='<mao-worker application>'
export A2A_APP='<mao-a2a application>'
export ACME_APP='<acme-agent application>'

sync_wait() {
  local diff_status=0
  argocd app get "$1" --hard-refresh >/dev/null || return
  argocd app diff "$1" || diff_status=$?
  # 1 means a normal diff; other nonzero statuses are errors.
  if [ "$diff_status" -gt 1 ]; then return "$diff_status"; fi
  argocd app sync "$1" || return
  argocd app wait "$1" --sync --health --operation --timeout 1800
}
```

Use Bash for the snippets. Review `argocd app diff` **before** invoking the
helper if you need a manual approval checkpoint; the helper displays the diff but
does not pause for confirmation. Do not add `--prune` indiscriminately. If automatic sync already started the same
operation, wait for it rather than starting a competing sync.

**Automatic sync matters:** `values-global.yaml` sets `syncPolicy: Automatic`.
Use a **separate reviewed commit/push per phase below, wait for it, then prepare
the next phase**. A single commit enabling everything cannot subsequently be
ordered by typing manual child sync commands. Parent sync distributes child
Application parameters; child sync applies their manifests. Chart sync waves do
not impose readiness ordering across all the separate Applications.

## 2. Build and push the four application images

First finish the fixes and verify all four source trees contain the intended
files, including currently untracked source/tests/templates. Prefer reviewed,
clean source commits. Do not use `git add .` in these dirty working trees: there
are unrelated files, caches, and local credential/CA artifacts.

For a diagnostic dirty-tree build, the timestamp below avoids falsely labelling
the image as an exact HEAD build. Record dirty status and the tested source
changes separately. Do not treat such an image as a reproducible release.

**Inspect the build context before building.** MAS's current Dockerfile copies
the entire context with `ADD .` and has no repository `.dockerignore`. Do not
send local `.env` files, credentials, private keys, logs, or caches into that
context. Prefer a reviewed clean source checkout, or prepare a separate sanitized
context that retains all intended source changes, including untracked build
inputs. Do not delete or overwrite existing local artifacts to clean this
working tree. The commands below assume the chosen build context is safe.

```bash
for repo in "$PATTERN" "$OPERATOR" "$HUB" "$MAS"; do
  git -C "$repo" rev-parse HEAD
  git -C "$repo" status --short
done > "$EVIDENCE/source-state.txt"

export OP_IMG="$REGISTRY/lightspeed-agentic-operator:$BUILD_ID"
export HUB_IMG="$REGISTRY/lightspeed-hub-adapter:$BUILD_ID"
export MAS_IMG="$REGISTRY/mas-api:$BUILD_ID"
export ACME_IMG="$REGISTRY/acme-agent:$BUILD_ID"

podman login quay.io
podman login registry.redhat.io

# Operator (not the sandbox).
make -C "$OPERATOR" docker-build CONTAINER_TOOL=podman IMG="$OP_IMG"
podman push --digestfile "$EVIDENCE/operator.digest" "$OP_IMG"

# Adapter: requires the sibling operator/api checkout through go.mod replace.
make -C "$HUB" image-a2a-adapter CONTAINER_TOOL=podman A2A_ADAPTER_IMG="$HUB_IMG"
podman push --digestfile "$EVIDENCE/adapter.digest" "$HUB_IMG"

# One MAS image, three runtime roles: API, worker, inbound A2A.
podman build -f "$MAS/Dockerfile" -t "$MAS_IMG" "$MAS"
podman push --digestfile "$EVIDENCE/mas.digest" "$MAS_IMG"

# ACME build context is the agent directory, not the pattern root.
podman build -f "$PATTERN/agents/acme_agent/Containerfile" \
  -t "$ACME_IMG" "$PATTERN/agents/acme_agent"
podman push --digestfile "$EVIDENCE/acme.digest" "$ACME_IMG"
```

Build on/for the cluster's architecture. Operator builders require authenticated
access to `registry.redhat.io`. MAS's Dockerfile downloads an internal Red Hat CA
from `certs.corp.redhat.com`; the builder needs that network access. The Hub target
vendors the sibling API into its staging context: an unrelated old operator API
checkout is not a compatible input. Do not run either repository's standalone
`make deploy*`: ArgoCD owns the deployed resources here.

Make every image pullable by the cluster, using public lab repositories or the
appropriate namespace/ServiceAccount pull-secret setup; registry login on the
build workstation does not configure cluster image pulls.

There is **one more existing runtime image to pin**, not a fifth required source
change: `agenticOperator.sandbox.image`. Replace the current floating sandbox
`:main` with the tested image's digest. None of the four commands builds that
sandbox. Confirm it supports Execution/Verification, `oc`, the chosen LLM
provider, gateway kubeconfig, and Result publication.

Update these parameters under each app's `overrides` in
`variants/standalone/values-standalone.yaml`:

| App key | Image parameters |
|---|---|
| `lightspeed-hub` | `agenticOperator.image.repository`, `.tag`, `.digest`; `adapter.image.repository`, `.tag`, `.digest`; `agenticOperator.sandbox.image` |
| `mao-api`, `mao-worker`, `mao-a2a` | `image.repository`, `.tag`, `.digest`: **same MAS digest on all three** |
| `acme-agent` | `image.repository`, `image.tag` |

Use the registry repositories above, `image.tag: "$BUILD_ID"` as the actual
expanded string, and the corresponding `sha256:...` from the digest files. YAML
does not expand shell variables. Quote every tag. Hub/MAS charts prefer digest
over tag; **replace any existing digest too**. ACME's chart currently supports
`repository:tag`, not `image.digest`; use a unique registry-enforced immutable
tag and record/verify its actual deployed digest. Praxis needs configuration
changes, not a new application build; retain its tested pinned image.

Run the source regression suites and chart tests in ephemeral environments.
For the pattern, for example:

```bash
python3 -m venv "$EVIDENCE/venv"
"$EVIDENCE/venv/bin/pip" install pytest pyyaml
cd "$PATTERN"
"$EVIDENCE/venv/bin/python" -m pytest tests/chart -q
./pattern.sh make show > "$EVIDENCE/pattern-install-render.yaml"
./pattern.sh make validate-schema
```

These are preflight checks, **not proof of readiness**. The review reproduced
defects despite passing focused suites. Render the affected charts with their
actual per-Application overrides, not just `helm -f values-standalone.yaml`:
those overrides are nested under `clusterGroup.applications`, not chart-root
values. After each parent sync, `argocd app manifests "$APP"` shows the actual
desired render. Check vendored CRDs against the operator API you built; an image
upgrade does not automatically update the chart's vendored schemas.

## 3. Phase A — close the development API Route; deploy safe baseline images

In `mao-api.overrides`, set `route.enabled` to `"false"`. A private development
management API is a temporary lab arrangement, not production authentication;
restrict cluster-internal access/port-forward permissions too. Alternatively
configure API-specific Keycloak authentication (`mas-api` audience and real
administrator identities), not the inbound A2A allow-list.

Keep **all execution switches disabled** in this phase. Include the compatible
images and reviewed chart/source changes, but not write-path activation. Do not
let a broad push accidentally include later phases already edited locally.

After reviewing, committing the intended files, and pushing **only Phase A**:

```bash
sync_wait "$PARENT_APP"
sync_wait "$API_APP"
sync_wait "$WORKER_APP"
sync_wait "$A2A_APP"
sync_wait "$HUB_APP"
sync_wait "$ACME_APP"

oc -n mao get routes
oc -n mao rollout status deployment/mas-api --timeout=10m
oc -n mao rollout status deployment/mas-worker --timeout=25m
oc -n lightspeed-hub rollout status deployment/lightspeed-hub --timeout=10m
oc -n lightspeed-hub rollout status deployment/lightspeed-hub-agentic-operator --timeout=10m
```

The API Service/Deployment is **`mas-api`**, even though its app key is
`mao-api`; worker is **`mas-worker`**. MAS A2A's resource fullname derives from
the Helm release (normally `mao-a2a-mas-a2a` for release `mao-a2a`); discover it
with `oc -n mao get deploy,svc,route`. Do not assume `deployment/mao-a2a`.
Verify the public development Route is actually absent; removing desired YAML
without pruning may leave it behind. If needed, review
`argocd app diff "$API_APP"` and deliberately sync **that app** with `--prune`.

Check all pods are using the expected images/imageIDs, not merely that the push
succeeded. Also check Mongo, Redis, Temporal and worker readiness; A2A readiness
does not prove the Temporal worker can execute the blueprint.

## 4. Phase B — Vault, existing Keycloak realm, and human test principal

Use the migration in
[keycloak-oidc README](../charts/all/keycloak-oidc/README.md#remediation-execution-token-and-role-separation-disabled-by-default).
**Never delete/reimport the existing realm to add these resources.**

1. Merge the new `praxis-spoke-gateway-exec` entry from
   `values-secret.yaml.template` into your existing **home-directory** secrets
   input. Do not replace that input or regenerate existing exchanger secrets.
   Run `./pattern.sh make load-secrets` with your normal secrets-loader setup.
   The new Vault record is `secret/data/global/praxis-spoke-gateway-exec`, property
   `praxis-spoke-gateway-exec-client-secret`, generated with `alphaNumericPolicy`.
2. Create exchange-only client `praxis-spoke-gateway-exec`: confidential,
   standard token exchange enabled, browser/direct/implicit/service-account
   grants disabled. Match its secret to the new Vault value, handled securely.
3. Add the R_exec audience mapper to `lightspeed-hub-a2a`, retaining its R
   mapper. Ensure the audience clients actually exist.
4. Add `agenticrun-creator`/`agenticrun-approver` roles on the Hub audience
   client, and `reader`/`writer` on both spoke audience clients. Preserve ordinary
   creator/reader mappings. Create a restricted group, e.g.
   `remediation-e2e-approvers`, and give **that group only** the approver/writer
   additions. A designated human also needs ordinary `acme-agent-rca` membership.
5. Limit the read exchanger's target scopes to `reader`; execution exchanger's
   to `reader,writer`. Configure audience-specific role-to-`groups` mappers and
   the `mas-a2a` Hub role scope/mapper for creator plus approver. Do not turn on
   full-scope audience resolution as a shortcut.
6. Read back the actual clients, scopes, roles, mappers and group memberships.
   PostSync repairs **secrets**, not these existing-realm objects.

Enable these **keycloak-oidc app parameters only**, then commit/push Phase B:

```yaml
- name: keycloak.hubExchange.executionTokenAudience
  value: praxis-spoke-gateway-exec
- name: keycloak.spokeGatewayExecExchanger.enabled
  value: "true"
- name: keycloak.spokeGatewayExecExchanger.vaultKey
  value: secret/data/global/praxis-spoke-gateway-exec
- name: keycloak.spokeGatewayExecExchanger.spokeAudiences[0]
  value: praxis-spoke-hub
- name: keycloak.spokeGatewayExecExchanger.spokeAudiences[1]
  value: praxis-spoke-mario-ipi
- name: keycloak.exchangeRoles.remediation.groups[0]
  value: remediation-e2e-approvers
```

```bash
sync_wait "$PARENT_APP"
sync_wait "$KC_APP"
oc -n keycloak-system wait externalsecret/praxis-spoke-gateway-exec-client \
  --for=condition=Ready --timeout=5m
oc -n praxis-proxy wait externalsecret/praxis-spoke-gateway-exec-client \
  --for=condition=Ready --timeout=5m
oc -n keycloak-system logs job/rca-realm-secrets-reconciler
```

### Optional personally attributed test: human Token A

For the alternative human-token backend test, provision a temporary OIDC public test client,
for example `remediation-e2e`, using standard authorization-code + PKCE S256:

- Exact redirect `http://127.0.0.1:8765/callback`; no wildcard redirects.
- Direct password, implicit and service-account grants disabled.
- `fullScopeAllowed=false`; access-token audience mapper for **`mas-a2a` only**.
  Exclude other default-scope audience resolvers if they add audiences.
- Use the designated human account's ordinary and remediation group memberships.
  Do not mint a service-account token and call it a human token.
- Add this client's exact `azp` (`remediation-e2e`) to Praxis
  `policy.allowedCallers` in Phase C, preserving existing entries.
- For the MAS test, add the human's exact validated `sub`/principal to
  `mao-a2a`'s `a2a.allowedPrincipals`, preserving current allowed principals.

Obtain Token A through that client with your organization's OIDC login tooling;
use the PKCE helper in the appendix if needed. Save the access token in a private
file outside Git, referenced by `A2A_TOKEN_FILE`. Inspect claims locally: issuer
is the expected realm, audience is only `mas-a2a`, `sub` is the human, `azp` is
the test client, and expiry is sufficient. Decoding is inspection, not signature
validation; the services must still validate the token.

The same human must initiate and approve the task. For renewal, get a fresh
token for that same principal. Do not substitute an OpenShift SA token, console
token with extra audiences, or another person's token.

## 5. Phase C — execution gateways, still no Hub execution activation

Add to the **praxis-proxy** app's overrides:

```yaml
- name: spokeGatewayConfig.execution.enabled
  value: "true"
- name: spokeGatewayConfig.execution.exchangeClient.clientId
  value: praxis-spoke-gateway-exec
- name: spokeGatewayConfig.execution.exchangeClient.secretName
  value: praxis-spoke-gateway-exec-client
- name: spokeGatewayConfig.execution.inboundAudience
  value: praxis-spoke-gateway-exec
- name: spokeGatewayConfig.execution.allowedCallers[0]
  value: lightspeed-hub-a2a
- name: policy.allowedCallers[2]
  value: remediation-e2e
```

Index `2` assumes the current two existing callers. Inspect the list and append
without replacing another entry. Keep the ordinary read gateways/audience/client
unchanged. The expected execution resources are `praxis-spoke-hub-exec` and
`praxis-spoke-mario-ipi-exec`, separate from the read resources.

Commit/push Phase C, then:

```bash
sync_wait "$PARENT_APP"
sync_wait "$PRAXIS_APP"
oc -n praxis-proxy rollout status deployment/praxis-spoke-hub-exec --timeout=10m
oc -n praxis-proxy rollout status deployment/praxis-spoke-mario-ipi-exec --timeout=10m
oc -n praxis-proxy get svc,networkpolicy,configmap
```

Verify serving certificates and CA trust, restricted gateway ingress, target API
reachability, and that exec network-policy labels do not accidentally select read
pods. Both targets are registered; first test **`hub`**, which acts as its own
spoke. Do not copy the HTML's illustrative `west` target into these values.

## 6. Create the disposable workload, then Phase D — enable the Hub path

On the hub target, create only a dedicated test namespace/workload. Do not use
`payments`, a real application namespace, or any system/control-plane namespace.
Use a tested, cluster-pullable UBI minimal image **by digest** below.

```bash
export TEST_NS=remediation-e2e
export TEST_IMAGE='registry.access.redhat.com/ubi9/ubi-minimal@sha256:REPLACE_WITH_TESTED_DIGEST'
oc create namespace "$TEST_NS"
cat <<EOF | oc -n "$TEST_NS" apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: remediation-demo
spec:
  replicas: 1
  selector:
    matchLabels: {app: remediation-demo}
  template:
    metadata:
      labels: {app: remediation-demo}
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: demo
          image: $TEST_IMAGE
          command: [/bin/sh, -c, 'sleep 3600']
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 10m, memory: 32Mi}
            limits: {cpu: 100m, memory: 128Mi}
EOF
oc -n "$TEST_NS" rollout status deployment/remediation-demo --timeout=5m
# Introduce a deterministic, contained failure only after the baseline is healthy.
oc -n "$TEST_NS" patch deployment/remediation-demo --type=json -p \
  '[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["/bin/sh","-c","exit 42"]}]'
oc -n "$TEST_NS" get pods
```

If this namespace already exists, inspect it rather than reusing it blindly.
Keep application credentials and privileged ServiceAccounts out of it. Namespace
workload-writer rights can indirectly mount Secrets/use ServiceAccounts even
when the role itself lacks Secret reads. Set quotas/admission restrictions if
needed. Normal platform-created image-pull Secrets do not make it an empty trust
boundary.

After all safety fixes and prior phase gates pass, add these **lightspeed-hub**
parameters and commit/push Phase D:

```yaml
- name: adapter.executionTokenAudience
  value: praxis-spoke-gateway-exec
- name: agenticOperator.config.gatewayExecutionServerURLTemplate
  value: https://praxis-spoke-{targetCluster}-exec.praxis-proxy.svc.cluster.local:8443
- name: adapter.hubRunApproversGroup
  value: keycloak:lightspeed-hub-a2a:agenticrun-approver
- name: spokeExecutorRBAC.enabled
  value: "true"
- name: spokeExecutorRBAC.groups[0]
  value: keycloak:praxis-spoke-hub:writer
- name: spokeExecutorRBAC.namespaces[0]
  value: remediation-e2e
```

Keep read mode configured: `adapter.runTokenAudience=praxis-spoke-gateway`,
`sandboxCredentialMode=gateway`, the ordinary gateway URL, and its CA Secret.
Keep the target allow-list and admission-policy allow-list `hub,mario-ipi` aligned
with Praxis. The reader RoleBindings must remain present.

```bash
sync_wait "$PARENT_APP"
sync_wait "$HUB_APP"
oc -n lightspeed-hub rollout status deployment/lightspeed-hub --timeout=10m
oc -n lightspeed-hub rollout status deployment/lightspeed-hub-agentic-operator --timeout=10m
oc get approvalpolicy cluster -o yaml
oc -n lightspeed-hub get configmap lightspeed-agentic-configuration -o yaml
oc -n "$TEST_NS" get rolebinding lightspeed-hub-spoke-executor -o yaml
```

**Stop unless the live policy is Analysis Automatic / Execution Manual /
Verification Automatic** (Escalation Automatic). Runs must reference all three.
Do not use a manual `AgenticRun` or hand-edit an Approval CR as the successful
A2A test: that bypasses the identity/decision bridge being tested.

This chart's writer binding is local to the chart's cluster. Testing remote
`mario-ipi` later requires equivalent reviewed namespace RoleBindings there,
native OIDC trust for `praxis-spoke-mario-ipi`, proper read grants, API CA trust,
and gateway-to-API reachability. A binding on the hub grants no remote writes.
The target is structured via `X-Hub-Cluster`; the requested namespace remains
prose in `spec.request`, not an enforced `targetNamespaces` scope. Writer RBAC
and admission restrictions, not that prose, bound the allowed write namespace.

## 7. Test Praxis → Hub first: rejection, then approval

This bypasses MAS/ACME only; it still uses the public policy gateway and the
actual caller/exchange path. Do not publish a public Route directly to the
adapter or substitute its ServiceAccount.

```bash
export PRAXIS_BASE="https://$(oc -n praxis-proxy get route praxis-proxy -o jsonpath='{.spec.host}')"
export HUB_RPC="$PRAXIS_BASE/a2a"
export CA_BUNDLE='/absolute/path/to/trusted-public-and-cluster-ingress-ca.pem'
export A2A_TOKEN_FILE="$EVIDENCE/human-access-token"
# Populate this file through the PKCE login; do not echo a token into shell history.
python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ['EVIDENCE'])
token = Path(os.environ['A2A_TOKEN_FILE']).read_text().strip()
assert token and '\n' not in token and '\r' not in token
(root / 'auth-header').write_text('Authorization: Bearer ' + token + '\n')
PY

curl --fail-with-body --cacert "$CA_BUNDLE" \
  "$PRAXIS_BASE/a2a/.well-known/agent-card.json" | jq .

# Separate terminals; evidence contains resource names/status, not Secret contents.
bash scripts/monitor.sh --join --since 10m
# In another terminal:
oc -n lightspeed-hub get agenticruns -w
# Take snapshots of related resources in a further terminal:
oc -n lightspeed-hub get agenticrunapprovals,analysisresults,executionresults,verificationresults
```

From the pattern directory, submit a Hub task (legacy Hub A2A dialect):

```bash
export HUB_MESSAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
jq -n --arg mid "$HUB_MESSAGE_ID" '{jsonrpc:"2.0",id:1,method:"message/stream",
  params:{message:{kind:"message",role:"user",messageId:$mid,parts:[{kind:"text",
    text:"Investigate Deployment remediation-demo in namespace remediation-e2e on cluster hub. It used to run /bin/sh -c sleep 3600 and was deliberately changed to exit 42. Propose bounded fixes only in remediation-e2e, do not execute until I approve, and verify the rollout automatically after execution."}]}}}' \
  > "$EVIDENCE/hub-start.json"
curl --fail-with-body --no-buffer --cacert "$CA_BUNDLE" "$HUB_RPC" \
  -H @"$EVIDENCE/auth-header" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' -H 'X-Hub-Cluster: hub' \
  --data-binary @"$EVIDENCE/hub-start.json" | tee "$EVIDENCE/hub-start.sse"
```

Record the returned **Hub** `taskId`, `contextId`, and the run named in
`approvalRequest.run`. Do not invent these IDs or substitute a MAS/ACME ID.
If attachment ends while still working, get/resubscribe to the same task; do not
submit a fresh investigation. `input-required` with `final:true` ends an SSE
attachment, **not** the resumable task.

```bash
export HUB_TASK='<returned Hub taskId>'
export HUB_CONTEXT='<returned Hub contextId>'
export RUN='<approvalRequest.run>'
oc -n lightspeed-hub get agenticrun "$RUN" -o yaml
oc -n lightspeed-hub get agenticrunapproval "$RUN" -o yaml
export RUN_UID="$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')"
export TOKEN_SECRET="ls-run-token-$RUN_UID"
# Keys only; never print .data token values.
oc -n lightspeed-hub get secret "$TOKEN_SECRET" -w \
  -o go-template='{{.metadata.name}} {{range $key, $value := .data}}{{$key}} {{end}}{{"\n"}}'
```

Before deciding: successful Analysis has real options, Execution is pending,
there is **no execution sandbox or `token-execution`**, and the failing workload
has not been changed. If analysis says `NoActionRequired`, this is not a test of
the remediation path; investigate the fixture/analysis before proceeding.

### 7a. Reject first

Use a new message ID but the same task/context/target:

```bash
jq -n --arg task "$HUB_TASK" --arg ctx "$HUB_CONTEXT" \
  --arg mid "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  '{jsonrpc:"2.0",id:2,method:"message/stream",params:{message:{kind:"message",
    role:"user",messageId:$mid,taskId:$task,contextId:$ctx,
    parts:[{kind:"data",data:{reject:{stage:"Execution"}}}]}}}' \
  > "$EVIDENCE/hub-decision.json"
curl --fail-with-body --no-buffer --cacert "$CA_BUNDLE" "$HUB_RPC" \
  -H @"$EVIDENCE/auth-header" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' -H 'X-Hub-Cluster: hub' \
  --data-binary @"$EVIDENCE/hub-decision.json" | tee "$EVIDENCE/hub-decision.sse"
```

Expected: same-name Approval CR has Execution `Denied`; no write credential is
minted, no Execution/Verification work is launched, workload stays broken. Hub
returns **completed investigation with analysis and rejection explanation**, not
“remediation succeeded.” Observe the key watch/audit trace throughout; a missing
key after cleanup alone does not prove it was never minted.

### 7b. Approve a selected option on a fresh investigation

Repeat the initial submission with a **new** message ID/fresh task (do not try
to reverse the denied decision), then record its new task/context/run. Review
the complete original AnalysisResult, not just a short UI preview. In the
decision command above replace the DataPart with:

```json
{"kind":"data","data":{"approve":{"stage":"Execution","option":0}}}
```

`option:0` is displayed proposal 1; `option:1` is displayed proposal 2. Use only
an option actually present and reviewed. The adapter updates Approval, **not**
`AgenticRun.spec.execution`. Expected: approved index matches the original
AnalysisResult, execution key appears only at approval, Execution runs through
the `-exec` gateway, that key is removed after Execution, Verification runs
through the read gateway without another answer, and all successful Result
artifacts return on the same Hub task.

```bash
oc -n "$TEST_NS" rollout status deployment/remediation-demo --timeout=5m
oc -n lightspeed-hub get agenticrun "$RUN" -o yaml
oc -n lightspeed-hub get executionresults,verificationresults -o yaml
```

Success requires actual healthy workload **and** successful VerificationResult /
run conditions, not an HTTP 200, stream EOF, or a fluent model summary. If
verification fails/escalates, record that result rather than declaring success.

## 8. Register the MAS blueprint, then Phase E — activate its ID/principal

With the API Route closed, use a separate terminal:

```bash
oc -n mao port-forward --address 127.0.0.1 svc/mas-api 8002:8002
```

In the working terminal:

```bash
export MAS_API_URL=http://127.0.0.1:8002
export MAS_TOKEN=dev-token   # ONLY for this isolated private development API
export HUB_ADAPTER_A2A_URL="$PRAXIS_BASE/a2a"
export HUB_ADAPTER_TARGET_HEADER=X-Hub-Cluster
export HUB_ADAPTER_CLUSTERS=hub,mario-ipi
export HUB_ADAPTER_HITL_MODE=ask
export KUBECONFIG='<your existing administrative kubeconfig path>'
```

For a Keycloak-authenticated API, replace `MAS_TOKEN` with a valid management
token for its **`mas-api`** audience/admin identity. That is not Token A. The
script otherwise defaults `KUBECONFIG` to `~/gitops.kubeconfig`, so set it
explicitly if you use another file.

Provide your working orchestrator provider inputs (`LITELLM_MODEL`,
`LITELLM_API_BASE`, `LITELLM_API_KEY` for the LiteLLM route; or the script's
Vertex configuration and already-provisioned ADC). Keep secrets out of Git/logs.
The worker and sandbox LLM credentials are separate requirements even when they
share a Vault source. Ensure the worker trusts the Praxis Route CA.

If the existing saved multicluster blueprint already meets these requirements,
reuse it and skip the setup script. Verify the persisted node/resource settings,
not just the local JSON or the blueprint's name. Otherwise, after reviewing its
inventory-update side effects, run:

```bash
cd "$PATTERN"
./setup-mas-blueprint.sh --dry-run multi-cluster
# Inspect output: Praxis PUBLIC URL, target header, closed cluster list,
# and investigation-node hitl_mode: ask. Dry run does not save a blueprint.
./setup-mas-blueprint.sh multi-cluster
```

The script creates/updates named inventory resources as well as saving a
blueprint. Use an isolated lab inventory; on a shared MAS instance, same-named
resources can be updated, so review that effect first. It writes temporary
blueprint material under `/tmp`; keep it private and treat it as sensitive.

Use **`multi-cluster`**, not `multi-cluster-hitl`: the latter adds a separate
pre-orchestrator `oc whoami` approval gate, not the Hub Execution gate. Never put
a static Hub bearer token into the node: MAS must delegate the inbound caller's
credential. The target belongs in structured `data.headers.X-Hub-Cluster`, not
merely prose in the prompt.

Record the returned blueprint ID. Inspect the persisted blueprint/resource
configuration via MAS administration; do not assume the currently pinned ID
`bdf07d00-a104-47b1-99fb-47af1a7a8e04` has been rewritten. In the **mao-a2a**
app overrides, set `a2a.blueprintId` to the returned ID and append the human's
exact principal to `a2a.allowedPrincipals`. Keep Keycloak/multi-issuer validation
enabled, audience `mas-a2a`, and the existing ACME allow-list entries. Commit/push
Phase E and sync parent → MAS A2A:

```bash
sync_wait "$PARENT_APP"
sync_wait "$A2A_APP"
oc -n mao get deploy,svc,route
```

Confirm API, worker, and A2A share **the same MAS image**, Mongo database, Redis,
Temporal namespace, `RUN_CREDENTIAL_KEYS`, and `HITL_APPROVAL_COLL`. Compare key
configuration securely without printing raw credentials. Mismatched Fernet
keys/mailboxes can make a healthy service incapable of resuming the workflow.

## 9. Test MAS → Praxis → Hub with the same human principal

Discover the actual MAS A2A Route and set `A2A_BASE_URL` to its HTTPS origin,
without `/a2a/jsonrpc`. Its card must advertise an external `supportedInterfaces`
URL and A2A **1.0**. The Hub's legacy `message/stream` request is **not** the MAS
wire contract; use the MAS-pinned SDK rather than copying the Hub curl body.

Create a separate ephemeral client environment:

```bash
python3 -m venv "$EVIDENCE/client-venv"
"$EVIDENCE/client-venv/bin/pip" install 'a2a-sdk[http-server]==1.1.5' httpx
export A2A_BASE_URL='https://ACTUAL_MAS_A2A_ROUTE'
export SSL_CERT_FILE="$CA_BUNDLE"
```

Save this helper as **`$EVIDENCE/mas-client.py`**, not in production source:

```python
import asyncio
import os
import uuid
from pathlib import Path

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types import (
    GetTaskRequest, Message, Part, Role, SendMessageConfiguration,
    SendMessageRequest,
)
from google.protobuf.json_format import MessageToJson, ParseDict


async def main():
    token = Path(os.environ['A2A_TOKEN_FILE']).read_text().strip()
    timeout = httpx.Timeout(330, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as discovery:
        card = await A2ACardResolver(
            httpx_client=discovery, base_url=os.environ['A2A_BASE_URL']
        ).get_agent_card()
    async with httpx.AsyncClient(
        timeout=timeout, headers={'Authorization': f'Bearer {token}'}
    ) as transport:
        client = await create_client(
            agent=card,
            client_config=ClientConfig(httpx_client=transport, streaming=False),
        )
        async with client:
            task_id = os.environ.get('MAS_TASK')
            action = os.environ.get('ACTION', 'get' if task_id else 'start')
            if action == 'get':
                task = await client.get_task(GetTaskRequest(id=task_id))
                print(MessageToJson(task))
                return
            if action == 'start':
                parts = [Part(text=os.environ['PROMPT'])]
            else:
                assert action in ('approve', 'reject')
                decision = {'decision': action}
                if action == 'approve':
                    # Structured MAS and Hub options are both zero-based.
                    # OPTION here is a human-facing number supplied by the user.
                    number = int(os.environ['OPTION'])
                    assert number >= 1, 'OPTION must be a displayed number >= 1'
                    decision['modifiedArgs'] = {'option': number - 1}
                parts = [ParseDict({'data': decision}, Part())]
            message = Message(
                message_id=os.environ.get('MESSAGE_ID') or uuid.uuid4().hex,
                role=Role.ROLE_USER, parts=parts,
            )
            if action != 'start':
                message.task_id = os.environ['MAS_TASK']
                message.context_id = os.environ['MAS_CONTEXT']
            async for update in client.send_message(SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=True),
            )):
                print(MessageToJson(update))


asyncio.run(main())
```

Use a new deliberately broken fixture for each fresh positive/negative test.
If resetting the same Deployment, ensure the previous run is terminal and no
execution is still active before reapplying the `exit 42` fault.

```bash
export PROMPT='On cluster hub, investigate Deployment remediation-demo in namespace remediation-e2e. It used to run /bin/sh -c sleep 3600 and was deliberately changed to exit 42. Propose bounded fixes, ask me before execution, and verify automatically afterwards. Delegate this live investigation to Hub Cluster Investigator.'
unset MAS_TASK MAS_CONTEXT
export MESSAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
ACTION=start "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
```

Record the returned **MAS** task ID/context. Separately correlate the Hub task
and run in the worker/adapter logs. They are **different ID spaces**.

```bash
export MAS_TASK='<returned MAS task ID>'
export MAS_CONTEXT='<returned MAS context ID>'
ACTION=get "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
# Repeat get until INPUT_REQUIRED, terminal failure, or completion.
```

Check this is specifically the **Hub Execution** gate, with all options intact,
not a generic MAS tool approval. If no Hub run appears, debug delegation/header/
blueprint/provider configuration before approving anything. Refuse to approve
truncated/unreadable options (M4).

First test rejection, then a fresh task for approval:

```bash
# Human rejection on the SAME paused MAS task/context, NEW decision message ID.
export MESSAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
ACTION=reject "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
ACTION=get "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"

# Wait until rejection is terminal, then submit a fresh investigation.
unset MAS_TASK MAS_CONTEXT
export MESSAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
ACTION=start "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
export MAS_TASK='<NEW returned MAS task ID>'
export MAS_CONTEXT='<NEW returned MAS context ID>'
ACTION=get "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
# Repeat get until INPUT_REQUIRED and review the options, then select option 1:
export MESSAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
ACTION=approve OPTION=1 "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
ACTION=get "$EVIDENCE/client-venv/bin/python" "$EVIDENCE/mas-client.py"
```

For the approved task, poll to real completion while watching the actual run,
Approval, credentials' **key names**, workload, and Result CRs. The explicit MAS
`modifiedArgs.option:0` must become Hub `approve.option:0` for displayed option 1.
Only the plain-chat fallback uses a one-based number in prose. This helper avoids
prose inference for a smoke test; it does not fix the review's malformed-choice,
revision, durable gate, or projection defects. Retrying an unknown outcome uses
the **same** message ID/payload; a distinct new human decision uses a new ID.

Required observation: pause → human selection → same remote task resumes →
Execution → automatic Verification → MAS returns the actual result. A
nonterminal attachment closing must never be considered completion.

## 10. ACME UI is a separate final gate

The updated ACME image must preserve the same paused MAS binding and route a
decision directly to it, without letting the routing model create a new task.
Use the embedded UI's existing Route and a fresh conversation. Repeat rejection
and approval, checking all three layers' IDs and the actual Kubernetes outcome.

**Label this as shared-principal lab approval with the current auth configuration.**
ACME's user-token option is not implemented. For the selected lab, its shared
principal needs explicit approver and `mario-ipi`-only writer grants; protect
the endpoints and restrict writer RBAC to `payments` as described in section 0.
Do not claim personal attribution or independent maker/checker authorization.
Per-user authentication/token propagation is a future requirement only for
those stronger claims. The direct human MAS test above is an alternative
backend identity test, not proof that ACME implements login.

ACME must never report `working → EOF` as success. It may recover a confirmed
pause/result with a read-only task query or explicitly fail the attachment
(the current implementation does the latter when the task is still working).
It must preserve confirmed-open invalid-decision gates for correction and
distinguish protocol failures/rejection from successful remediation. These are
A1–A3 acceptance tests, not solved by raising the stream timeout. Automatic
reattachment to a still-working task is not claimed.

## 11. Acceptance tests before calling this end-to-end ready

Record pass/fail/not-run, image digests, Git revisions, principal, target, task
IDs, run UID, actual Approval, Result outcomes, and API/audit observations.

- **Read/write split:** real exchanges produce R only for
  `praxis-spoke-gateway` and R_exec only for `praxis-spoke-gateway-exec`. Actual
  gateway calls reject the wrong audience. U is reader-only even for a human
  who belongs to the remediation group; U_exec gains writer only for the
  designated human. Decode securely and test requests, not just mapper YAML.
- **Namespace/target bounds:** dry-run mutating API requests with read credentials
  are denied. Approved execution can write the test namespace, not another
  namespace/system resources/another target. Check ordinary reader permissions
  separately; the claim is not that every out-of-namespace read is forbidden.
  Use a separate empty denied namespace on the same target for the negative
  write test, and perform the actual token-authenticated API/gateway request.
  Administrator `oc auth can-i --as-group` checks alone do not prove JWT claims.
- **Approval:** no write credential/work before the answer; rejection mints none;
  approved option index is correct. Another principal cannot get/approve the
  task. Do not confuse this ownership denial with support for separate
  maker/checker approval.
- **Decision validation:** malformed/fractional/negative/out-of-range selection
  and mixed approve/revise commands authorize **nothing**. Corrected valid
  input resumes the same real pending gate. Revision prose does not approve.
- **Verification/renewal:** verification starts without another approval;
  credential expiry/renewal does not strand Verification or revive terminal
  execution. Test expiry at Analysis, approval, Execution, and Verification.
  A sandbox-time 401/403 is not automatically an operator credential pause.
- **Continuity:** reconnect an attachment and restart/kill the worker and ACME
  in a controlled test. No duplicate delegation, lost binding, false success,
  or stale-event credential deletion. Current outbound bindings are not proven
  failover-durable; record that limitation rather than asserting exactly-once.
- **Real network enforcement:** validate source-pod selectors/CNI on actual
  sandbox-to-gateway traffic and rejection from an unauthorized namespace.
  Port-forward tests are useful diagnostics but bypass parts of normal ingress.
- **Residual boundaries:** selected plan is model input, not a command-level
  authorization rule; copied bearer credentials outlive Secret deletion until
  expiry; default-SA hub Result publication permissions accumulate across active
  sandboxes. Decide explicitly whether those risks are acceptable for the lab
  versus any wider deployment.

## 12. Troubleshooting and rollback

| Symptom | Check first |
|---|---|
| Built image but behavior unchanged | Push visibility, actual app branch/revision, parent parameters, digest precedence, pod imageID. |
| Praxis 404 | `X-Hub-Cluster` exact spelling and registered value; structured MAS `data.headers`; no `west` target here. |
| 401/403 before a run | Token issuer/audience/expiry, Praxis `azp` caller list, MAS exact-principal allow-list. |
| Keycloak `invalid_client_credentials` / `unauthorized_client` | Live client secret vs Vault; old literal import placeholders; required exchanger-in-subject-audience condition. See lessons learned. |
| Approval forbidden | Same original principal, H approver group, namespaced Approval `get/update` RBAC, current human group mapping/fresh token. |
| Proposed forever | Live Manual gate, correct task IDs, actual Approval decision, durable MAS mailbox, truncated/consumed gate findings. |
| Execution forbidden or fails | R_exec audience, execution exchanger role scopes, U_exec groups, local vs remote namespace binding, service CA/DNS/CNI. |
| Verification stuck after renewal | O1; no sandbox plus `Verified=Unknown` is not successful verification. |
| ACME says done but Hub still working | A1/nonterminal EOF; query actual MAS task and run, do not accept the summary as evidence. |

For rollback: stop new submissions, inspect active runs, and use the operator's
documented stop/cancel workflow; closing an SSE stream does not stop execution.
Remove the temporary human group membership/client allow-list and revoke the
test login where appropriate. Push a reviewed Git change removing namespace
writer grants and disabling the Hub execution audience **together with** its
execution URL template; then disable Praxis execution gateways. Synchronize the
parent/affected children and verify removals/pruning deliberately. Do not alter
credential mode under a paused active run as a recovery shortcut (O2).

Do not rotate shared production secrets, delete a realm, or use an ArgoCD
rollback alone while automatic Git sync still points at enabling values. Merely
deleting a Secret is not bearer-token revocation; remove authorization and allow
issued tokens to expire or use the applicable revocation mechanism.

After all test runs have stopped and evidence is collected, remove **only the
disposable workload/namespace you created**, test identity/client, temporary
port-forwards, and sensitive local evidence/token files. Stop the test-namespace
writer binding through Git before namespace deletion so ArgoCD does not keep
trying to restore it. Do not delete unrelated existing resources.

## Appendix: minimal local authorization-code + PKCE login

Run this on the workstation whose browser will open the login URL (not a remote
machine whose loopback the browser cannot reach), after creating the temporary
OIDC client described in step 4. `CA_BUNDLE` must contain trusted public roots and
the Keycloak Route's signing CA; do not replace verification with `-k`.

```bash
export KC_ISSUER='https://YOUR_KEYCLOAK_ROUTE/realms/rca'
export OIDC_CLIENT_ID=remediation-e2e
export A2A_TOKEN_FILE="$EVIDENCE/human-access-token"
python3 - <<'PY'
import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

issuer = os.environ['KC_ISSUER'].rstrip('/')
client = os.environ['OIDC_CLIENT_ID']
redirect = 'http://127.0.0.1:8765/callback'
ctx = ssl.create_default_context(cafile=os.environ['CA_BUNDLE'])
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
state = secrets.token_urlsafe(32)
answer = {}

class Callback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log the authorization code.

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(path.query)
        valid = path.path == '/callback' and query.get('state') == [state]
        self.send_response(200 if valid else 400)
        self.end_headers()
        self.wfile.write(b'Login response received; return to the terminal.' if valid else b'Invalid login response.')
        if valid:
            answer.update({key: values[0] for key, values in query.items()})

with urllib.request.urlopen(issuer + '/.well-known/openid-configuration', context=ctx, timeout=30) as response:
    discovery = json.load(response)
assert discovery['issuer'] == issuer
url = discovery['authorization_endpoint'] + '?' + urllib.parse.urlencode({
    'client_id': client, 'redirect_uri': redirect, 'response_type': 'code',
    'scope': 'openid', 'state': state, 'code_challenge': challenge,
    'code_challenge_method': 'S256',
})
with HTTPServer(('127.0.0.1', 8765), Callback) as server:
    server.timeout = 1
    deadline = time.monotonic() + 300
    print('Open this URL in the local browser and sign in as the test HUMAN:\n' + url)
    while not answer and time.monotonic() < deadline:
        server.handle_request()
if 'code' not in answer:
    raise SystemExit('Login did not return a valid code.')
body = urllib.parse.urlencode({
    'grant_type': 'authorization_code', 'client_id': client,
    'redirect_uri': redirect, 'code': answer['code'], 'code_verifier': verifier,
}).encode()
request = urllib.request.Request(discovery['token_endpoint'], data=body)
with urllib.request.urlopen(request, context=ctx, timeout=30) as response:
    tokens = json.load(response)
path = Path(os.environ['A2A_TOKEN_FILE'])
with path.open('w', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
    output.write(tokens['access_token'])
path.chmod(0o600)
print('Access token saved privately; expires_in:', tokens.get('expires_in'))
PY
```

To renew, repeat the same login for the same principal and rebuild the private
curl header file if using the direct Hub test. The MAS helper reads the token
file anew on each invocation. Keep the issuer/client configuration trusted; this
small helper is for the lab, not an ACME session/authentication implementation.
