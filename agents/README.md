# Agents

This directory contains agents that can be deployed with the multicloud GitOps
pattern. The first agent is [`rca_agent`](./rca_agent/), an A2A endpoint that
submits analysis-only `AgenticRun` custom resources to the OpenShift Lightspeed
Agentic operator and returns the resulting root-cause analysis proposals.

