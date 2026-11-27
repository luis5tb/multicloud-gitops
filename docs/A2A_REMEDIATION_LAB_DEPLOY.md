# Remediation lab deployment: shared ACME principal, `mario-ipi/payments`

Concrete build → rollout → test procedure for the selected lab. It complements
the general [runbook](A2A_REMEDIATION_E2E.md), which keeps the background,
the alternative human-token tests and extra diagnostics. Nothing here has been
run against the cluster yet; treat every step as something to verify.

Lab boundary: ACME's existing Keycloak service-account token is the **shared
principal** that both starts and approves runs. It gets approver rights on the
hub and `writer` **only on `mario-ipi`, only in namespace `payments`**. Anyone
who can reach ACME's UI can therefore approve a remediation in `payments`:
restrict who can reach it, and remove the grants after testing (step 9).

Flow under test: ACME → MAS (`hitl_mode: ask`) → Praxis `/a2a` → Hub adapter →
AgenticRun Analysis → approval in ACME → Execution through
`praxis-spoke-mario-ipi-exec` (R_exec → U_exec) → automatic Verification
through the read gateway → result back to ACME.

## 0. Inputs and the per-phase values changes

```bash
export REPOS="$HOME/git_repos"
export PATTERN="$REPOS/multicloud-gitops"
export OPERATOR="$REPOS/lightspeed-agentic-operator"
export HUB="$REPOS/lightspeed-hub"
export MAS="$REPOS/uie-mas-hosted"
export REGISTRY=quay.io/ltomasbo
export TAG=a2a-rem-20261126          # must match the tags in phase-a.patch
export MARIO_KUBECONFIG="$HOME/mario-ipi.kubeconfig"   # adjust
export LAB="$PATTERN/scripts/remediation-lab"   # committed lab assets
umask 077
mkdir -p "$HOME/remediation-lab-evidence"           # outside every Git repo
export EVIDENCE="$(mktemp -d "$HOME/remediation-lab-evidence/run.XXXXXX")"
```

All lab assets are committed in `scripts/remediation-lab/` of this repository:
the four cumulative `values-standalone.yaml` patches, the mario-ipi writer
RBAC manifest and the token-gate observer. The committed
`variants/standalone/values-standalone.yaml` keeps execution disabled; ArgoCD
auto-syncs, so apply and push the patches **one phase at a time**:

| Patch | App(s) | Change |
|---|---|---|
| `phase-a.patch` | acme-agent, mao-api/worker/a2a, lightspeed-hub | new image tags; disable ACME token debug/override; close the dev-mode `mas-api` Route; sandbox reminder |
| `phase-b.patch` | keycloak-oidc | execution exchanger + `acme-remediation-mario-ipi` group |
| `phase-c.patch` | praxis-proxy | `praxis-spoke-<key>-exec` gateways |
| `phase-d.patch` | lightspeed-hub | R_exec audience, Execution gateway URL, hub approver RBAC (no hub writer RBAC) |

The keycloak-oidc chart change that lets the import create a dedicated (empty)
remediation group, and the phase-ordering test in
`tests/chart/test_remediation_execution.py`, are already committed; they must
be on the pushed branch before Phase B.

Each patch applies on top of the previous one, starting from the committed
values file. If that file changed since the patches were written, use
`git apply --3way` and review the result:

```bash
cd "$PATTERN"
git apply --check "$LAB/phase-a.patch"
```

## 1. Build and push the images

Use a clean checkout of the committed branches if you can. Otherwise: MAS now
has a committed `.dockerignore` excluding `.env*`, keys, kubeconfigs,
virtualenvs, caches and logs; the Hub target stages its own context; the
operator `.dockerignore` excludes docs/config; ACME copies only
`requirements.txt`, `pyproject.toml` and `src/`.

