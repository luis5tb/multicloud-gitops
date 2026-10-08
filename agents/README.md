# Agents

This directory contains agents that can be deployed with the multicloud GitOps
pattern. [`acme_agent`](./acme_agent/) is the public A2A coordinator: it
validates a user-supplied target OpenShift cluster API URL, then forwards the
request through the Praxis gateway to OpenShift Lightspeed, which answers
questions and investigates cluster incidents using its OpenShift MCP tools.

OpenShift Lightspeed itself is deployed via the OpenShift Lightspeed Operator
(see `charts/all/openshift-lightspeed-config/`) and a vendored, A2A-enabled
copy of its service (`vendor/lightspeed-service/`) -- there is no agent
source directory for it under this path.

See [`AUTHENTICATION.md`](./AUTHENTICATION.md) for the full per-request
authentication flow from the ACME agent's UI through to OpenShift Lightspeed's
exchanged, MCP-scoped request.
