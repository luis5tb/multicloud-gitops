# MAO runtime charts

These charts package the runtime components from `uie-mas-hosted` for use by
the Multicloud GitOps Pattern:

- `mongo` — persistent MongoDB storage
- `redis` — internal Redis Streams storage
- `temporal` — internal Temporal server
- `mas-worker` — Temporal workflow worker
- `mas-api` — externally reachable MAS API

The Pattern deploys these charts into the `mao` namespace. The charts are
kept as separate applications so each component can be upgraded or scaled
independently. The default values are suitable for a development installation;
set `identity.providerMode` to `prod` and configure the SSO values before
exposing the API to users.

The source repository also contains a Vault Secrets Operator chart. It is not
deployed here because the Pattern already installs Vault and the Validated
Patterns `vault-backend` ClusterSecretStore through ESO. The runtime defaults
create local connection Secrets for MongoDB and Redis, so the MAO stack does
not require an additional secret operator.

The source implementation is maintained in the private
[`uie-mas-hosted`](https://gitlab.cee.redhat.com/ai_tools/uie-mas-hosted)
repository. Keep the copied chart templates aligned when the source chart
changes.
