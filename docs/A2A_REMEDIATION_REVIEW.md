# A2A remediation: cross-repository review

Review snapshot: **2026-11-25**, including tracked diffs and new source/test files.
This is a source and local-test review, **not live deployment certification**.
No production-code fixes were included in that historical review pass. The
separately authorized remediation pass is tracked below; the findings retain
their original reproduction evidence and source-line references.

**Historical review verdict: Request changes before enabling remediation.** In particular,
fix unsafe approval interpretation/ordering (H1/H2, M1/M2), resumability
(M3, O1), and premature completion (A1). Passing unit/render/race suites do not
cover the reproduced transitions below. Keep the standalone write-path switches
disabled.

## Remediation follow-up — 2026-11-26

The user authorized all 15 fixes, regression tests, and an independent review.
This follow-up changes source/tests only, plus their documentation: no images,
cluster configuration, credentials, execution enablement, or commits. Local
verification is **not** live deployment certification.

ACME A1–A3/M4-consumer and chart G1 changes are implemented and independently
**approved**: exact-pinned ADK 2.10.0 / A2A SDK 1.2.1 ACME suite **49 passed**
on Python 3.12 (the image's Python minor) and 3.14,
chart suite **93 passed**. Review caught an unsupported auth-pause recovery;
recovery now accepts only confirmed `input-required` or terminal states, with
a regression proving explicit failure and no decision replay otherwise.
An actual MAS SDK 1.1.5 task projection also passes through the pinned ACME
stack, including invalid-decision recovery, unchanged task/context IDs, and
browser option extraction before and after correction. Operator O1–O2 changes
are independently approved; the post-resume path was checked against subsequent
handler retries. MAS M1–M4 changes are now independently **approved**, including
the final malformed-selection rejection and generic-tool compatibility checks.
**All 15 findings are fixed and independently approved across the four
repositories.** Final Hub review verified the expired-token and
operator-condition-removal corrections, found no further production
correctness or deadlock issues, and approved the user-authorized test-only
correction for one scheduling-dependent regression test (below).

### Finding-to-fix and regression map

Paths are relative to the repository named in the original finding (Hub server
paths below are under `a2a-adapter/`). The historical findings and line
references below remain unchanged; this table tracks the remediation delta.
Source approval is not live deployment certification.

| Finding | Source correction and representative checked-in regression | Source review |
|---|---|---|
| H1 | Hub `internal/server/handler.go`, `watch.go`, shared `runlock.go`: reload current binding/terminality under the run lock before writes, including expired-token follow-ups and lazy recovery; serialize deadline transitions. `TestAdapterTerminalRecordReloadPreventsStaleExecutionApprovalReplay`, `TestSweepDeadlineSerializesWithApprovalRenewal`, `TestExpiredCredentialFollowUpPreservesSweepTerminalState`. | Independently approved |
| H2 | Hub `handler.go`: reject mixed approve/reject/revise before side effects. `TestMixedFollowUpCommandsAreRejectedWithoutWrites`. | Independently approved |
| H3 | Hub `handler.go`, `watch.go`: clear renewal overlay without stale watcher overwrite; clear CR-derived pauses when the operator condition disappears without clearing adapter terminality. `TestExecutionApprovalRenewalClearsAdapterPauseOverlay`, `TestRunChangedLifecycleLockPreventsStalePauseOverlay`, `TestOperatorClearedCredentialConditionRemovesPauseOverlay`. | Independently approved |
| H4 | Hub `analysis_identity.go`, `handler.go`: present omitted decision is effectively Approved and immutable, never credential adoption. `TestOmittedApprovalStagesAreImmutableAndNeverAdopted`, `TestOmittedExecutionObjectMatchesOptionlessReplayWithoutMutation` cover nil/default option versus explicit zero. | Independently approved |
| H5 | Hub `watch.go`, `execution_reconcile.go`: reread under lock; revalidate UID/task/generation before cleanup; full sweep retries skipped hints. `TestStaleTerminalEventDoesNotRemoveCurrentExecutionCredential`, `TestTerminalCleanupRevalidatesReopenedGenerationBeforeDelete`, both `TestSweepRetries*EventSkippedUnderRunLock` tests. | Independently approved |
| O1 | Operator `controller/agenticrun/handlers.go`: refresh optimistic-lock base after resume. `TestHandleVerification_CredentialPauseResumeRelaunch` exercises the real handler with a Verify stub: one successful resumed invocation and a fresh-read retry with two total calls (including the initial credential-wait attempt), not a live sandbox launch. | Independently approved |
| O2 | Operator `credential_pause.go`, `gateway.go`: record token-key/endpoint selector identity; validate a newly selected credential even at the same Secret RV. `TestResumeFromCredentialPause_SelectorChangeUsesNewCredentialAtSameRV`, `TestCredentialPauseMessage_RoundTrip`. | Independently approved |
| M1 | MAS `lib/mas/a2a/approval.py`, outbound node: nonempty MODIFY feedback is revision text; reject feedback plus explicit selection. `test_modify_feedback_mentions_never_infer_an_approval_option`, `test_modify_feedback_and_structured_option_are_ambiguous`. | Independently approved |
| M2 | MAS shared validator: exact finite integral nonboolean structured indexes and complete one-based chat syntax; without a structured option, only empty approval feedback omits selection and all other unrecognized text rejects. `test_option_mapping_rejects_fractional_non_finite_and_boolean_values`, `test_text_option_parser_requires_a_complete_unsigned_selection`, real inbound wrapped/Unicode same-gate correction tests. | Independently approved |
| M3 | MAS inbound orchestrator validates before mailbox submission; alternate-channel invalid decisions re-open a bounded durable checkpoint. `test_approval_data_part_and_invalid_then_corrected_same_task_gate`, `test_invalid_decision_from_durable_operator_channel_reopens_a_gate`. Generic tool MODIFY keeps arbitrary arguments via persisted backward-compatible `approval_type`. | Independently approved |
| M4 | MAS persists sanitized full `approvalRequest`, projects TextPart + DataPart; ACME preserves it in the approval call/UI. `test_structured_approval_options_sanitize_and_truncate_summaries_explicitly`, real HTTP durable-contract test, ACME `test_actual_mas_115_projection_survives_pinned_acme_and_correction`. | Independently approved in MAS and ACME |
| A1 | ACME `approval.py`: separate active binding from pending gate; nonterminal/empty EOF fails explicitly unless one bounded read confirms an open gate or terminal result. `test_nonterminal_or_empty_eof_is_not_success`, `test_working_eof_after_approval_keeps_active_binding_but_fails_attachment`. | Independently approved |
| A2 | ACME one read-only same-task/context recovery query; re-expose only a confirmed open gate, otherwise explicit failure. `test_rpc_validation_error_recovers_only_a_confirmed_open_gate`, captured MAS fixture/correction test. | Independently approved |
| A3 | ACME maps protocol rejected/failed/canceled to unsuccessful outer outcomes and retains explanations. `test_contentless_terminal_status_clears_pending_pause`. | Independently approved |
| G1 | Praxis chart validates generated gateway identity uniqueness while preserving reader names. `tests/chart/test_remediation_execution.py::test_praxis_gateway_resource_names_are_unique` rejects cross-mode collisions. | Independently approved |

### Resolved: nondeterministic Hub concurrency regression

**Medium — test reliability, not an additional production defect.**
`lightspeed-hub/a2a-adapter/internal/server/watch_test.go:1366–1393` leaves the
harness's default **10 ms** automatic sweep enabled, advances the test clock
beyond the renewal deadline, then starts renewal. A background sweep can
legitimately terminalize first. The correctly guarded renewal does not enter
token exchange, and the test fails waiting for its barrier.

The final reviewer observed this in a full `-race` run. With user approval,
the test now sets `SweepInterval = time.Hour` through its Config callback
**before harness startup** (the watcher ticker is created with that interval),
leaving its explicit barrier-controlled sweep intact. No production code
changed. After the fix, the focused test passed **200** `-race` repetitions and
the full server package **10** `-race` repetitions, for both the lead and the
independent reviewer, who approved the Hub delta.

Local evidence: `/tmp/opencode/remediation-independent-review/` contains
`hub-round2-race.log` (failure), `hub-round2-full-repeat.log`, and
`hub-round2-adversarial-lazy.log` (production interleaving/lazy-recovery probes).

### Remediation validation checkpoint

- ACME: **49 passed** independently and by the lead, with exact ADK **2.10.0** /
  A2A SDK **1.2.1**; lead also reran on Python **3.12.7** and **3.14**.
- Pattern local suites: **184 passed** (`tests/chart tests/scripts tests/mao`),
  comprising **93 chart** tests and **91 script/MAS-chart** tests.
- Hub: `make test-a2a-adapter` (including vet) passed independently and for the
  worker/lead; server coverage **78.4%**. After the test-ordering fix above,
  repeated server race runs passed. All adapted production adversarial probes,
  including expired-token follow-up, operator-condition removal, and lazy
  recovery versus sweep, passed under race.
- Operator: vet and changed-package tests passed independently and for the lead;
  the focused real Verification pause/resume/post-launch-retry test also passed.
  Lead Make-based `-race -count=1` tests passed for `controller/agenticrun` and
  `pkg/configuration` on an extended rerun after an initial 120-second timeout.
  Full `make test` remains blocked by `cmd/check-isa-level` fixture builds
  failing with `error obtaining VCS status: exit status 128`. Its helper clears
  `GOFLAGS`, so the attempted `-buildvcs=false` override did not resolve it.
- MAS expanded A2A suite after the final malformed-input correction: **139
  passed, 20 subtests passed**, independently and by the lead. The five
  wrapped/Unicode probes now have checked-in same-gate and parser-to-node
  coverage. Durable store tests use production
  `MongoApprovalStore` with **mongomock**, not live MongoDB.
  Independent probes also checked 33 malformed/unknown replies, 10 typed/legacy
  generic-tool modifications, and 27 malformed identified/legacy remote schemas.
- Current MAS SDK **1.1.5** HTTP `GetTask` was regenerated after the final review
  corrections and matches the saved ACME fixture apart from generated IDs and
  timestamp. The independently approved ACME test exercises actual HTTP/ADK
  conversion, invalid-decision recovery, same-task correction, and actual Node
  UI extraction on the original and recovered option payloads.
- Runbook: **23 Bash blocks** passed syntax checking and local file links were
  checked. No live acceptance, image build, credential exchange, or rollout is
  claimed.

### Deliberately retained limitations

- ACME reports interrupted nonterminal attachments explicitly; it does **not**
  implement automatic reconnection or worker-failover recovery. Its approval
  bindings remain process-local; MAS outbound task bindings are not a durable
  exactly-once checkpoint.
- Hub per-run locks are process-local coordination for the supported
  single-replica adapter, not distributed fencing across multiple adapters.
- Alternate-channel invalid MAS decisions may consume their original mailbox
  gate before node validation. The node opens a new durable gate up to three
  retries, then fails locally; the remote Hub task remains paused.
- The approval projection cap is **10 options**, matching the AnalysisResult
  CRD. All identities are preserved or projection fails; shortened summaries
  carry an explicit truncation marker. Structured indexes are zero-based;
  displayed/plain-chat numbers are one-based.
- Existing CLI/external omitted approvals remain effective and immutable, but
  replaying them does not bind analysis or mint/adopt execution credentials.
- Shared-principal lab authorization, management-API hardening, image pins,
  Keycloak migration, gateway switches, and target writer RBAC are later rollout
  work. Automatic Verification is configured, but rejection and
  `NoActionRequired` still intentionally skip execution/verification.

### Image-build handoff

Combined source sign-off is complete. Build the operator, Hub A2A adapter, MAS, and
ACME from these tested working trees, including their new untracked source
files. The adapter build uses the sibling operator API checkout. Use the same
MAS image for API, worker, and inbound A2A; also provide a compatible tested
sandbox image reference (the operator build does not build the sandbox).
[Runbook section 2](A2A_REMEDIATION_E2E.md#2-build-and-push-the-four-application-images)
documents build contexts and digest pinning. Image construction does not
authorize execution enablement; identity/gateway/RBAC changes and positive and
negative live tests remain separate gates. Inspect/sanitize build contexts first:
MAS currently copies its whole context and has no repository `.dockerignore`;
local credential files must not be sent into the image build.

The selected later lab uses the existing ACME credentials as a **shared
principal**, with writer authorization restricted to `payments` on `mario-ipi`.
This does not require new per-user login, but does require protected chat/
approval endpoints and explicit approver/writer grants. No such grants are
made by this code pass. See [the runbook](A2A_REMEDIATION_E2E.md) for the lab
boundary versus the alternative personally attributed human-token tests.

The review records **15 reproduced findings: 7 High and 8 Medium**. O1 is
pre-existing; it is included because the modified verification flow depends on
that path. The remaining findings concern the new integration or its interactions.

| Repository | Branch | HEAD underneath the uncommitted changes |
|---|---|---|
| `multicloud-gitops` | `rca-mao-praxis-hub` | `f3ddc643` |
| `lightspeed-hub` | `a2a-split` | `4bcefd0` |
| `lightspeed-agentic-operator` | `a2a-support-split` | `2d5b92a` |
| `uie-mas-hosted` | `a2a-outbound-hitl` | `38d9c8c` (the requested pr7 base) |

Scope includes the adapter, operator credential changes, MAS outbound HITL,
ACME, the charts, and their integration contracts. Pre-existing unrelated
working-tree files were not changed. Paths below are repository-relative;
line numbers refer to this snapshot, not an upstream release.

Companion: [MAS → AgenticRun → approval walkthrough](a2a-api-watch-map.html)
(offline HTML; one investigation followed through approval/rejection and the
returned A2A events, with current implementation caveats).

Deployment/test procedure: [Remediation end-to-end lab runbook](A2A_REMEDIATION_E2E.md)
(image builds, phased GitOps rollout, existing-realm migration, human-principal
Hub/MAS tests, and the remaining ACME authentication gate; not live certification).

## Historical verified findings

<a id="h1"></a>

### H1 · High · Adapter-terminal tasks can still refresh execution credentials

**Repository:** `lightspeed-hub`\
**Source:** `a2a-adapter/internal/server/handler.go:1145-1180,1268-1289`\
**Origin:** new approval-renewal ordering. **Evidence:** disposable regression
using the adapter's existing fake-client harness.

An approved execution can outlive the adapter's renewal timeout, leaving the
task record terminal while the operator run remains credential-paused. Replaying
the approval with fresh credentials invokes execution-token renewal **before**
the adapter-terminal guard. The response still says `failed`, but the changed
Secret can allow the operator to resume execution.

**Improvement:** enforce terminal credential restrictions before approval
preparation, and refresh the task record after acquiring the run lock. Allow
idempotent terminal responses without reminting credentials; handle any supported
revision reopening explicitly. Test approval replay after timeout and pause-cap
failure, asserting no Secret writes.

<a id="h2"></a>

### H2 · High · Invalid combined approval/revision commands can authorize execution

**Repository:** `lightspeed-hub`\
**Source:** `a2a-adapter/internal/server/handler.go:1150-1174`\
**Origin:** new command ordering. **Evidence:** disposable regression.

A message containing both `{"approve":{"stage":"Execution","option":0}}`
and `{"revise":"Investigate further instead"}` commits the approval and writes
execution credentials first. It then rejects the revision because Execution has
already been decided. Thus an `InvalidParams` response can accompany a successful
authorization to execute the old plan.

**Improvement:** parse and validate the entire command before writes. Reject
mutually exclusive approval/rejection/revision commands up front. Test all mixed
command forms and assert **no approval, annotation, or Secret mutations** on an
invalid request.

<a id="o1"></a>

### O1 · High · Verification can stall permanently after credential renewal

**Repository:** `lightspeed-agentic-operator`\
**Sources:** `controller/agenticrun/handlers.go:298,320-345`,
`controller/agenticrun/credential_pause.go:93-96`\
**Origin:** pre-existing in HEAD, but on the verification-resume path this
feature now relies on. **Evidence:** reproduced with the real handler and a
fake Kubernetes client.

The verification handler captures its optimistic-lock patch base before
`resumeFromCredentialPause()` patches the run. Resume advances the run's
resourceVersion; the subsequent status patch uses the stale base and conflicts.
The persisted `Verified=Unknown` condition remains, the credential-pause
condition is gone, and no verification sandbox has been launched. On retry,
the handler interprets that condition as an already-running verification and
returns. There is no sandbox for timeout handling to recover.

**Improvement:** refresh the patch base after successful resume, before the
verification-in-progress patch. Add a handler-level pause → renewal → launch
test asserting exactly one sandbox launch across retries.

<a id="m1"></a>

### M1 · High · Revision prose mentioning an option becomes execution approval

**Repository:** `uie-mas-hosted`\
**Source:** `lib/mas/elements/nodes/a2a_agent/a2a_agent_node.py:634-655`\
**Evidence:** reproduced with the response mapper.

The node searches `MODIFY` feedback for an option number before deciding whether
it is a revision. `Do not use option 2; produce a safer plan` becomes
`{"approve":{"stage":"Execution","option":1}}`. Likewise, a request to change
option 2's namespace approves the unchanged option and discards the modification.

**Improvement:** treat nonempty `MODIFY` feedback as revision, never infer
execution authorization from prose requesting changes. If structured
`modifiedArgs.option` intentionally selects approval, define that separately.
Test negative references to an option and requests to modify a selected option.

<a id="m2"></a>

### M2 · High · Malformed explicit selections silently approve another/default option

**Repository:** `uie-mas-hosted`\
**Source:** `lib/mas/elements/nodes/a2a_agent/a2a_agent_node.py:602-609`\
**Evidence:** reproduced with the response mapper.

The text parser cannot distinguish no supplied option from an invalid explicit
selection. Approval feedback `option -1` or `option two` sends approval without
an option, selecting the default; `option 1.5` selects index `0`. The structured
numeric path correctly rejects fractional and boolean values, but the prose
fallback bypasses that protection.

**Improvement:** use a strict selection grammar and consume the complete numeric
token. Reject malformed explicit selections while preserving a real paused
checkpoint. Test signed, fractional, nonnumeric, and out-of-range choices.

<a id="m3"></a>

### M3 · High · Invalid choices consume the durable gate and cannot be corrected

**Repository:** `uie-mas-hosted`\
**Source:** `lib/mas/elements/nodes/a2a_agent/a2a_agent_node.py:363-366,469-472`\
**Evidence:** reproduced with the real approval gate and mailbox.

An out-of-range selection such as `option 8` is validated only after the gate
consumes the decision. The delegation exits with `input_required` metadata, but
no pending durable gate remains. The remote hub task is still paused; a corrected
decision on the same MAS task instead raises `A2ATaskIdConflictError`.

**Improvement:** re-prompt inside the delegation with a new durable gate and
bounded validation attempts/deadline, or preserve an actual resumable checkpoint.
Test an invalid decision followed by a successful corrected decision on the same
MAS task, not merely the returned error metadata.

<a id="a1"></a>

### A1 · High · A nonterminal MAS stream ending becomes successful ACME completion

**Repository:** `multicloud-gitops` / ACME\
**Sources:** `agents/acme_agent/src/acme_agent/approval.py:143-151,192-198`;
MAS `adapters/inbound/a2a/executor.py:196-202`\
**Evidence:** reproduced through real ADK / `to_a2a` with the container's pinned
dependency versions.

When MAS emits `working` and then closes its attachment, ACME clears the pending
approval binding and the stock ADK executor completes the outer task. MAS
deliberately permits this at its attachment time limit while execution continues.
ACME can report success before completion and lose access to a later approval.

**Improvement:** track active downstream bindings separately from pending human
approvals. On nonterminal EOF, poll/resubscribe to the same MAS task, or report an
interrupted attachment explicitly rather than success. Test `working → EOF →
later input-required/completed` with the real converter/executor.

<a id="h3"></a>

### H3 · Medium · Approval-based renewal leaves the task auth-required

**Repository:** `lightspeed-hub`\
**Sources:** `a2a-adapter/internal/server/handler.go:1199-1205,1236`,
`a2a-adapter/internal/server/watch.go:124-151`\
**Origin:** new execution-approval early return. **Evidence:** disposable regression.

An identical Execution approval can successfully refresh both credentials while
the task is auth-paused, but this path returns before clearing `rec.AuthPaused`.
Even when the operator removes `CredentialsExpired`, that overlay remains; the
watcher clears it on terminal runs, not ordinary resumed runs. The adapter can
eventually fail a run that has actually resumed.

**Improvement:** share successful-renewal bookkeeping across every credential
write path. Test the immediate response and the subsequent operator-condition
update; both must leave the task out of `auth-required`.

<a id="h4"></a>

### H4 · Medium · Adapter and operator disagree about an omitted approval decision

**Repository:** `lightspeed-hub`, integrating with operator/CLI\
**Sources:** `a2a-adapter/internal/server/analysis_identity.go:46-53`,
`a2a-adapter/internal/server/handler.go:1268-1313`; operator
`controller/agenticrun/approval.go:113-119`, `cli/run/approve.go:144`\
**Origin:** new predicates and revision guard. **Evidence:** disposable adapter
regression plus operator and CLI source trace.

The operator regards a stage whose decision is omitted as approved; the CLI
uses that representation. The adapter recognizes only explicit `Approved` or
`Denied`. A CLI-shaped Execution approval is consequently treated as undecided,
so the adapter can accept a revision after an operator-recognized approval or
try to overwrite its selected option. Under the legacy credential mode, the
retained approval can then authorize the revised plan without a new gate.

**Improvement:** normalize stage semantics consistently with the API, including
the omitted-decision case. Keep the approved-analysis identity requirement for
scoped credentials. An external approval lacking that binding needs an explicit
safe adoption path, not classification as a pending decision. Test API-, CLI-,
and adapter-created approval representations.

<a id="h5"></a>

### H5 · Medium · A stale terminal event can delete current execution credentials

**Repository:** `lightspeed-hub`\
**Source:** `a2a-adapter/internal/server/watch.go:94-115,317-337`\
**Origin:** new immediate execution-key cleanup; terminal scheduling is also
affected. **Evidence:** delayed-event injection, not live watch scheduling.

The watcher bases destructive cleanup on the supplied informer snapshot rather
than rereading the run under its lock. An old Completed/no-action event delivered
after a valid revision and new approval removes the new execution key and can
also schedule deletion of the whole Secret. The periodic reconciler cannot
restore a token without a fresh caller credential.

**Improvement:** treat events as reconciliation triggers. Reread the current
UID/state under the run lock before removing credentials or scheduling/deleting
the Secret. Consolidate this with durable cleanup rather than maintaining two
different authorization checks. Test old-terminal-event delivery after revision
and approval, including the delayed whole-Secret cleanup.

<a id="o2"></a>

### O2 · Medium · Changing the execution credential selector cannot clear a pause

**Repository:** `lightspeed-agentic-operator`\
**Sources:** `controller/agenticrun/credential_pause.go:80-90`,
`controller/agenticrun/gateway.go:197-205`\
**Origin:** new interaction with the optional execution template.
**Evidence:** reproduced with a fake Kubernetes client.

If execution pauses because `token-execution` is absent, removing the execution
gateway template makes the existing `token-a` the correct credential. Validation
then succeeds, but resume still requires a different Secret resourceVersion.
Configuration-triggered reconciliation cannot resume the run unless something
also rewrites the otherwise valid Secret.

**Improvement:** persist the selected credential key/endpoint identity with the
pause. If the selector changes, validate the newly selected credential rather
than requiring a change to the old Secret version. Test configuration hot reload
during a credential pause.

<a id="g1"></a>

### G1 · Medium · Read and execution gateway names can collide across spokes

**Repository:** `multicloud-gitops`\
**Sources:** `charts/all/praxis-proxy/templates/_helpers.tpl:68-75`,
`charts/all/praxis-proxy/templates/spoke-gateway-configmap.yaml:12-18`\
**Origin:** new execution-name suffix. **Evidence:** targeted Helm render.

With execution enabled, spokes named `east` and `east-exec` both generate
`praxis-spoke-east-exec`: the execution gateway for the former and the reader
gateway for the latter. Helm exits successfully and emits duplicate identities
for the ConfigMap, Deployment, Service, and NetworkPolicy. Deployment tools
cannot reliably install both target/mode combinations.

**Improvement:** retain existing reader names for upgrade compatibility, but
validate uniqueness of every generated `(kind, namespace, name)` across modes.
Fail rendering on this collision and add a regression test. A future naming
migration could reserve distinct prefixes for execution resources.

<a id="m4"></a>

### M4 · Medium · The actual MAS approval projection loses and truncates options

**Repositories:** `uie-mas-hosted` and ACME UI in `multicloud-gitops`\
**Sources:** MAS `lib/mas/elements/nodes/a2a_agent/a2a_agent_node.py:559-577`,
`adapters/outbound/hitl/channel_gate.py:134-148`,
`adapters/inbound/a2a/task_mapping.py:79-100`;
`agents/acme_agent/src/acme_agent/ui.py:302-324`\
**Evidence:** reproduced through durable gate persistence and A2A task projection.

The outbound node constructs structured options, but the durable gate keeps only
a 300-character JSON preview, and inbound A2A exposes a TextPart rather than the
structured payload. ACME's picker expects an object and is therefore absent in
the real path. A three-option reproduction truncated in option 1; options 2 and
3 were not visible.

**Improvement:** persist a bounded, sanitized approval schema with explicit option
indexes, titles, and summaries; project it into ACME's approval-call payload.
Keep readable text as a fallback, not the only transport. Test realistic long
options through the mailbox, inbound projection, and UI extraction together.

<a id="a2"></a>

### A2 · Medium · Invalid-decision RPC errors erase a valid approval and appear successful

**Repository:** `multicloud-gitops` / ACME\
**Sources:** `agents/acme_agent/src/acme_agent/approval.py:231-263`;
MAS `adapters/inbound/a2a/orchestrator.py:523-535`\
**Evidence:** reproduced with an HTTP-200 JSON-RPC `-32602` response.

ACME deletes the matching pending binding on an RPC error. The inherited error
event also lacks the error code needed by the outer A2A converter, so the outer
task completes successfully. MAS rejects unparseable decisions before mailbox
submission: its gate remains pending, but ACME has removed the correction route.

**Improvement:** normalize RPC errors into explicit outer failures. For validation
errors, query the bound MAS task and re-expose input-required if its gate remains
open. Do not infer downstream terminality from transport/RPC errors. Test invalid
input followed by successful correction.

<a id="a3"></a>

### A3 · Medium · Downstream rejected tasks become completed in ACME

**Repository:** `multicloud-gitops` / ACME\
**Source:** `agents/acme_agent/src/acme_agent/approval.py:115-121`\
**Evidence:** reproduced with a contentless downstream `rejected` state.

Only failed/canceled states receive explicit error mapping. A rejected task
clears the pending binding and becomes outer completed. This is distinct from
the hub's intentional completed-analysis response when a person rejects Execution.

**Improvement:** explicitly map protocol-level `rejected` to an unsuccessful outer
outcome and preserve its explanation. Include rejected in terminal-state tests.

<a id="boundaries"></a>

## Important boundaries, not new regression findings

- **Configured steps are not guaranteed executed steps.** The adapter creates
  Analysis, Execution, and Verification references on every run
  (`internal/run/creator.go:102-107` in the adapter). The operator still completes
  immediately on analysis `NoActionRequired`, bypassing execution/verification
  (`api/v1alpha1/agenticrun_types.go:122-128`). Rejection also intentionally stops
  before those stages. The charts supply Analysis Automatic / Execution Manual /
  Verification Automatic; the adapter alone does not enforce that policy.
  Confirm that these explicit skip cases satisfy the “every run includes all
  three steps” requirement, and test that the deployed policy cannot silently
  turn Execution automatic.
- **Selected option is model input, not a command-level security boundary.**
  Namespace writer RBAC bounds where execution can act; it does not enforce
  that every API mutation exactly matches the human-selected remediation.
  Workload creation/mutation also permits using ServiceAccounts and mounting
  Secrets in an allowed namespace, as documented in the chart.
- **Secret cleanup is not token revocation.** Removing `token-execution`
  prevents subsequent mounts/reads; a copied bearer token remains usable until
  expiration or an independent revocation/enforcement mechanism acts. Prefer
  short execution lifetimes and, for stronger guarantees, gateway-side checks
  against an active run/approval or sender-constrained credentials.
- **Hub Result publication and spoke access use different identities.** The
  gateway sandbox's hub `default` ServiceAccount publishes Result CRs; R/R_exec
  authorize spoke access. Per-step Result Roles bound to the same default SA
  accumulate permissions across concurrently active sandboxes. Consider
  dedicated per-run/step hub Result-publisher identities without bringing back
  spoke ServiceAccounts. See operator `sandbox_manager.go:151-186` and
  `rbac.go:510-564`.
- **A pause is not inferred from every authentication error.** The operator
  checks Secret existence, labels, and selected-key contents before launch.
  A sandbox-time downstream 401/403 currently follows ordinary Pod/Result
  failure handling; it does not automatically set `CredentialsExpired`.
- **Human identity depends on the inbound credential.** With ACME's shared
  workload token, task/context possession is a capability, not independent
  per-user approval. Use authenticated per-user sessions/tokens for personal
  attribution. Adapter ownership checks currently require the task's principal,
  so a separate maker/checker approver is not implemented by granting a group
  alone.
- **Durable inbound state is not durable outbound execution.** MAS persists
  tasks/contexts/mailboxes, but the outbound hub task/context bindings remain
  node-local until the activity returns. Outgoing message IDs are random and
  Temporal heartbeats contain only `running`. Add a durable outbound identity/
  idempotency checkpoint and worker-crash tests before claiming failover-safe or
  exactly-once delegation. ACME approval bindings are also process-local.
- **A revision API is not a working plain-chat revision path.** MAS accepts
  structured `decision: modify` / `feedback`, but its plain parser recognizes
  only leading approve/yes and reject/no/deny. A normal ACME chat request to
  revise is not currently translated into that structured command. Expose an
  explicit revision UI/command and test the full route.

## Suggested engineering improvements

1. Add cross-repository contract tests using the pinned Python and Go A2A SDKs,
   not just hand-built fakes: submit, pause, option selection, revise, reject,
   authentication renewal, terminal failures, stream reconnect, and restart.
2. Keep task projection separate from side effects. Derive a typed task snapshot
   once; let API reads, informer notifications, and SSE use it. Reconcile token
   writes/cleanup separately, with explicit retryable states.
3. Version and validate the `approvalRequest`/decision payloads with shared JSON
   fixtures or JSON Schema. Document which IDs are local at each hop; never
   assume the ACME, MAS, and hub task IDs are identical.
4. Add a rendered-pattern contract test that extracts the actual ArgoCD Helm
   parameters and checks audience/client IDs, gateway DNS, allowed targets,
   Secret names, namespaces, and image/flag compatibility across charts.
5. Keep the execution rollout disabled until negative as well as positive live
   tests pass. Successful chart renders and unit tests do not prove Keycloak
   audience downscoping, actual group claims, CNI selectors, or API-server RBAC.

### Repository-specific follow-ups

| Repository | Recommended improvement beyond the individual fixes |
|---|---|
| `lightspeed-hub` | Replace incremental follow-up side effects with parse/validate → load current view → authorize → commit → project. Use one cleanup reconciler; stale informer payloads should only trigger it. Correct the README's old “first absent stage” text and SA-read/caller-update identity description (`a2a-adapter/README.md:78-80,408-410,472-475`). |
| `lightspeed-agentic-operator` | Add handler-level Execution and Verification renewal tests; persist a structured credential-pause selector instead of relying solely on a Secret RV extracted from condition text. Keep per-step endpoint/key/projection selection centralized. |
| `uie-mas-hosted` | Exercise the real inbound decision parser and event/channel path in outbound HITL tests, and test invalid selections, delayed decisions, credential renewal, and both streaming and polling behavior together. Make the remote approval contract explicit rather than relying only on loose metadata dictionaries. |
| `multicloud-gitops` (including ACME) | Test generated resource identity uniqueness and actual cross-chart values. Retain ACME's real-`to_a2a` integration harness and test it against the container dependency closure; keep per-user authentication and task-capability behavior explicit. |

## Historical review validation and readiness

These are local review checks, including independent reviewers' disposable tests.
They do **not** establish live deployment correctness.

| Scope | Result |
|---|---|
| Charts | `python3 -m pytest tests/chart -q`: **91 passed**, one existing pytest/anyio deprecation warning. Targeted `east` / `east-exec` Helm render reproduced G1. |
| Hub adapter | `make test-a2a-adapter`: **passed**. Focused race tests for server/run/store: **passed**. Five disposable harness regressions reproduced H1–H5. |
| Operator | Isolated-source changed-package tests: **passed** (`controller/agenticrun`, `pkg/configuration`); `go vet ./...`: **passed**. Two temporary Go-overlay tests reproduced O1/O2. Full `make test` was not green: copied-tree `cmd/check-isa-level` fixture builds failed obtaining VCS status, outside the changed packages. |
| MAS | Focused suite: **57 passed, 6 subtests passed**. Disposable mapper/gate/projection probes reproduced M1–M4. The unchanged `test_a2a_agent_node_validator.py` failed collection due to a missing `StaticAuthMethod` import and was not part of the successful focused run. |
| ACME | **34 passed**, including a fresh environment installed from the exact container `requirements.txt` (ADK **2.10.0**, A2A SDK **1.2.1**). Additional real-stack/response probes reproduced A1–A3. No dependency-pin incompatibility was found. |
| Review/walkthrough documentation | Report and replacement walkthrough independently checked: **approved** after payload corrections. The HTML follows **one request across seven stages**. Fresh headless Chrome checks: **62 assertions passed** across proposal 1 / proposal 2 / rejection, including editable examples and invalid-input rejection; Enter/Space controls and retained focus passed. Layouts at **1365 / 760 / 390 / 320px**, including 63-character example names, had no page overflow or JavaScript exceptions; no network requests were made. Light/dark desktop and mobile screenshots inspected. Inline JS syntax, **39 unique IDs**, ARIA references, **24 links** (including review anchors), and **8 YAML excerpts/branch snapshots** passed checks. This replaces the superseded four-flow explorer’s validation. |
| All four working trees | `git diff --check`: **passed** at final review. |

The design has useful foundations: caller-authenticated run creation/approval,
atomic dual-key Secret writes, analysis-bound execution credentials, selected-key
Secret projection, deterministic same-task ACME routing, and MAS ownership/
mailbox replay protection. The principal gaps are cross-layer state/command
semantics, not a need to replace the architecture.

### Recommended order

1. Fix the seven High findings, then the Medium state/projection defects. Commit
   regression tests at the boundary where each defect was observed.
2. Add a cross-repository acceptance scenario: submit → complete analysis → show
   all options → invalid choice/correction → revise → approve → execute →
   automatic verify, including renewal and nonterminal stream EOF at each hop.
   Test rejection separately: completed analysis, no execution sandbox or token.
3. Build and pin compatible adapter/operator/MAS/ACME images, and reconcile
   existing Keycloak clients/roles as described by the chart migration notes.
4. In a controlled live namespace, prove read credentials cannot write, execution
   credentials cannot escape approved namespaces/target/audience, rejection mints
   no execution credential, and approval/renewal is attributed to the expected
   principal. Confirm real CNI selectors and Kubernetes authorization.
5. Test restart/worker-kill and stale-event recovery, then explicitly decide
   whether the documented default-SA Result publication and bearer-token lifetime
   boundaries are acceptable. Only then enable the standalone execution switches.

No image builds, live Keycloak exchanges, Kubernetes enforcement checks,
end-to-end rollout, worker-kill/failover tests, commits, or pushes were performed
as part of this review/map pass. Broader schema validation remains subject to the
previously missing Ansible collection; it is not claimed as passing here.