```bash
for repo in "$PATTERN" "$OPERATOR" "$HUB" "$MAS"; do
  printf '%s %s\n' "$repo" "$(git -C "$repo" rev-parse --short HEAD)"
  git -C "$repo" status --short
done > "$EVIDENCE/source-state.txt"

podman login quay.io
podman login registry.redhat.io       # operator builder image

# Operator (the sandbox image is NOT built here).
make -C "$OPERATOR" docker-build CONTAINER_TOOL=podman \
  IMG="$REGISTRY/lightspeed-agentic-operator:$TAG"
podman push --digestfile "$EVIDENCE/operator.digest" \
  "$REGISTRY/lightspeed-agentic-operator:$TAG"

# Hub A2A adapter. Vendors the sibling ../lightspeed-agentic-operator/api,
# so that checkout must be at the committed a2a-support-split state.
make -C "$HUB" image-a2a-adapter CONTAINER_TOOL=podman \
  A2A_ADAPTER_IMG="$REGISTRY/lightspeed-hub-a2a-adapter:$TAG"
podman push --digestfile "$EVIDENCE/adapter.digest" \
  "$REGISTRY/lightspeed-hub-a2a-adapter:$TAG"

# MAS: one image for API, worker and inbound A2A. Needs access to
# certs.corp.redhat.com (the Dockerfile downloads the IT root CA).
podman build -f "$MAS/Dockerfile" -t "$REGISTRY/mas-api:$TAG" "$MAS"
podman push --digestfile "$EVIDENCE/mas.digest" "$REGISTRY/mas-api:$TAG"

# ACME: build context is the agent directory.
podman build -f "$PATTERN/agents/acme_agent/Containerfile" \
  -t "$REGISTRY/acme-agent:$TAG" "$PATTERN/agents/acme_agent"
podman push --digestfile "$EVIDENCE/acme.digest" "$REGISTRY/acme-agent:$TAG"

cat "$EVIDENCE"/*.digest
```

Build for the cluster architecture (add `--platform linux/amd64` to the
`podman build` commands when building elsewhere). The repositories must be
pullable by the cluster. Never reuse `$TAG` for a different build: if you
rebuild, pick a new tag and change it in `phase-a.patch` (or set the charts'
`image.digest` / `adapter.image.digest` / `agenticOperator.image.digest` from
the digest files; ACME supports tags only).

**Sandbox image:** the operator runs Execution and Verification in
`agenticOperator.sandbox.image` (currently a floating `:main`). Pin a tested
sandbox build **by digest** before Phase D; it must support Execution and
Verification with the gateway kubeconfig and Result publication.

## 2. Phase A — images, close the development API Route

```bash
cd "$PATTERN"
git apply "$LAB/phase-a.patch"
git add variants/standalone/values-standalone.yaml
git commit -m "standalone: remediation lab images (phase A)"
git push
```

Then wait for ArgoCD (or `argocd app sync`/`wait` per app; see the runbook's
`sync_wait`). `mao-a2a` drains streams for up to ~21 minutes on rollout.

**Gate before any Keycloak grant (step 3b):** ACME must be running with token
debug and override **off**, otherwise anyone reaching its UI could copy a
token that will soon be able to approve and write.

```bash
oc -n mao get route                         # no mas-api route remains
oc -n mao get deploy -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
oc -n lightspeed-hub get deploy -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
oc -n acme-agent get deploy acme-agent -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
oc -n mao get pods; oc -n lightspeed-hub get pods; oc -n acme-agent get pods
oc -n acme-agent rollout status deployment/acme-agent --timeout=10m
oc -n acme-agent get deploy acme-agent -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -E '^(AUTH_TOKEN_DEBUG_ENABLED|ACME_ALLOW_TOKEN_OVERRIDE)='   # both =false
```

Tokens copied earlier through the debug view stay valid until they expire.
Wait at least **30 minutes** after this rollout (longer than ACME's Token A
and the 1800 s run token) before step 3b grants new roles to that principal.

If the `mas-api` Route stays after sync, it needs a prune of that app. Ask an
ordinary read-only question through ACME to confirm the baseline still works.

## 3. Phase B — Vault, live Keycloak realm, then values

Start only after the step 2 gate (token debug/override off, 30-minute wait).

The realm was imported once; the chart will **not** add clients, roles,
mappers or groups to it. Configure them through the Admin API first.

### 3a. Vault secret for the execution exchanger

Copy the `praxis-spoke-gateway-exec` entry from `values-secret.yaml.template`
into your existing `~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml`
(do not regenerate other entries), then:

```bash
cd "$PATTERN" && ./pattern.sh make load-secrets
```

### 3b. Admin API changes (inside the Keycloak pod)

```bash
oc -n keycloak-system get pods
oc -n keycloak-system exec -it keycloak-0 -- bash   # adjust the pod name
```

Inside the pod (kcadm prompts for the master-realm admin password; nothing is
written to shell history). If plain HTTP on 8080 is disabled, use the Admin
Console instead, performing the same operations.

