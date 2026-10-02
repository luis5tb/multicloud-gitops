// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Praxis Contributors

//! Tests for the fail-closed OLS cluster selector guard example.

use std::collections::HashMap;

use praxis_test_utils::{free_port, http_send, parse_body, parse_status, start_backend_with_shutdown};

fn load_config(
    listener_port: u16,
    cluster_a: u16,
    cluster_b: u16,
    fallback: u16,
    agent_card: u16,
) -> praxis_core::config::Config {
    super::load_example_config(
        "ols-cluster-guard.yaml",
        listener_port,
        HashMap::from([
            ("127.0.0.1:9101", cluster_a),
            ("127.0.0.1:9102", cluster_b),
            ("127.0.0.1:9103", fallback),
            ("127.0.0.1:9104", agent_card),
        ]),
    )
}

#[test]
fn ols_cluster_guard_example_parses() {
    let config = load_config(29920, 29921, 29922, 29923, 29924);
    assert_eq!(config.listeners.len(), 1, "should have one listener");
    assert_eq!(&*config.listeners[0].name, "ols-gateway");
}

#[test]
#[expect(
    clippy::too_many_lines,
    reason = "keeps the public exception and fail-closed fallback cases together"
)]
fn ols_cluster_guard_routes_only_valid_selectors_and_never_uses_fallback_for_invalid_values() {
    let cluster_a = start_backend_with_shutdown("cluster-a-backend");
    let cluster_b = start_backend_with_shutdown("cluster-b-backend");
    let fallback = start_backend_with_shutdown("fallback-backend");
    let agent_card = start_backend_with_shutdown("agent-card-backend");
    let proxy = praxis_test_utils::start_proxy(&load_config(
        free_port(),
        cluster_a.port(),
        cluster_b.port(),
        fallback.port(),
        agent_card.port(),
    ));

    let public_agent_card = http_send(
        proxy.addr(),
        "GET /.well-known/agent-card.json HTTP/1.1\r\n\
         Host: localhost\r\n\
         Connection: close\r\n\r\n",
    );
    assert_eq!(parse_status(&public_agent_card), 200, "opted-in public card GET should pass");
    assert_eq!(parse_body(&public_agent_card), "agent-card-backend");

    let valid = http_send(
        proxy.addr(),
        "GET /a2a/ HTTP/1.1\r\n\
         Host: localhost\r\n\
         X-OLS-Cluster: cluster-a\r\n\
         Connection: close\r\n\r\n",
    );
    assert_eq!(parse_status(&valid), 200, "one valid selector should route");
    assert_eq!(parse_body(&valid), "cluster-a-backend");

    let valid_case_variant = http_send(
        proxy.addr(),
        "GET /a2a/ HTTP/1.1\r\n\
         Host: localhost\r\n\
         x-OlS-cLuStEr: cluster-b\r\n\
         Connection: close\r\n\r\n",
    );
    assert_eq!(parse_status(&valid_case_variant), 200, "field-name matching is case-insensitive");
    assert_eq!(parse_body(&valid_case_variant), "cluster-b-backend");

    let invalid_requests = [
        (
            "unrelated headerless GET",
            "GET /unrelated HTTP/1.1\r\n\
             Host: localhost\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "agent-card POST without selector",
            "POST /.well-known/agent-card.json HTTP/1.1\r\n\
             Host: localhost\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "agent-card HEAD without selector",
            "HEAD /.well-known/agent-card.json HTTP/1.1\r\n\
             Host: localhost\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "empty selector",
            "GET /a2a/ HTTP/1.1\r\n\
             Host: localhost\r\n\
             x-ols-cluster: \r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "unknown selector",
            "GET /a2a/ HTTP/1.1\r\n\
             Host: localhost\r\n\
             x-ols-cluster: not-registered\r\n\
             Connection: close\r\n\r\n",
            403,
        ),
        (
            "repeated identical selectors",
            "GET /a2a/ HTTP/1.1\r\n\
             Host: localhost\r\n\
             X-OLS-Cluster: cluster-a\r\n\
             x-ols-cluster: cluster-a\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "conflicting case-variant selectors",
            "GET /a2a/ HTTP/1.1\r\n\
             Host: localhost\r\n\
             X-OLS-Cluster: cluster-a\r\n\
             x-Ols-Cluster: cluster-b\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
        (
            "comma-joined selector",
            "GET /a2a/ HTTP/1.1\r\n\
             Host: localhost\r\n\
             x-ols-cluster: cluster-a, cluster-b\r\n\
             Connection: close\r\n\r\n",
            400,
        ),
    ];

    for (name, request, expected_status) in invalid_requests {
        let response = http_send(proxy.addr(), request);
        assert_eq!(
            parse_status(&response),
            expected_status,
            "{name} must be rejected before the router's catch-all fallback",
        );
        assert_ne!(
            parse_body(&response),
            "fallback-backend",
            "{name} must never be forwarded to the fallback backend",
        );
    }
}
