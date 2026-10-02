// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Praxis Contributors

//! Unit tests for the OLS cluster header guard.

use http::{HeaderMap, HeaderValue, Method};
use praxis_filter::{FilterAction, HttpFilter};

use super::OlsClusterGuardFilter;
use crate::test_utils::{make_filter_context, make_request};

fn make_filter() -> Box<dyn HttpFilter> {
    make_filter_from_yaml("allowed_clusters:\n  - cluster-a\n  - cluster-b\n")
}

fn make_filter_from_yaml(yaml: &str) -> Box<dyn HttpFilter> {
    let config: serde_yaml::Value = serde_yaml::from_str(yaml).unwrap();
    OlsClusterGuardFilter::from_config(&config).unwrap()
}

async fn run(headers: HeaderMap) -> FilterAction {
    let filter = make_filter();
    run_with_filter(filter, Method::POST, "/a2a/", headers).await
}

async fn run_with_filter(
    filter: Box<dyn HttpFilter>,
    method: Method,
    path: &str,
    headers: HeaderMap,
) -> FilterAction {
    let mut request = make_request(method, path);
    request.headers = headers;
    let mut context = make_filter_context(&request);
    filter.on_request(&mut context).await.unwrap()
}

#[test]
fn config_requires_a_nonempty_allow_list_of_unique_safe_ids() {
    for config in [
        "allowed_clusters: []",
        "allowed_clusters:\n  - ''",
        "allowed_clusters:\n  - 'cluster,other'",
        "allowed_clusters:\n  - cluster-a\n  - cluster-a",
    ] {
        let value = serde_yaml::from_str(config).unwrap();
        assert!(OlsClusterGuardFilter::from_config(&value).is_err(), "config should fail: {config}");
    }
}

#[test]
fn config_rejects_unknown_fields() {
    let value = serde_yaml::from_str("allowed_clusters: [cluster-a]\nallow_all: true").unwrap();
    assert!(OlsClusterGuardFilter::from_config(&value).is_err());
}

#[tokio::test]
async fn missing_cluster_header_is_rejected() {
    assert!(matches!(run(HeaderMap::new()).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn empty_cluster_header_is_rejected() {
    let mut headers = HeaderMap::new();
    headers.insert("x-ols-cluster", HeaderValue::from_static(""));
    assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn malformed_comma_joined_cluster_header_is_rejected() {
    let mut headers = HeaderMap::new();
    headers.insert("x-ols-cluster", HeaderValue::from_static("cluster-a, cluster-b"));
    assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn non_utf8_cluster_header_is_rejected() {
    let mut headers = HeaderMap::new();
    headers.insert(
        "x-ols-cluster",
        HeaderValue::from_bytes(&[0xff]).expect("opaque header bytes are valid HeaderValue bytes"),
    );
    assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn unknown_or_non_exact_cluster_id_is_rejected() {
    for value in ["cluster-c", "Cluster-a"] {
        let mut headers = HeaderMap::new();
        headers.insert("x-ols-cluster", HeaderValue::from_static(value));
        assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 403));
    }
}

#[tokio::test]
async fn exactly_one_allow_listed_cluster_id_continues() {
    let mut headers = HeaderMap::new();
    headers.insert("X-OLS-Cluster", HeaderValue::from_static("cluster-a"));
    assert!(matches!(run(headers).await, FilterAction::Continue));
}

#[tokio::test]
async fn identical_duplicate_values_are_rejected() {
    let mut headers = HeaderMap::new();
    headers.append("x-ols-cluster", HeaderValue::from_static("cluster-a"));
    headers.append("x-ols-cluster", HeaderValue::from_static("cluster-a"));
    assert_eq!(headers.get_all("x-ols-cluster").iter().count(), 2);
    assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn conflicting_case_variant_duplicate_values_are_rejected() {
    let mut headers = HeaderMap::new();
    headers.append("X-OLS-Cluster", HeaderValue::from_static("cluster-a"));
    headers.append("x-ols-cluster", HeaderValue::from_static("cluster-b"));
    assert_eq!(headers.get_all("x-ols-cluster").iter().count(), 2);
    assert!(matches!(run(headers).await, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn public_agent_card_get_requires_explicit_opt_in() {
    let filter = make_filter();
    let action = run_with_filter(filter, Method::GET, "/.well-known/agent-card.json", HeaderMap::new()).await;
    assert!(matches!(action, FilterAction::Reject(rejection) if rejection.status == 400));
}

#[tokio::test]
async fn opted_in_public_agent_card_get_continues_without_a_selector() {
    let filter = make_filter_from_yaml(
        "allow_public_agent_card: true\nallowed_clusters:\n  - cluster-a\n  - cluster-b\n",
    );
    let action = run_with_filter(filter, Method::GET, "/.well-known/agent-card.json", HeaderMap::new()).await;
    assert!(matches!(action, FilterAction::Continue));
}

#[tokio::test]
async fn public_agent_card_opt_in_does_not_exempt_other_methods_or_paths() {
    let requests = [
        (Method::POST, "/.well-known/agent-card.json"),
        (Method::HEAD, "/.well-known/agent-card.json"),
        (Method::GET, "/.well-known/agent-card.json/extra"),
        (Method::GET, "/unrelated"),
    ];

    for (method, path) in requests {
        let request_name = format!("{method} {path}");
        let filter = make_filter_from_yaml(
            "allow_public_agent_card: true\nallowed_clusters:\n  - cluster-a\n  - cluster-b\n",
        );
        let action = run_with_filter(filter, method, path, HeaderMap::new()).await;
        assert!(
            matches!(action, FilterAction::Reject(rejection) if rejection.status == 400),
            "{request_name} must still require a cluster selector",
        );
    }
}
