# Multicloud Gitops

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

[Live build status](https://validatedpatterns.io/ci/?pattern=mcgitops)

## Start Here

If you've followed a link to this repository, but are not really sure what it contains
or how to use it, head over to [Multicloud GitOps](https://validatedpatterns.io/patterns/multicloud-gitops/)
for additional context and installation instructions

## Rationale

The goal for this pattern is to:

* Use a GitOps approach to manage hybrid and multi-cloud deployments across both public and private clouds.
* Enable cross-cluster governance and application lifecycle management.
* Securely manage secrets across the deployment.


## Deployment

The steps are the next:

* Copy the secrets:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ```

* Install the pattern:

    ```bash
    ./pattern.sh make install
    ```

* If secrets are added/modified after installation then:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
    ```

## Lightspeed Agentic Operator

See the [lightspeed-agentic-operator chart README](charts/all/lightspeed-agentic-operator/README.md)
for instructions on configuring, using, and testing the operator.

## MAO Configuration

The standalone variant deploys the MAO runtime from
[`uie-mas-hosted`](https://gitlab.cee.redhat.com/ai_tools/uie-mas-hosted) into
the `mao` namespace. It is split into five Argo CD applications so the
components can be upgraded independently:

- MongoDB, with persistent storage
- Redis, for event streaming and HITL overrides
- Temporal, for workflow execution
- `mas-worker`, the Temporal worker
- `mas-api`, exposed through an OpenShift Route

The chart values are copied under
[`charts/all/mao`](</home/ltomasbo/git_repos/multicloud-gitops/charts/all/mao>).
The default Pattern configuration is intended for development:

- MongoDB and Redis are internal, single-instance services.
- MongoDB client TLS is disabled because the bundled MongoDB chart does not
  configure MongoDB certificates or TLS server mode. This matches the source
  repository's Docker Compose stack. The source repository's Helm/OpenShift
  values set `MONGODB_TLS=true`, but its bundled MongoDB deployment does not
  enable server-side TLS; copying that setting unchanged would make the client
  connect to a plaintext server with TLS and fail.
- MAO uses `providerMode: dev`, which accepts bearer tokens permissively. Do
  not use this mode for an internet-facing deployment.
- Langfuse is disabled unless its Secret is configured.

For a production deployment, override the MAS API and worker values in
`variants/standalone/values-standalone.yaml` or with an application-specific
values file:

```yaml
overrides:
  - name: identity.providerMode
    value: prod
  - name: identity.ssoIssuerUri
    value: https://sso.example.com/realms/example
  - name: identity.ssoAudience
    value: api.mas
  - name: identity.adminAllowedUsers
    value: '["user@example.com"]'
```

Enable MongoDB TLS only when MongoDB is deployed with matching certificates
and server-side TLS configuration. Adjust the MongoDB storage class, image
references, API/worker replicas, and Route settings through the component
values as needed.