```bash
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config /tmp/kcadm.config; }
kc config credentials --server http://localhost:8080 --realm master --user admin
R=rca
cid() { kc get clients -r "$R" -q clientId="$1" --fields id --format csv --noquotes; }

# 1. Execution exchanger client (secret is aligned later by the PostSync hook).
kc create clients -r "$R" -s clientId=praxis-spoke-gateway-exec \
  -s 'name=Praxis spoke execution-gateway token exchanger' -s enabled=true \
  -s publicClient=false -s standardFlowEnabled=false -s implicitFlowEnabled=false \
  -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=false \
  -s fullScopeAllowed=false -s 'attributes."standard.token.exchange.enabled"=true'
HUBC=$(cid lightspeed-hub-a2a); MARIO=$(cid praxis-spoke-mario-ipi)
SPOKEHUB=$(cid praxis-spoke-hub); EXEC=$(cid praxis-spoke-gateway-exec)
MASA=$(cid mas-a2a); ACME=$(cid acme-agent)

# 2. R_exec audience on the adapter's exchanger (keep the existing R mapper).
kc create "clients/$HUBC/protocol-mappers/models" -r "$R" \
  -s name=audience-praxis-spoke-gateway-exec -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.custom.audience"=praxis-spoke-gateway-exec' \
  -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=false'

# 3. Roles: approver on the hub audience, writer on mario-ipi ONLY.
kc create "clients/$HUBC/roles" -r "$R" -s name=agenticrun-approver
kc create "clients/$MARIO/roles" -r "$R" -s name=writer

# 4. Dedicated group; ACME's service account is its only member.
kc create groups -r "$R" -s name=acme-remediation-mario-ipi
kc add-roles -r "$R" --gname acme-remediation-mario-ipi \
  --cclientid lightspeed-hub-a2a --rolename agenticrun-approver
kc add-roles -r "$R" --gname acme-remediation-mario-ipi \
  --cclientid praxis-spoke-mario-ipi --rolename writer
GID=$(kc get groups -r "$R" -q exact=true -q search=acme-remediation-mario-ipi --fields id --format csv --noquotes)
SA=$(kc get "clients/$ACME/service-account-user" -r "$R" --fields id --format csv --noquotes)
kc update "users/$SA/groups/$GID" -r "$R" -s realm="$R" -s userId="$SA" -s groupId="$GID" -n

# 5. Execution exchanger: reader+writer scope for mario-ipi, reader for hub,
#    plus one client-role -> groups mapper per spoke audience.
printf '[%s,%s]' "$(kc get "clients/$MARIO/roles/reader" -r "$R")" \
  "$(kc get "clients/$MARIO/roles/writer" -r "$R")" \
  | kc create "clients/$EXEC/scope-mappings/clients/$MARIO" -r "$R" -f -
printf '[%s]' "$(kc get "clients/$SPOKEHUB/roles/reader" -r "$R")" \
  | kc create "clients/$EXEC/scope-mappings/clients/$SPOKEHUB" -r "$R" -f -
for aud in praxis-spoke-mario-ipi praxis-spoke-hub; do
  kc create "clients/$EXEC/protocol-mappers/models" -r "$R" \
    -s "name=$aud-roles" -s protocol=openid-connect \
    -s protocolMapper=oidc-usermodel-client-role-mapper \
    -s "config.\"usermodel.clientRoleMapping.clientId\"=$aud" \
    -s "config.\"usermodel.clientRoleMapping.rolePrefix\"=$aud:" \
    -s 'config."claim.name"=groups' -s 'config.multivalued=true' \
    -s 'config."jsonType.label"=String' -s 'config."access.token.claim"=true' \
    -s 'config."id.token.claim"=false' -s 'config."userinfo.token.claim"=false'
done

# 6. Let H carry the approver role (A -> H exchange by mas-a2a).
printf '[%s]' "$(kc get "clients/$HUBC/roles/agenticrun-approver" -r "$R")" \
  | kc create "clients/$MASA/scope-mappings/clients/$HUBC" -r "$R" -f -

# Read back before leaving the pod.
kc get "clients/$EXEC" -r "$R" --fields clientId,fullScopeAllowed,serviceAccountsEnabled,attributes
kc get "clients/$EXEC/scope-mappings/clients/$MARIO" -r "$R" --fields name
kc get "clients/$EXEC/scope-mappings/clients/$SPOKEHUB" -r "$R" --fields name
kc get "groups/$GID/role-mappings" -r "$R"
kc get "users/$SA/groups" -r "$R" --fields name
kc get "clients/$MASA/scope-mappings/clients/$HUBC" -r "$R" --fields name
# Read exchanger must still be reader-only:
kc get "clients/$(cid praxis-spoke-gateway)/scope-mappings/clients/$MARIO" -r "$R" --fields name
```

