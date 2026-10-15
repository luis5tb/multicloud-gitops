# Agents

This directory contains agents that can be deployed with the multicloud GitOps
pattern. [`acme_agent`](./acme_agent/) is the public A2A coordinator: it
validates a user-supplied target OpenShift cluster API URL, then forwards the
request through the Praxis gateway to OpenShift Lightspeed, which answers
questions and investigates cluster incidents using its OpenShift MCP tools.

OpenShift Lightspeed itself is deployed via the OpenShift Lightspeed Operator
(see `charts/all/openshift-lightspeed-config/`) and an A2A-enabled image built
from the external [`a2a-split` fork](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)
-- there is no agent source directory for it under this path.

See [`AUTHENTICATION.md`](./AUTHENTICATION.md) for the full per-request
authentication flow from the ACME agent's UI through to OpenShift Lightspeed's
exchanged, MCP-scoped request.

The optional inbound MAS A2A service is a separate, dormant application with
its own Keycloak audience and exact caller allow-list. Its trust path and HITL
resume behavior are documented in
[`AUTHENTICATION.md`](./AUTHENTICATION.md#separate-trust-boundary-inbound-mas-a2a)
and its [chart guide](../charts/all/mao/mas-a2a/README.md).
