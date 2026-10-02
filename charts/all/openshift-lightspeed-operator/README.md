# OpenShift Lightspeed Operator install scaffold

This chart is an **inert, optional OLM install scaffold** for the patched
downstream source snapshot at
[`fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6`](https://github.com/openshift/lightspeed-operator/tree/fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6).
It is not the `OLSConfig` application chart
([`openshift-lightspeed-config`](../openshift-lightspeed-config/)), does not
install or configure an OLSConfig, and does not override any operator-owned
Deployment, Service, MCP configuration, or other operand.

## Gated: no downstream OLM release is available yet

No patched operator image, versioned bundle, catalog image, or catalog digest
has been published. The snapshot's checked-in bundle still identifies itself
as version `1.1.4`. **Do not publish or install the changed downstream contents
under that version.** Build and publish a new versioned bundle/catalog first,
then provide its actual immutable catalog image reference and actual package,
channel, and starting CSV as deployment values.

Both `catalogSource.enabled` and `subscription.enabled` default to `false`.
There is no default catalog image, package, channel, or CSV. Enabling the
Subscription without the chart-managed CatalogSource fails Helm rendering;
enabling the CatalogSource requires an image pinned by a lowercase SHA-256
digest. A tag, floating reference, or placeholder image is rejected. The
immutable reference is a deployment-controlled input, not an assertion that an
image exists or is trusted. The CatalogSource is configured for gRPC and does
not enable registry polling; publish a new digest and update the deployment
values to move to a new catalog.

Example values are intentionally omitted until a real downstream catalog is
published. Do not enable either resource with an example, upstream, or
unverified image. Set `catalogSource.enabled`, `subscription.enabled`,
`catalogSource.image`, and the subscription's actual `packageName`, `channel`,
and `startingCSV` only after release metadata has been reviewed. Choose
`subscription.installPlanApproval` explicitly (`Manual` is the chart default;
`Automatic` is available if deployment policy approves automatic upgrades).

## Verified OLM scope

The pinned CSV in `vendor/lightspeed-operator/bundle/manifests/`
(`lightspeed-operator.clusterserviceversion.yaml`) declares:

| Item | Checked-in value / supported mode |
| --- | --- |
| Bundle package annotation | `lightspeed-operator` |
| Bundle channel annotation | `alpha` (also the annotated default channel) |
| Bundle/CSV version | `1.1.4` — stale upstream metadata; **not valid for publishing the changed downstream snapshot** |
| Supported install mode | `OwnNamespace` only |
| Unsupported install modes | `SingleNamespace`, `MultiNamespace`, `AllNamespaces` |
| Suggested install namespace | `openshift-lightspeed` |

The package and channel above describe the stale checked-in bundle only; this
chart deliberately does **not** default to them. The downstream release may
change package/channel/CSV metadata. Confirm those values against the newly
published bundle/catalog before enabling installation.

For the verified OwnNamespace mode, the generated OperatorGroup targets only
the configured installation namespace. The chart can create that Namespace
and OperatorGroup when the Subscription is enabled. If the parent
clustergroup already owns either resource, set the corresponding
`installNamespace.create` or `operatorGroup.create` to `false`; ensure the
pre-existing OperatorGroup targets exactly the install namespace. The source
namespace (default `openshift-marketplace`) must already exist. Do not create
an all-namespaces OperatorGroup for this bundle.

The separate OLSConfig chart can use the downstream API fields
`spec.ols.mcpServerSecurity`, `spec.ols.a2a`, and immutable
`spec.ols.serviceImage` only after this downstream operator version is built,
published, and installed. Stock upstream operators do not provide these
downstream reconciliation changes. This chart does not build/publish images,
configure SPIFFE, or perform live-cluster validation.

## Values

- `catalogSource.enabled`: create the downstream CatalogSource (default
  `false`).
- `catalogSource.image`: required when enabled; must end in
  `@sha256:<64 lowercase hex characters>`.
- `catalogSource.name` / `catalogSource.namespace`: source identity and
  namespace.
- `subscription.enabled`: create the OLM Subscription (default `false`;
  requires the chart-managed CatalogSource).
- `subscription.packageName`, `channel`, `startingCSV`: required when enabled;
  sourced from the published downstream bundle/catalog, never inferred.
- `subscription.installPlanApproval`: `Manual` or `Automatic`; defaults to
  `Manual`.
- `installNamespace.name`: OLM install namespace, default
  `openshift-lightspeed`; `installNamespace.create` controls Namespace
  creation.
- `operatorGroup.name` / `operatorGroup.create`: OperatorGroup name and whether
  this chart manages it (default enabled with the Subscription).

## Validation

The defaults render no resources and can be checked without cluster access:

```bash
helm lint charts/all/openshift-lightspeed-operator
helm template openshift-lightspeed-operator charts/all/openshift-lightspeed-operator \
  --namespace openshift-lightspeed
```

Rendering with installation enabled requires a real release's immutable
catalog digest and actual OLM identifiers. Successful Helm rendering alone
does not prove that the catalog exists, is trusted, or works on a target
cluster.