Expected: exec client confidential, no service account, `fullScopeAllowed`
false, token exchange on; mario-ipi scope `reader, writer`, hub scope `reader`;
the group has `agenticrun-approver` + mario-ipi `writer`; the ACME service
account is in `acme-agent-rca` and `acme-remediation-mario-ipi`; `mas-a2a`
scope has `agenticrun-creator, agenticrun-approver`; read exchanger `reader` only.

### 3c. Stop/go gate: role projection (no tokens handled)

Still inside the Keycloak pod (same `kc`, `R`, `SA` and client IDs), ask
Keycloak which claims it would issue to ACME's service account through each
exchanger. These are example claims, not usable tokens:

```bash
for c in praxis-spoke-gateway-exec praxis-spoke-gateway mas-a2a; do
  echo "== $c"
  kc get "clients/$(cid "$c")/evaluate-scopes/generate-example-access-token" \
    -r "$R" -q userId="$SA" --fields groups
done
rm -f /tmp/kcadm.config   # then leave the pod
```

Go only if: `praxis-spoke-gateway-exec` → `praxis-spoke-mario-ipi:reader`,
`praxis-spoke-mario-ipi:writer`, `praxis-spoke-hub:reader` (**no**
`praxis-spoke-hub:writer`); `praxis-spoke-gateway` → readers only;
`mas-a2a` → `lightspeed-hub-a2a:agenticrun-creator` and
`lightspeed-hub-a2a:agenticrun-approver`. Audience down-scoping of the real
exchanges (R vs R_exec) is checked in step 7 from the actual tokens.

### 3d. Push Phase B

```bash
cd "$PATTERN"
git apply "$LAB/phase-b.patch"
git add variants/standalone/values-standalone.yaml
git commit -m "standalone: remediation lab Keycloak execution exchange (phase B)"
git push
# after the keycloak-oidc and acme-agent apps sync:
oc -n keycloak-system wait externalsecret/praxis-spoke-gateway-exec-client --for=condition=Ready --timeout=5m
oc -n praxis-proxy wait externalsecret/praxis-spoke-gateway-exec-client --for=condition=Ready --timeout=5m
```

Check the keycloak-oidc PostSync hook succeeded (it aligns the new client's
secret with Vault; a missing client fails it). The live realm import is not
re-applied.

## 4. Phase C — execution gateways

```bash
cd "$PATTERN"
git apply "$LAB/phase-c.patch"
git add variants/standalone/values-standalone.yaml
git commit -m "standalone: remediation lab execution gateways (phase C)"
git push
# after praxis-proxy syncs:
oc -n praxis-proxy rollout status deployment/praxis-spoke-mario-ipi-exec --timeout=10m
oc -n praxis-proxy rollout status deployment/praxis-spoke-hub-exec --timeout=10m
oc -n praxis-proxy get deploy,svc,networkpolicy | grep -- '-exec'
oc -n praxis-proxy logs deploy/praxis-spoke-mario-ipi-exec --tail=50
```

`praxis-spoke-hub-exec` is also rendered (the chart enables execution for every
registered spoke) but grants nothing: the hub has no writer binding and the
live realm maps no `writer` on `praxis-spoke-hub`.

## 5. Phase D — Hub execution path

First set the tested sandbox digest in `agenticOperator.sandbox.image`.

```bash
cd "$PATTERN"
git apply "$LAB/phase-d.patch"
"$EDITOR" variants/standalone/values-standalone.yaml   # sandbox image @sha256:...
git add variants/standalone/values-standalone.yaml
git commit -m "standalone: remediation lab hub execution path (phase D)"
git push
# after lightspeed-hub syncs:
oc -n lightspeed-hub rollout status deployment/lightspeed-hub --timeout=10m
oc -n lightspeed-hub rollout status deployment/lightspeed-hub-agentic-operator --timeout=10m
oc get approvalpolicy cluster -o yaml        # Analysis Automatic, Execution MANUAL, Verification Automatic
oc -n lightspeed-hub get configmap lightspeed-agentic-configuration -o yaml | grep -i gateway
oc -n lightspeed-hub get role,rolebinding | grep approver
oc -n lightspeed-hub get deploy lightspeed-hub -o yaml | grep -A1 -- '--execution-token-audience'
```

