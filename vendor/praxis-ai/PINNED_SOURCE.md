# Pinned Praxis AI source

This directory is a source snapshot of [`praxis-proxy/ai`](https://github.com/praxis-proxy/ai)
release v0.4.1 at upstream commit
`b9d6016764888e02dc049ec088496b10b7e886c1`. The upstream snapshot is retained
here without a nested Git repository, build output, or Cargo cache.

The `ols_cluster_guard` filter is a local security patch to that source. It
rejects missing, malformed, duplicate (including differently-cased header
names), and non-allow-listed `X-OLS-Cluster` selectors before the router, with
an exception only for `GET /.well-known/agent-card.json` when
`allow_public_agent_card: true` is set (default false). Its allow-list contains
exact stable IDs and is never used to resolve or construct an upstream. Keep
authenticated pipelines ordered **policy →
`ols_cluster_guard` → router → load_balancer**; this patch does not remove or
replace Keycloak/JWT or Praxis Policy Engine checks.

There is no known duplicate-header visibility limitation at the filter API:
`praxis_filter::Request` carries an `http::HeaderMap`, and the guard uses
`get_all()` to count every stored value. Source inspection of the Praxis Core
0.7.0 lockfile dependency and the planned 0.7.2 source shows the Pingora
adapter cloning the parsed request header map into that request snapshot; its
pre-pipeline duplicate normalization is limited to `Content-Length` and
`Content-Type`, not `X-OLS-Cluster`. The integration test also sends repeated
wire headers (including field-name case variants) and asserts the fallback
backend is not reached. Run it against the final Core 0.7.2 build before
publishing, because source inspection is not a substitute for that build's
runtime verification.

Build and test from this directory with the upstream toolchain requirements
(Rust stable 1.96+, nightly `rustfmt`, CMake 3.31+, and Docker/Podman for
container builds):

```bash
cargo test -p praxis-ai-filters --features full ols_cluster_guard
cargo test -p praxis-tests-integration --features full ols_cluster_guard
cargo xtask generate-filter-docs
cargo xtask lint-example-tests
cargo test --workspace --locked
make container PRAXIS_AI_FEATURES=full
```

The checked-in upstream `Cargo.lock` remains the v0.4.1/Core 0.7.0 lockfile;
the separate planned Core 0.7.2 candidate build must retain and review its
updated lockfile before publication. The patch's source tests and docs are
included, but Rust verification must be run in an environment meeting the
toolchain and native build requirements above.
