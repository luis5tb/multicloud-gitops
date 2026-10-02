# Agents

This directory contains agents that can be deployed with the multicloud GitOps
pattern. [`acme_agent`](./acme_agent/) is the A2A coordinator and browser entry
point. The greenfield target is ACME → Praxis → OpenShift Lightspeed, with the
Lightspeed Operator managing the service and its OpenShift MCP backend. The
former RCA agent and its standalone MCP chart have been retired from this
repository.

See [`AUTHENTICATION.md`](./AUTHENTICATION.md) for the intended per-request
Keycloak token validation/exchange flow and its live-deployment gates.