**Stop if Execution is not Manual.**

## 6. MAS blueprint

The pinned blueprint is `bdf07d00-a104-47b1-99fb-47af1a7a8e04`. Reuse it only
if its persisted Hub node points at Praxis `/a2a`, uses header `X-Hub-Cluster`,
lists `hub, mario-ipi`, has **`hitl_mode: ask`** and no static bearer token.
The API Route is closed now, so use a port-forward:

```bash
oc -n mao port-forward --address 127.0.0.1 svc/mas-api 8002:8002   # separate terminal
export MAS_API_URL=http://127.0.0.1:8002 MAS_TOKEN=dev-token
export HUB_ADAPTER_A2A_URL="https://$(oc -n praxis-proxy get route praxis-proxy -o jsonpath='{.spec.host}')/a2a"
export HUB_ADAPTER_TARGET_HEADER=X-Hub-Cluster HUB_ADAPTER_CLUSTERS=hub,mario-ipi HUB_ADAPTER_HITL_MODE=ask
cd "$PATTERN"
./setup-mas-blueprint.sh --dry-run multi-cluster    # inspect; saves nothing
```

If the persisted blueprint differs, run `./setup-mas-blueprint.sh multi-cluster`
(with your usual `LLM_PROVIDER`/Vertex inputs; it also updates named inventory
resources), then set `a2a.blueprintId` in the `mao-a2a` overrides to the new ID
and commit/push that one change (Phase E). ACME's principal is already in
`a2a.allowedPrincipals`.

## 7. Stop/go gate: real token separation, still without any writer binding

Run one approval **before** step 8, so a mistake cannot write anything yet.

```bash
export MOC="oc --kubeconfig $MARIO_KUBECONFIG"
$MOC get clusterrolebinding,rolebinding -A -o wide | grep 'praxis-spoke-mario-ipi:writer' \
  && echo 'STOP: a writer binding already exists'
# The token-gate observer is $LAB/capture_exec_claims.py (stdlib only): it
# decodes selected claims of token-a and token-execution from the SAME
# Secret read, never prints a token, and fails on timeout, finished
# Execution or a missing Secret.
```

Ask ACME a mario-ipi/payments question (step 9 prompt). When it shows the
options, identify the new run and start the observer **before** approving:

```bash
RUN=$(oc -n lightspeed-hub get agenticruns --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
UID_=$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')
oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.spec.targetCluster}{"\n"}'   # mario-ipi
SECRET="ls-run-token-$UID_"
python3 "$LAB/capture_exec_claims.py" "$SECRET" "$RUN" 900 \
  > "$EVIDENCE/token-claims.json" 2> "$EVIDENCE/token-claims.err" &
OBSERVER=$!
```

Now approve one option in ACME, then:

```bash
wait "$OBSERVER" && cat "$EVIDENCE/token-claims.json" || cat "$EVIDENCE/token-claims.err"
oc -n praxis-proxy logs deploy/praxis-spoke-mario-ipi-exec --since=15m | tail -50
oc -n praxis-proxy logs deploy/praxis-spoke-mario-ipi --since=15m | tail -50
```

If the observer fails, the gate fails: do not proceed without evidence; repeat
with a fresh question.

Go only if: `R` (`token-a`) has `aud` exactly `praxis-spoke-gateway`;
`R_exec` (`token-execution`) has `aud` exactly `praxis-spoke-gateway-exec`; both
have ACME's `sub`; the Execution step reached `praxis-spoke-mario-ipi-exec`
(and the read gateway shows no R_exec); and Execution's writes on mario-ipi
were **denied** (403), because no writer binding exists yet. That run then
fails or escalates: expected. Confirm `payments` is unchanged.

## 8. Prepare `mario-ipi` (writer binding in `payments` only)

```bash
$MOC whoami --show-server        # must be https://api.mario-ipi...:6443
$MOC get authentication cluster -o jsonpath='{.spec.oidcProviders[*].issuer.audiences}{"\n"}'
$MOC get clusterrolebinding,rolebinding -A -o wide | grep 'keycloak:praxis-spoke-mario-ipi'
$MOC get ns payments
```

