# Agents

This directory contains agents that can be deployed with the multicloud GitOps
pattern. [`acme_agent`](./acme_agent/) is the public A2A coordinator: it
delegates each request to the MAO workflow, which selects the appropriate
specialist agents and owns target-cluster selection and validation.

See [`AUTHENTICATION.md`](./AUTHENTICATION.md) for the full per-request
authentication flow from the ACME agent's UI through to the downstream
services.

The optional inbound MAS A2A service is a separate, dormant application with
its own Keycloak audience and exact caller allow-list. Its trust path and HITL
resume behavior are documented in
[`AUTHENTICATION.md`](./AUTHENTICATION.md#separate-trust-boundary-inbound-mas-a2a)
and its [chart guide](../charts/all/mao/mas-a2a/README.md).
