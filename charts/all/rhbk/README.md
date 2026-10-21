# rhbk

<!-- markdownlint-disable MD013 -->

![Version: 0.1.0](https://img.shields.io/badge/Version-0.1.0-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square)

<!-- markdownlint-enable MD013 -->

<!-- markdownlint-disable MD013 -->

Deploys RHBK

<!-- markdownlint-enable MD013 -->

This chart is used to serve as the template for Validated Patterns Charts

## Notable changes

**Homepage:** <https://github.com/validatedpatterns/rhbk-chart>

## Maintainers

| Name                    | Email                                | Url |
| ----------------------- | ------------------------------------ | --- |
| Validated Patterns Team | <validatedpatterns@googlegroups.com> |     |

<!-- markdownlint-disable MD013 MD034 MD060 -->

## Values

| Key                             | Type   | Default                                                                                                                                                                                                                                                                                                                                                                                                         | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| defaultDenyNetworkPolicy        | object | false                                                                                                                                                                                                                                                                                                                                                                                                           | Default-deny NetworkPolicy for the keycloak namespace. When enabled, deploys a namespace-wide NetworkPolicy that blocks all ingress and egress for pods without an explicit allow policy. Patterns that need zero-trust network isolation should enable this and provide per-pod allow rules via networkPolicy.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| externalSecrets                 | object | `{"adminUser":{"metadata":{},"targetMetadata":{}},"postgresqlDb":{"metadata":{},"targetMetadata":{}}}`                                                                                                                                                                                                                                                                                                          | Per-ExternalSecret lifecycle and metadata overrides. Only adminUser and postgresqlDb are managed directly by this chart. All other secrets should use keycloak.extraSecrets.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| global.creationPolicy           | string | `"Owner"`                                                                                                                                                                                                                                                                                                                                                                                                       |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.deletionPolicy           | string | `"Retain"`                                                                                                                                                                                                                                                                                                                                                                                                      |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.localClusterDomain       | string | `"apps.example.com"`                                                                                                                                                                                                                                                                                                                                                                                            |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.refreshInterval          | string | `"1h0m0s"`                                                                                                                                                                                                                                                                                                                                                                                                      |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.refreshPolicy            | string | `"Periodic"`                                                                                                                                                                                                                                                                                                                                                                                                    |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.secretStore.kind         | string | `"ClusterSecretStore"`                                                                                                                                                                                                                                                                                                                                                                                          |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| global.secretStore.name         | string | `"vault-backend"`                                                                                                                                                                                                                                                                                                                                                                                               |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| keycloak.adminUser              | object | `{"enabled":true,"passwordVaultKey":"","secretName":"keycloak-admin-user","username":"admin"}`                                                                                                                                                                                                                                                                                                                  | Keycloak admin bootstrap user. Creates an ExternalSecret that pulls the admin password from the configured secret store.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| keycloak.extraSecrets           | list   | `[]`                                                                                                                                                                                                                                                                                                                                                                                                            | Extra ExternalSecrets. Each entry creates an ExternalSecret resource that pulls data from the configured secret store into a Kubernetes Secret. Use this for user passwords, OIDC client secrets, or any other secrets that your realm configuration requires. Example: extraSecrets: - name: my-users targetName: my-users-secret data: - secretKey: user_password remoteRef: key: secret/data/my/path property: user-password templateData: user-password: "{{ .user_password }}"                                                                                                                                                                                                                                                                              |
| keycloak.ingress                | object | `{"enabled":true,"hostname":"","service":"keycloak-service-trusted","termination":"reencrypt"}`                                                                                                                                                                                                                                                                                                                 | Keycloak Ingress (OpenShift Route).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| keycloak.name                   | string | `"keycloak"`                                                                                                                                                                                                                                                                                                                                                                                                    | Keycloak instance name. Used for pod labels, service names, and hostname generation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| keycloak.postgresqlDb           | object | `{"database":"keycloak","passwordVaultKey":"","secretName":"postgresql-db","username":"keycloak"}`                                                                                                                                                                                                                                                                                                              | PostgreSQL database credentials. Creates an ExternalSecret that pulls the DB password from the configured secret store.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| keycloak.realmPlaceholders      | object | `{}`                                                                                                                                                                                                                                                                                                                                                                                                            | Realm import placeholders. Maps placeholder variables (used as ${VAR} in realm definitions) to Kubernetes Secret references. Applied to all realm imports. Example: realmPlaceholders: MY_USER_PASSWORD: secret: name: my-users-secret key: user-password                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| keycloak.realms                 | list   | `[]`                                                                                                                                                                                                                                                                                                                                                                                                            | Keycloak realm definitions. Each entry is a full Keycloak realm representation that will be rendered as a KeycloakRealmImport CR. Consumers define their realm structure (clients, users, roles, scopes, identity providers) here. Example: realms: - realm: my-realm enabled: true displayName: "My Realm" clients: [...] users: [...]                                                                                                                                                                                                                                                                                                                                                                                                                          |
| keycloak.spiffeIdentityProvider | object | `{"config":{"alias":"spiffe","config":{"authorizationUrl":"","clientId":"keycloak","clientSecret":"unused","issuer":"","jwksUrl":"","supportsClientAssertionReuse":"true","supportsClientAssertions":"true","syncMode":"LEGACY","tokenUrl":"","useJwksUrl":"true","validateSignature":"true"},"displayName":"SPIFFE Workload Identity","enabled":true,"hideOnLogin":true,"providerId":"oidc"},"enabled":false}` | SPIFFE Identity Provider for Federated Client Authentication. Requires RHBK 26.4+ with Technology Preview features: spiffe + client-auth-federated (automatically enabled in keycloak.yaml when this is enabled). Uses an OIDC provider type (not Keycloak's native SPIFFE provider) because the ZTWIM operator forces SpireServer.jwtIssuer to be an HTTPS URL, so JWT SVIDs contain iss: "https://spire-spiffe-oidc-discovery-provider.<domain>". Keycloak's native SPIFFE IdP rejects this (expects spiffe:// URI). The OIDC provider matches the HTTPS issuer, enabling Keycloak's federated-jwt client authenticator to resolve clients by iss+sub without requiring client_id. Reference: https://www.keycloak.org/2026/01/federated-client-authentication |
| keycloak.tls                    | object | `{"secret":"keycloak-tls","serviceServing":true}`                                                                                                                                                                                                                                                                                                                                                               | TLS configuration for the Keycloak instance.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| networkPolicy                   | object | `{"keycloak":{"egress":[],"enabled":false},"operator":{"egress":[],"enabled":false,"ingress":[]},"postgresql":{"egress":[],"enabled":false,"ingress":[]},"realmImport":{"egress":[],"enabled":false,"podSelector":{"app":"keycloak-realm-import"}}}`                                                                                                                                                            | Per-pod NetworkPolicy rules for keycloak, PostgreSQL, and operator pods. Only effective when defaultDenyNetworkPolicy is enabled. The RHBK operator manages its own ingress policy for keycloak pods (keycloak-network-policy) — these templates add egress rules for keycloak and full ingress/egress rules for PostgreSQL and operator pods.                                                                                                                                                                                                                                                                                                                                                                                                                   |

<!-- markdownlint-enable MD013 MD034 MD060 -->

---

Autogenerated from chart metadata using [helm-docs v1.14.2](https://github.com/norwoodj/helm-docs/releases/v1.14.2)

## Network Policies

This chart supports deploying Kubernetes NetworkPolicies for network isolation
in the Keycloak namespace. Three layers are available:

### Default-deny policy

A namespace-wide default-deny NetworkPolicy that blocks all ingress and egress
traffic for every pod in the namespace unless an explicit allow policy exists.
Enable it by setting:

```yaml
defaultDenyNetworkPolicy:
  enabled: true
```

### Operator-managed ingress policy

The RHBK operator automatically creates and manages a `keycloak-network-policy`
that controls ingress to keycloak pods (ports 8443, 9000, and JGroups 7800/57800).
This policy is owned by the operator and should not be modified — the operator
will revert any changes.

### Per-pod allow rules

When the default-deny policy is enabled, additional NetworkPolicy templates
allow defining fine-grained rules for each pod type:

- `networkPolicy.keycloak` — egress rules for keycloak pods (ingress is
  managed by the operator policy above)
- `networkPolicy.postgresql` — ingress and egress rules for PostgreSQL pods
- `networkPolicy.operator` — ingress and egress rules for rhbk-operator pods

Example — allow keycloak egress to DNS and PostgreSQL, PostgreSQL
ingress from keycloak, and operator egress to Kubernetes API:

```yaml
defaultDenyNetworkPolicy:
  enabled: true

networkPolicy:
  keycloak:
    enabled: true
    egress:
      - ports:
          - protocol: UDP
            port: 5353
          - protocol: TCP
            port: 5353
        to:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: openshift-dns
      - ports:
          - protocol: TCP
            port: 5432
        to:
          - podSelector:
              matchLabels:
                app: postgresql-db
  postgresql:
    enabled: true
    ingress:
      - ports:
          - protocol: TCP
            port: 5432
        from:
          - podSelector:
              matchLabels:
                app: keycloak
    egress:
      - ports:
          - protocol: UDP
            port: 5353
          - protocol: TCP
            port: 5353
        to:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: openshift-dns
  operator:
    enabled: true
    egress:
      - ports:
          - protocol: TCP
            port: 443
          - protocol: TCP
            port: 6443
```

Patterns can supply these values via `extraValueFiles` in their
`values-hub.yaml` to keep network policy configuration separate from the main
chart values.
