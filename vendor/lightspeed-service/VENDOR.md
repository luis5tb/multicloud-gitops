# Vendoring notes: openshift/lightspeed-service

This directory is a tracked snapshot of the upstream
[`openshift/lightspeed-service`](https://github.com/openshift/lightspeed-service)
repository, extended with an A2A (Agent2Agent) endpoint so OpenShift
Lightspeed can serve the ACME delegation agent through Praxis. See
`LIGHTSPEED_MIGRATION_PLAN.md` and `LIGHTSPEED_IMPLEMENTATION_PLAN.md` at the
repository root for the full design and rationale.

## Upstream source

- Repository: <https://github.com/openshift/lightspeed-service>
- Commit vendored: **`c11e81671b3e84d76e8ad824f2d1d5485590e654`**
  ("Merge pull request #3114 from sriroopar/fix/OLS-0000-revert-langchain-aws-1-7-9",
  2026-09-30T22:14:16Z)

Two candidate pinned commits were identified by prior research for this
migration: `c11e81671b3e84d76e8ad824f2d1d5485590e654` and
`690939861bf7888209b1c9187614af3efd3a5a26`. Both resolved successfully via
the GitHub API (`GET /repos/openshift/lightspeed-service/commits/<sha>` ->
`200`) at the time of vendoring, confirming neither plan's research was
stale. `c11e8167...` is the later of the two commits (22:14 UTC vs 12:37 UTC
on the same day) and was used per the task instructions' stated preference
for the first candidate when reachable. Neither commit has an A2A endpoint
upstream, matching what both prior plans independently found -- this was
re-confirmed directly against the vendored tree (`ols/app/routers.py` had no
`a2a` import, and there was no `ols/app/endpoints/a2a.py`) before any new
code was added.

## How this snapshot was produced

A plain `git clone`/`git fetch --depth 1 <sha>` of this repository was
attempted first, but in this environment the clone transferred data at
roughly 1 MB/s and the upstream repository's full object pack is very large
(GitHub reports the repo's `.git` content at ~450 MB), so neither a full
clone nor a shallow fetch of the single pinned commit completed in
reasonable time (observed clone sizes still growing past 400+ MB after
several minutes with no end in sight).

Instead, the exact pinned commit's **tree** (not history) was fetched via
GitHub's codeload tarball endpoint, which does not require transferring any
git history:

```shell
curl -sL -o lightspeed-service.tar.gz \
  "https://codeload.github.com/openshift/lightspeed-service/tar.gz/c11e81671b3e84d76e8ad824f2d1d5485590e654"
tar xzf lightspeed-service.tar.gz
```

The extracted tree was copied into `vendor/lightspeed-service/` with `rsync`,
excluding `.git` (there was none in the tarball) and `embeddings_model/`
(see below). This is a **snapshot import**, not a `git subtree add` and not
a submodule: there is no nested `.git` directory, and the vendored files are
tracked as ordinary files in this repository's own git history, exactly as
instructed. No nested `.git`, `.venv`, `node_modules`, build caches, or
secrets were copied.

## Excluded: `embeddings_model/`

The upstream tree includes a committed `embeddings_model/` directory
containing two pre-downloaded HuggingFace embedding models
(`all-mpnet-base-v2` and `granite-embedding-30m-english`, split into
`model.safetensors.tar.gz.*` chunks) used for OLS's own RAG/doc-QA feature
and reassembled by the upstream `Containerfile` during the image build. That
directory alone is **434 MB** -- effectively a binary build input/cache, not
reviewable source -- and committing it here would make this already-large
GitOps pattern repository's history unmanageably large for a feature (RAG
over OpenShift product docs) this migration does not use or modify. It was
therefore deliberately **not** vendored.

This is a known, documented deviation from a literal "copy everything"
import, not an oversight. If a real image build is ever performed from this
vendored copy, restore it first:

```shell
curl -sL "https://codeload.github.com/openshift/lightspeed-service/tar.gz/c11e81671b3e84d76e8ad824f2d1d5485590e654" \
  | tar xzf - --strip-components=2 \
      "lightspeed-service-c11e81671b3e84d76e8ad824f2d1d5485590e654/embeddings_model" \
  -C vendor/lightspeed-service
```

(or restore it as a build-time fetch step outside of git, which is the
better long-term fix -- it does not need to live in version control at all).

## License and attribution

Upstream is licensed under the **Apache License 2.0**; the original
`LICENSE` file is preserved unmodified at `vendor/lightspeed-service/LICENSE`.
No copyright headers were altered. New files added for the A2A integration
(see below) are part of this pattern repository and carry no separate
upstream copyright claim; they are additive extensions built against the
Apache-2.0-licensed codebase.

## How to refresh this vendored copy

1. Pick the new upstream commit to pin (check CI/release notes for
   regressions first).
2. Re-run the codeload fetch above with the new commit SHA into a scratch
   directory.
3. Diff the scratch tree against `vendor/lightspeed-service/` (excluding
   `embeddings_model/`, which is intentionally absent here) to see what
   upstream changed.
4. Re-apply this fork's additive changes on top -- currently:
   - `ols/app/endpoints/a2a_auth.py` (new file)
   - `ols/app/endpoints/a2a.py` (new file -- real `a2a-sdk` server wiring)
   - `ols/app/endpoints/a2a_executor.py` (new file -- the `AgentExecutor`
     driving OLS's pipeline)
   - `ols/app/routers.py` (calls `a2a.register_routes(app)` directly, since
     the real `a2a-sdk` mounts raw Starlette routes onto `app.routes` itself
     and cannot go through `app.include_router()`)
   - `pyproject.toml` (adds `PyJWT[crypto]`, `spiffe`, and
     `a2a-sdk[http-server,fastapi]` as direct dependencies -- `PyJWT` was
     already present transitively; `spiffe` and `a2a-sdk` are new)
   - `.konflux/requirements.overrides.txt` (pins `a2a-sdk==1.1.5`; RHOAI's
     index only has the old, incompatible `0.3.26` -- see "Dependency
     lockfile" below)
   - `Containerfile` (adds an early `COPY ols/version.py` -- see "Local
     build issues found and fixed" below; check whether the new upstream
     commit still needs it before re-applying blindly)
   - This `VENDOR.md` file
5. Copy the refreshed tree over `vendor/lightspeed-service/` (still
   excluding `embeddings_model/` and `.git`), re-apply the diff from step 4,
   update the commit SHA and date in this file, and re-run the test suite
   described below.
6. Regenerate `uv.lock`/`requirements.txt` for the new dependencies (see
   "Dependency lockfile" below) and re-run a `helm template`/pattern lint
   pass on anything that consumes this image.

## New dependencies

`pyproject.toml` now also declares:

- `PyJWT[crypto]>=2.10.0,<3.0.0` -- already present transitively
  (`pyjwt==2.14.0` is already pinned in `requirements.txt`), declared
  directly because `ols/app/endpoints/a2a_auth.py` imports it explicitly.
- `spiffe==0.3.1` -- new. Used to fetch this pod's own SPIFFE JWT-SVID for
  the RFC 8693 token exchange, matching the pin already used by
  `agents/rca_agent` and `agents/acme_agent` elsewhere in this pattern
  repository.
- `a2a-sdk[http-server,fastapi]==1.1.5` -- new. The real A2A server
  framework (`ols/app/endpoints/a2a.py`), replacing a hand-rolled JSON-RPC
  dispatcher. Pinned to exactly the version `agents/acme_agent`'s own
  `a2a-sdk` client resolves to (`agents/acme_agent/requirements.txt`), for
  wire-format parity with the one real client this endpoint serves -- `uv
  lock` pulled in only three small new transitive packages (`aiologic`,
  `culsans`, `json-rpc`); `fastapi`/`starlette`/`httpx`/`pydantic`/
  `protobuf`/`google-api-core`/`googleapis-common-protos` were already
  present in this project's dependency graph (the last few transitively, via
  `langchain-google-vertexai`/the OTLP gRPC exporter).

### Dependency lockfile

**Regenerated** (`uv lock` + the Makefile's own `requirements.txt` export
command, confirmed with `uv lock --check`) once a real local build actually
needed it -- not done in the original pass that added `spiffe`/`PyJWT[crypto]`
to `pyproject.toml`, which is why the first local build attempt hit `error:
The lockfile at uv.lock needs to be updated, but --locked was provided`:

```shell
cd vendor/lightspeed-service
uv lock
uv export --format requirements-txt --no-dev --no-extra evaluation --no-editable --no-emit-package ols --output-file requirements.txt
```

Resulting changes: `spiffe==0.3.1` and its transitive `pem==23.1.0` added;
`pyjwt` resolved down from `2.14.0` to `2.13.0` across every consumer
(`ols`, `msal`, `spiffe` itself) -- `spiffe`'s own `pyjwt[crypto]` constraint
is the tightest in the graph, so `uv` picked the highest version satisfying
all of them at once. Still well within `pyproject.toml`'s declared
`PyJWT[crypto]>=2.10.0,<3.0.0`; not treated as a regression, but worth a
glance at PyJWT's changelog between 2.13.0 and 2.14.0 before shipping if
that gap turns out to matter.

`uv lock --check` passes against the current `pyproject.toml`. Re-run
`pip check`/the unit tests to confirm nothing else shifted before relying on
this for anything beyond a local build.

Adding `a2a-sdk` (`uv lock --upgrade-package a2a-sdk` per `docs/ai/
dependencies.md`'s bump sequence) was purely additive to the lock: only
`a2a-sdk`, `aiologic`, and `culsans` were added; no existing package version
shifted.

**Konflux hermetic regen** (`make konflux-requirements`, per
`docs/update-requirements.md`) surfaced a real problem, not just a routine
regen: the RHOAI package index only carries the old, pre-1.0 `a2a-sdk==0.3.26`
("v0.3" protocol era -- a completely different module layout, pydantic types
instead of protobuf, no `DefaultRequestHandlerV2`) and the resolver's
auto-generated RHOAI-version override silently substituted it for the pinned
`1.1.5` on the first run. A hermetic Konflux build would have shipped an
`a2a.py` built against `1.1.5`'s APIs running on an installed `0.3.26` --
immediate `ImportError`s, not a subtle version-skew bug. Fixed by adding an
explicit `a2a-sdk==1.1.5` entry to `.konflux/requirements.overrides.txt`
(same mechanism already used there for `torch`/`faiss-cpu`/etc.), which takes
precedence over the auto-generated override; re-running
`make konflux-requirements` then correctly placed `a2a-sdk==1.1.5` in
`requirements.hashes.source.txt` (PyPI sdist, since RHOAI has no `1.1.5`
wheel). `aiologic`/`culsans` (small internal async-queue deps of `a2a-sdk`
itself) did resolve to older RHOAI-wheel versions than `uv.lock`'s -- left
as-is, matching this file's standing convention of only overriding a
transitive package when there's a concrete known incompatibility, not merely
a version difference. `scripts/verify_hermetic_requirements.sh` passes.

## A2A endpoint (new)

Added under `ols/app/endpoints/`:

- `a2a_auth.py` -- Keycloak token validation (issuer, RS256, audience
  `lightspeed-a2a`, and an `azp` **allow-list**), this pod's SPIFFE JWT-SVID
  fetch, and the RFC 8693 token exchange (client `lightspeed-mcp`, audience
  `openshift-mcp`, no `client_id` form parameter -- ported from the pattern
  proven in `agents/rca_agent/rca_agent/identity.py`, adapted to this
  service's async/FastAPI style). Also owns two cross-cutting concerns:
  - **Inbound `azp` allow-list (`A2A_INBOUND_AZP`).** `A2ASettings.inbound_azp`
    is a `frozenset[str]` parsed from the comma-separated `A2A_INBOUND_AZP`
    env var (split on comma, strip, drop empties; empty/unset ->
    `{"acme-agent"}`, preserving the original single-value behavior). The
    validator rejects any token whose `azp` is not in the set. This unblocks
    adding a second caller (e.g. a future orchestrator) by config alone --
    the chart renders the env var as `acme-agent` or `acme-agent,orchestrator`
    (no spaces).
  - **Per-request audit record + correlation id.** `CallerIdentity` carries a
    `request_id` (read from the optional inbound `X-Request-Id` header, else
    generated per request), and `audit_record()` builds a single greppable
    `a2a_audit key=value ...` line recording that the OLS workload
    (`actor=lightspeed-mcp`, who performs the exchange and drives MCP) acted
    `on_behalf_of` the validated caller (`azp`/`subject`), against `cluster`,
    for one a2a `task`/`context`, with an `outcome`. No token is ever included.
- `a2a.py` -- the real `a2a-sdk` server framework (`DefaultRequestHandler`,
  `InMemoryTaskStore`, the SDK's own Starlette/FastAPI route builders), not a
  hand-rolled JSON-RPC dispatcher: `GET /.well-known/agent-card.json`
  (public) and `POST /` supporting the full real method set --
  `SendMessage`, `SendStreamingMessage` (real SSE task-status/artifact
  streaming), `GetTask`, `SubscribeToTask`; `CancelTask` is accepted but
  always reports unsupported. A small `A2AAuthMiddleware` (ASGI-level, not a
  FastAPI `Depends()` -- the SDK's routes bypass FastAPI's own
  dependency-injection machinery) authenticates every call before the SDK's
  dispatcher runs, reusing `a2a_auth.py` unchanged; `InMemoryTaskStore`'s
  `owner_resolver` hook keys every task by `CallerIdentity.task_scope`, so a
  task created by one (caller, cluster) is reported as not-found (not merely
  forbidden) to any other, the same isolation the old hand-rolled dispatcher
  enforced with a bespoke check. The MCP-header-override block-list the old
  dispatcher needed is gone -- not loosened, structurally unnecessary: the
  new executor reads A2A message metadata only for the `ols_mode` selector and
  never for MCP headers (see `a2a_executor.py`).
- `a2a_executor.py` -- the actual business logic (`OLSAgentExecutor`),
  separate from the transport/routing wiring in `a2a.py`. Runs requests through
  `ols/app/endpoints/ols.py`'s `process_request()` (fresh OLS conversation ID,
  query validation/redaction, and quota pre-check), then uses the existing
  `generate_response(streaming=True)` async generator and
  `streaming_ols.py`'s `response_processing_wrapper()` for normal conversation
  and transcript storage, quota consumption, and structured OLS audit events.
  It maps the resulting `StreamedChunk` events onto real `TaskUpdater`
  status/artifact events: `TEXT` chunks
  stream into one running answer artifact, `TOOL_CALL`/`TOOL_RESULT`/
  `REASONING`/`SKILL_SELECTED` surface as `TASK_STATE_WORKING` status
  updates carrying the chunk's own data as metadata, and the final message
  on completion carries the full accumulated answer text (so non-streaming
  `GetTask` polling still sees the complete answer). `APPROVAL_REQUIRED`
  (OLS's human-in-the-loop tool-approval flow, `tool_approvals.py`) fails the
  task with a clear message instead of guessing at a `TASK_STATE_INPUT_REQUIRED`
  resumption protocol wired to that REST flow's conversation-id-keyed
  approval endpoint -- unverified, separate work, not something to fake.
  The A2A message metadata key `ols_mode` selects `ask` or
  `troubleshooting`; it defaults to `troubleshooting` for this live-cluster
  investigation use case. A2A-supplied context IDs are not used as OLS
  conversation IDs. Empty messages, non-text parts, and unknown mode values
  fail before token exchange or LLM invocation.
  Every exchange still performs a fresh per-call RFC 8693 exchange. The
  executor emits **exactly one** `a2a_audit` INFO
  line per request (see `a2a_auth.audit_record`) at whichever terminal path
  the request takes -- `outcome=ok` on completion, `outcome=error` on exchange
  failure / approval-required / query failure -- so every request that reached
  the MCP-drive stage leaves one "OLS did X on behalf of `<caller>`" record.

Registered in `ols/app/routers.py` via `a2a.register_routes(app)` (not
`app.include_router()` -- the real `a2a-sdk` appends raw Starlette routes
directly onto `app.routes`, so it needs the actual `FastAPI` app object), in
the same place/order `health`/`metrics` are registered, without the `/v1`
prefix.

One subtlety worth recording: the SDK's `DefaultRequestHandler` only ever
consults the `AgentCard` passed to its constructor for `capabilities.*`
(to gate `SendStreamingMessage`/push-notification methods) -- never its
deployment-specific fields (name, rpc url, ...). Those come from
`A2ASettings.from_env()`, which must stay lazy (raising
`A2AConfigurationError` only when the discovery endpoint is actually hit, as
before) so a deployment that never configures A2A (`appServerPatch` disabled)
can still start OLS at all. `a2a.py` therefore builds two different
`AgentCard`s: a static, env-free one (just `capabilities`) for the request
handler at startup, and the full one -- built lazily via
`create_agent_card_routes`'s `card_modifier` hook, called once per actual
request to the discovery endpoint -- for what callers actually see.

Configuration is via environment variables (see `A2ASettings.from_env()` in
`a2a_auth.py` for the full list and defaults); the required,
deployment-specific ones are `A2A_KEYCLOAK_ISSUER_URL`, `A2A_CLUSTER_ID`, and
`A2A_RPC_URL` (the public Praxis HTTPS origin -- never a hardcoded cluster
hostname). The routing header name (`X-OLS-Cluster`), inbound audience
(`lightspeed-a2a`), inbound caller allow-list (`A2A_INBOUND_AZP`, a
comma-separated list defaulting to `acme-agent`), and exchange audience
(`openshift-mcp`) match the frozen interface decisions in the implementation
plan and ship as defaults; they are still environment-overridable for
testing. An optional `X-Request-Id` inbound header is honored for
correlation and otherwise generated per request; it is not configuration.

Unit tests are under `tests/unit/app/endpoints/test_a2a_auth.py` and
`tests/unit/app/endpoints/test_a2a.py`. See their module docstrings and the
root-level test-run notes below for how to run them.

## Container image build (T1.4)

The upstream `Containerfile` is present at `vendor/lightspeed-service/Containerfile`.
The A2A endpoint itself is pure Python source copied the same way `ols/`
already is (no new build stage required), but one small Containerfile change
was needed to actually build locally -- see "Local build issues found and
fixed" below. Once that fix is in place, `embeddings_model/` is restored
(see above), and `uv.lock`/`requirements.txt` are regenerated, the build
command is the standard:

```shell
podman build -f vendor/lightspeed-service/Containerfile \
  -t <registry>/<repo>/lightspeed-service:<tag> \
  vendor/lightspeed-service
```

Building and publishing an image by immutable digest -- and wiring the
OpenShift Lightspeed Operator's `--service-image` override (now
`appServerPatch` in `charts/all/openshift-lightspeed-config`, a temporary
bridge -- see that chart's README.md) to it -- is tracked in Phase 0/2 of
the top-level `README.md`.

### Local build issues found and fixed

Discovered by actually attempting a local, non-hermetic `podman build`
(something CI never does -- Konflux's hermetic path takes a different code
path through the Containerfile that avoids the first two of these):

1. **`registry.redhat.io/rhel9/python-312` is an entitled RHEL image, not
   UBI.** `dnf install` inside it (`gcc gcc-c++ cmake cargo`) needs real Red
   Hat subscription entitlement certs mounted into the build
   (`/etc/pki/entitlement`, `/etc/rhsm`) -- a plain `podman login
   registry.redhat.io` (enough to pull the image itself) is not sufficient.
   Either register a system (a free Red Hat Developer Subscription works)
   and mount its entitlement certs via `podman build --volume
   /etc/pki/entitlement:/etc/pki/entitlement:ro --volume
   /etc/rhsm/ca:/etc/rhsm/ca:ro ...`, or override
   `--build-arg BUILDER_BASE_IMAGE=registry.access.redhat.com/ubi9/python-312:9.6
   --build-arg RUNTIME_BASE_IMAGE=registry.access.redhat.com/ubi9/python-312-minimal:9.6`
   to use the free UBI equivalents instead (same Python version, no
   entitlement needed; this pattern's own charts already standardize on the
   UBI9 minimal image elsewhere). Not fixed in the Containerfile itself --
   this is an environment/credentials concern, not a code bug.
2. **`uv sync --locked` fails without `uv.lock` regenerated** for the
   `spiffe`/`PyJWT[crypto]` additions -- see "Dependency lockfile" below,
   already documented before this build was attempted.
3. **`COPY ols ./ols` doesn't happen until *after* the dependency-install
   `RUN` step** (intentional Docker layer-caching: install deps once, only
   rebuild on source changes), but `uv sync --no-install-project` still
   invokes hatchling's `prepare_metadata_for_build_editable` hook to resolve
   `[tool.hatch.version] path = "ols/version.py"` (`pyproject.toml`'s
   dynamic version source) despite `--no-install-project`, and fails with
   `OSError: ... file does not exist: ols/version.py`. Only the hermetic
   branch avoids this (it calls `uv pip install --no-deps`, never `uv
   sync`, so it never resolves project metadata) -- which is why Konflux's
   CI never hit it. **Fixed** in the `Containerfile`: added
   `COPY ols/version.py ./ols/version.py` right after the Step 1 metadata
   copy, before the dependency-install `RUN`. `ols/version.py` is a static,
   rarely-changing one-line version string, so copying it this early barely
   affects layer-cache invalidation.

None of these three were hit or fixed by whichever earlier pass wrote the
original "Container image build -- not performed in this pass" note; they
were only discovered once a real local build was actually attempted.

## Provenance note

This vendoring and the A2A endpoint above were produced independently,
using only the genuine public upstream source (GitHub API/codeload) and this
pattern repository's own `agents/rca_agent`/`agents/acme_agent` code as
read-only reference. No other local copy of `lightspeed-service` (vendored,
forked, or otherwise) was read from or relied upon while doing this work.
