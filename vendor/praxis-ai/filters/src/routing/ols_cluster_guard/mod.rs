// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Praxis Contributors

//! Fail-closed validation of the stable cluster selector header.

mod config;

#[cfg(test)]
#[expect(clippy::allow_attributes, reason = "blanket test suppressions")]
#[allow(clippy::expect_used, clippy::panic, clippy::unwrap_used, reason = "tests")]
mod tests;

use std::collections::HashSet;

use async_trait::async_trait;
use http::{Method, header::HeaderName};
use praxis_filter::{FilterAction, FilterError, HttpFilter, HttpFilterContext, Rejection, parse_filter_config};

use self::config::{OlsClusterGuardConfig, validate_config};

const OLS_CLUSTER_HEADER: HeaderName = HeaderName::from_static("x-ols-cluster");
const PUBLIC_AGENT_CARD_PATH: &str = "/.well-known/agent-card.json";

/// Requires exactly one `X-OLS-Cluster` header whose value is an allow-listed
/// stable ID, with an explicit default-off exception for the public agent-card
/// GET. It validates only: it never resolves an upstream or derives a cluster
/// endpoint from request data.
///
/// This is not an authorization filter and does not replace JWT/Keycloak or
/// Praxis Policy Engine checks. In an authenticated deployment, preserve the
/// pipeline order **policy → `ols_cluster_guard` → router → load_balancer**.
/// The guard must run after authorization (so rejected identities are never
/// treated as authorized) and before routing (so requests requiring a selector
/// cannot reach a fallback route without one).
///
/// With `allow_public_agent_card: true`, only `GET /.well-known/agent-card.json`
/// bypasses selector validation. The option defaults to false. Every other
/// method and path still requires exactly one allow-listed selector. Header
/// names are case-insensitive; repeated values remain visible through the
/// request `HeaderMap` and are rejected even if identical.
///
/// Requests requiring a selector are rejected before routing if they have zero
/// or multiple values, an empty/malformed value, or a value outside the exact
/// allow-list.
///
/// # YAML configuration
///
/// ```yaml
/// filter: ols_cluster_guard
/// allow_public_agent_card: true
/// allowed_clusters:
///   - cluster-a
///   - cluster-b
/// ```
pub struct OlsClusterGuardFilter {
    allow_public_agent_card: bool,
    allowed_clusters: HashSet<String>,
}

impl OlsClusterGuardFilter {
    /// Parse and validate the exact stable-ID allow-list.
    ///
    /// # Errors
    ///
    /// Returns [`FilterError`] for an empty, malformed, or duplicate allow-list
    /// and for unknown configuration fields.
    pub fn from_config(value: &serde_yaml::Value) -> Result<Box<dyn HttpFilter>, FilterError> {
        let config: OlsClusterGuardConfig = parse_filter_config("ols_cluster_guard", value)?;
        validate_config(&config).map_err(FilterError::from)?;

        Ok(Box::new(Self {
            allow_public_agent_card: config.allow_public_agent_card,
            allowed_clusters: config.allowed_clusters.into_iter().collect(),
        }))
    }
}

#[async_trait]
impl HttpFilter for OlsClusterGuardFilter {
    fn name(&self) -> &'static str {
        "ols_cluster_guard"
    }

    async fn on_request(&self, ctx: &mut HttpFilterContext<'_>) -> Result<FilterAction, FilterError> {
        if self.allow_public_agent_card
            && ctx.request.method == Method::GET
            && ctx.request.uri.path() == PUBLIC_AGENT_CARD_PATH
        {
            return Ok(FilterAction::Continue);
        }

        let mut values = ctx.request.headers.get_all(&OLS_CLUSTER_HEADER).iter();
        let Some(value) = values.next() else {
            return Ok(FilterAction::Reject(Rejection::status(400)));
        };

        // Do not accept identical duplicates or rely on the router's first-value
        // behavior. The header name is case-insensitive in HeaderMap, so values
        // sent under differently-cased field names are counted together.
        if values.next().is_some() {
            return Ok(FilterAction::Reject(Rejection::status(400)));
        }

        let Ok(cluster_id) = value.to_str() else {
            return Ok(FilterAction::Reject(Rejection::status(400)));
        };
        if cluster_id.is_empty()
            || cluster_id
                .bytes()
                .any(|byte| !byte.is_ascii_graphic() || byte == b',')
        {
            return Ok(FilterAction::Reject(Rejection::status(400)));
        }

        if self.allowed_clusters.contains(cluster_id) {
            Ok(FilterAction::Continue)
        } else {
            Ok(FilterAction::Reject(Rejection::status(403)))
        }
    }
}
