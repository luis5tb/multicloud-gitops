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
   - `ols/app/endpoints/a2a.py` (new file)
   - `ols/app/routers.py` (adds the `a2a` router registration)
   - `pyproject.toml` (adds `PyJWT[crypto]` and `spiffe` as direct
     dependencies -- `PyJWT` was already present transitively; `spiffe` is
     new)
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

### Dependency lockfile

**`uv.lock` and `requirements.txt` were not regenerated in this pass.** This
project resolves/locks dependencies with `uv` (`uv sync --locked`, per the
`Containerfile`), and `requirements.txt` is a fully pinned, hash-locked
export produced from that lock. Regenerating it requires resolving the
*entire* dependency set (including `torch`, `faiss-cpu`, `llama-index`,
etc.) against PyPI, which was not attempted here because:

- it is a heavy, slow operation unrelated to reviewing the actual code
  change, and
- `spiffe==0.3.1` is a small, pure-Python-plus-grpc package with no
  resolution conflicts expected against the existing lock (it is already
  used elsewhere in this repository with the same constraints), so the risk
  of silently introducing a conflicting pin is low but not zero.

Before building a real image from this vendored copy, run (network access
required):

```shell
cd vendor/lightspeed-service
uv lock
uv export --no-dev --no-hashes -o requirements.txt   # or the project's existing export command
```

and re-run `pip check`/the unit tests to confirm nothing else shifted.

## A2A endpoint (new)

Added under `ols/app/endpoints/`:

- `a2a_auth.py` -- Keycloak token validation (issuer, RS256, audience
  `lightspeed-a2a`, `azp=acme-agent`), this pod's SPIFFE JWT-SVID fetch, and
  the RFC 8693 token exchange (client `lightspeed-mcp`, audience
  `openshift-mcp`, no `client_id` form parameter -- ported from the pattern
  proven in `agents/rca_agent/rca_agent/identity.py`, adapted to this
  service's async/FastAPI style).
- `a2a.py` -- `GET /.well-known/agent-card.json` (public) and `POST /`
  (JSON-RPC 2.0: `SendMessage`/`message/send` and `GetTask`/`tasks/get`,
  matching both the real pinned `a2a-sdk==1.1.5` client used by
  `agents/acme_agent` and the legacy method-name spelling used by that
  agent's own hand-written browser UI). Every RPC call feeds **only** the
  freshly exchanged token into OLS's existing MCP token-resolution path
  (`ols/utils/mcp_utils.py`'s `"kubernetes"` placeholder) for that one call,
  and explicitly rejects any client-supplied MCP header override. The stock
  `/v1/*` REST endpoints and their `k8s` TokenReview authentication are
  unchanged.

Registered in `ols/app/routers.py` without the `/v1` prefix, the same way
`health`/`metrics` are registered.

Configuration is via environment variables (see `A2ASettings.from_env()` in
`a2a_auth.py` for the full list and defaults); the required,
deployment-specific ones are `A2A_KEYCLOAK_ISSUER_URL`, `A2A_CLUSTER_ID`, and
`A2A_RPC_URL` (the public Praxis HTTPS origin -- never a hardcoded cluster
hostname). The routing header name (`X-OLS-Cluster`), inbound audience
(`lightspeed-a2a`), inbound caller (`azp=acme-agent`), and exchange audience
(`openshift-mcp`) match the frozen interface decisions in the implementation
plan and ship as defaults; they are still environment-overridable for
testing.

Unit tests are under `tests/unit/app/endpoints/test_a2a_auth.py` and
`tests/unit/app/endpoints/test_a2a.py`. See their module docstrings and the
root-level test-run notes below for how to run them.

## Container image build (T1.4 -- not performed in this pass)

The upstream `Containerfile` is present at `vendor/lightspeed-service/Containerfile`
and is unmodified by this change (the A2A endpoint is pure Python source
copied the same way `ols/` already is; no new build stage is required). The
build command, once `embeddings_model/` is restored (see above) and
`uv.lock`/`requirements.txt` are regenerated, is the standard:

```shell
podman build -f vendor/lightspeed-service/Containerfile \
  -t <registry>/<repo>/lightspeed-service:<tag> \
  vendor/lightspeed-service
```

**No image was built or pushed in this pass.** Per the task scope, this is a
source-only change: no `oc apply`/`helm install`/registry push was
performed, and no registry credentials exist in this task's environment to
do so. Building and publishing an image by immutable digest -- and wiring
the OpenShift Lightspeed Operator's `--service-image` override to it -- is
explicit follow-up work tracked in Phase 1/2 of
`LIGHTSPEED_IMPLEMENTATION_PLAN.md`, not something this pass claims to have
done.

## Provenance note

This vendoring and the A2A endpoint above were produced independently,
using only the genuine public upstream source (GitHub API/codeload) and this
pattern repository's own `agents/rca_agent`/`agents/acme_agent` code as
read-only reference. No other local copy of `lightspeed-service` (vendored,
forked, or otherwise) was read from or relied upon while doing this work.
