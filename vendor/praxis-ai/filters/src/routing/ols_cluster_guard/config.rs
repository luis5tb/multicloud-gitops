// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Praxis Contributors

//! Configuration for the OLS cluster header guard.

use std::collections::HashSet;

use serde::Deserialize;

/// Deserialized YAML configuration for the OLS cluster header guard.
///
/// `allowed_clusters` contains exact, stable cluster IDs. IDs must be
/// non-empty visible ASCII strings without whitespace or commas.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OlsClusterGuardConfig {
    /// Allow the cluster-neutral agent-card GET to omit `X-OLS-Cluster`.
    ///
    /// This exception applies only to `GET /.well-known/agent-card.json` and
    /// defaults to false.
    #[serde(default)]
    pub allow_public_agent_card: bool,

    /// Exact stable cluster IDs accepted in `X-OLS-Cluster`.
    pub allowed_clusters: Vec<String>,
}

/// Validate the allow-list before using it for request checks.
pub(super) fn validate_config(config: &OlsClusterGuardConfig) -> Result<(), String> {
    if config.allowed_clusters.is_empty() {
        return Err("ols_cluster_guard: allowed_clusters must not be empty".into());
    }

    let mut seen = HashSet::with_capacity(config.allowed_clusters.len());
    for cluster_id in &config.allowed_clusters {
        if cluster_id.is_empty()
            || !cluster_id.is_ascii()
            || cluster_id
                .bytes()
                .any(|byte| !byte.is_ascii_graphic() || byte == b',')
        {
            return Err(
                "ols_cluster_guard: cluster IDs must be non-empty visible ASCII without whitespace or commas".into(),
            );
        }
        if !seen.insert(cluster_id) {
            return Err("ols_cluster_guard: allowed_clusters must not contain duplicate IDs".into());
        }
    }

    Ok(())
}