The reader binding for `keycloak:praxis-spoke-mario-ipi:reader` must already
exist (analysis on mario-ipi depends on it). Save the current `payments` state,
then apply the reviewed writer RBAC (rules rendered from the lightspeed-hub
chart; `tests/chart/test_remediation_execution.py` keeps them in sync):

```bash
$MOC -n payments get deploy,sts,svc,cm,pods -o yaml > "$EVIDENCE/payments-before.yaml"
$MOC apply -f "$LAB/mario-ipi-writer-rbac.yaml"
# Negative/positive authorization checks via impersonation:
$MOC auth can-i patch deployments -n payments --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer    # yes
$MOC auth can-i patch deployments -n default  --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer    # no
$MOC auth can-i get secrets -n payments       --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer    # no
$MOC auth can-i patch deployments -n payments --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:reader    # no
oc auth can-i patch deployments -n payments   --as=lab --as-group=keycloak:praxis-spoke-hub:writer          # no (hub)
```

`payments` must not contain privileged ServiceAccounts or Secrets you would not
let the remediation agent use: writers can run Pods there.

## 9. Tests through ACME

Terminals to keep open:

```bash
bash scripts/monitor.sh --join --since 10m
oc -n lightspeed-hub get agenticruns -w
$MOC -n payments get deploy,pods -w
oc -n praxis-proxy logs -f deploy/praxis-spoke-mario-ipi-exec
```

Make sure the payments demo is in its failing state (the
`PaymentErrorRateHigh` scenario). Open the ACME UI and ask, for example:

> On cluster mario-ipi, investigate the PaymentErrorRateHigh alert in namespace
> payments. Propose remediation options limited to namespace payments, do not
> change anything until I approve, and verify the result after execution.

**9a. Rejection first.** When ACME shows the options, reject. Expected: the
AgenticRunApproval has Execution `Denied`; no Execution sandbox; the run's
token Secret never gains `token-execution`; `payments` unchanged; ACME reports
a completed analysis with the rejection, not a successful remediation.

```bash
RUN=$(oc -n lightspeed-hub get agenticruns --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
oc -n lightspeed-hub get agenticrunapproval "$RUN" -o yaml
UID_=$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')
oc -n lightspeed-hub get secret "ls-run-token-$UID_" \
  -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}{{"\n"}}'   # key names only
```

**9b. Approval on a fresh question.** Ask again (new task). Capture the
**new** run before approving, then review the full options and select one in
the UI (or type `approve option N`, one-based):

```bash
RUN=$(oc -n lightspeed-hub get agenticruns --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
UID_=$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')
echo "$RUN $UID_"   # must differ from the rejected run in 9a
oc -n lightspeed-hub get secret "ls-run-token-$UID_" -w \
  -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}{{"\n"}}'   # key names only
```

Expected, in order: `token-execution` appears only after approval; the
Execution pod uses the `-exec` gateway (its logs show U_exec exchanges for
`praxis-spoke-mario-ipi`); changes happen only in `payments`; `token-execution`
is removed after Execution; Verification runs automatically through the read
gateway; ACME shows the final verified result on the same conversation.

```bash
oc -n lightspeed-hub get agenticrun "$RUN" -o yaml
oc -n lightspeed-hub get analysisresults,executionresults,verificationresults
$MOC -n payments get deploy,pods
$MOC -n payments get deploy,sts,svc,cm,pods -o yaml > "$EVIDENCE/payments-after.yaml"
```

Success means a healthy workload **and** a successful VerificationResult, not
just a fluent summary. Also try an invalid choice (`approve option 9`): ACME
must stay on the same approval with an error, and a corrected choice must work.

## 10. Cleanup and rollback

```bash
$MOC delete -f "$LAB/mario-ipi-writer-rbac.yaml"
# Keycloak (inside the pod, kc/R/SA/GID as in 3b): drop the shared grant
#   kc delete "users/$SA/groups/$GID" -r "$R"
cd "$PATTERN"
git log --oneline -6                 # find the phase D, C and B commits
git revert --no-edit PHASE_D_SHA PHASE_C_SHA PHASE_B_SHA
git push
```

Reverting disables the Hub path first (Phase D), then the gateways, then the
Keycloak values; the Keycloak objects created in 3b stay harmless once the
group membership is removed. Re-enable ACME token debug only if still needed.
