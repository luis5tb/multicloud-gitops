{{- define "keycloak-oidc.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "keycloak-oidc.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- include "keycloak-oidc.name" . }}
{{- end }}
{{- end }}

{{- define "keycloak-oidc.labels" -}}
app.kubernetes.io/name: {{ include "keycloak-oidc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Cluster domain derivation (I6 -- single anchor). The deployer edits exactly one
value, global.clusterBaseDomain (the cluster's base domain WITHOUT the apps.
prefix, e.g. ocp.example.com); every domain-based URL below derives from it, so
the ~9 hand-repeated domain references that used to live in
variants/standalone/values-standalone.yaml are gone. Falls back to the
clustergroup-framework-injected global.localClusterDomain (the apps wildcard
domain) with its apps. prefix stripped, so a deployer can often set nothing at
all. Fails loud at render if neither is resolvable -- reached only when a
domain-derived value is actually needed (i.e. the feature using it is enabled).
Each derived value remains explicitly overridable per app (see the effective*
helpers). Keep this block in sync with the identical derivation in the other
charts (acme-agent, praxis-proxy, openshift-lightspeed-config).
*/}}
{{- define "keycloak-oidc.clusterBaseDomain" -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- if not $base -}}
{{- fail "global.clusterBaseDomain (or the framework-injected global.localClusterDomain) must be set: it is the single anchor the Keycloak issuer, console route, API URL, public A2A host and SPIFFE trust domain all derive from. Set global.clusterBaseDomain in variants/standalone/values-standalone.yaml." -}}
{{- end -}}
{{- $base -}}
{{- end -}}

{{- define "keycloak-oidc.appsDomain" -}}
{{- printf "apps.%s" (include "keycloak-oidc.clusterBaseDomain" .) -}}
{{- end -}}

{{/*
This chart's own keycloak.realm is the authoritative realm name (it names the
KeycloakRealmImport resource and the reconciler's realm), so the issuer derives
its realm segment from it -- not from global.keycloakRealm, which is the anchor
the OTHER charts use. The deployer keeps the two equal (both default "rca").
*/}}
{{- define "keycloak-oidc.keycloakRealm" -}}
{{- .Values.keycloak.realm -}}
{{- end -}}

{{- define "keycloak-oidc.keycloakIssuerURL" -}}
{{- printf "https://keycloak.%s/realms/%s" (include "keycloak-oidc.appsDomain" .) (include "keycloak-oidc.keycloakRealm" .) -}}
{{- end -}}

{{/* Effective values: explicit override wins; otherwise derive from the anchor. */}}
{{- define "keycloak-oidc.effectiveIssuerURL" -}}
{{- if .Values.openshiftOIDC.issuerURL -}}{{ .Values.openshiftOIDC.issuerURL }}{{- else -}}{{ include "keycloak-oidc.keycloakIssuerURL" . }}{{- end -}}
{{- end -}}

{{- define "keycloak-oidc.effectiveConsoleRoute" -}}
{{- if .Values.openshiftOIDC.consoleRoute -}}{{ .Values.openshiftOIDC.consoleRoute }}{{- else -}}{{ printf "https://console-openshift-console.%s" (include "keycloak-oidc.appsDomain" .) }}{{- end -}}
{{- end -}}

{{- define "keycloak-oidc.effectiveTrustDomain" -}}
{{- if .Values.keycloak.spiffeIdentityProvider.trustDomain -}}{{ .Values.keycloak.spiffeIdentityProvider.trustDomain }}{{- else -}}{{ include "keycloak-oidc.appsDomain" . }}{{- end -}}
{{- end -}}

{{/*
External IdP-to-group assignments, serialized as JSON so both the one-shot
realm import and the existing-realm PostSync reconciler consume the same
source of truth. Reconciliation matches by issuer first because the live IdP
alias can drift when KeycloakRealmImport was initially created with another
alias.
*/}}
{{- define "keycloak-oidc.externalGroupMappings" -}}
{{- $mappings := list -}}
{{- range .Values.keycloak.externalOidcProviders -}}
{{- if .hardcodedGroups -}}
{{- $mappings = append $mappings (dict "alias" .alias "issuerUrl" .issuerUrl "groups" .hardcodedGroups) -}}
{{- end -}}
{{- end -}}
{{- $mappings | toJson -}}
{{- end -}}

{{/* Shared managed configuration for imports and existing-realm updates. */}}
{{- define "keycloak-oidc.externalProviderConfig" -}}
{{- $credentials := default dict .credentials -}}
{{- if or $credentials.vaultKey $credentials.existingSecretName -}}
{{- range $field := list "authorizationUrl" "tokenUrl" "jwksUrl" "tokenIntrospectionUrl" "userInfoUrl" -}}
{{- $_ := required (printf "external OIDC provider %s requires %s when credentials are managed" $.alias $field) (index $ $field) -}}
{{- end -}}
{{- end -}}
{{- $config := dict "issuer" (required "external OIDC provider issuerUrl is required" .issuerUrl) "validateSignature" "true" "useJwksUrl" "true" "tokenExchangeEnabled" "true" "clientAuthMethod" (default "client_secret_post" .clientAuthMethod) "defaultScope" (default "openid" .defaultScope) -}}
{{- if hasKey . "allowTokenExchange" -}}
{{- $_ := set $config "tokenExchangeEnabled" (.allowTokenExchange | toString) -}}
{{- end -}}
{{- range $field := list "authorizationUrl" "tokenUrl" "jwksUrl" "tokenIntrospectionUrl" "userInfoUrl" "clientId" "isAccessTokenJWT" -}}
{{- if hasKey $ $field -}}
{{- $_ := set $config $field (index $ $field | toString) -}}
{{- end -}}
{{- end -}}
{{- $config | toJson -}}
{{- end -}}

{{- define "keycloak-oidc.externalProviderSecretName" -}}
{{- $credentials := default dict .provider.credentials -}}
{{- if and $credentials.existingSecretName $credentials.vaultKey -}}
{{- fail "external OIDC credentials: set only one of existingSecretName and vaultKey" -}}
{{- end -}}
{{- if $credentials.existingSecretName -}}
{{- $credentials.existingSecretName -}}
{{- else -}}
{{- printf "%s-idp-%s" (include "keycloak-oidc.fullname" .root) .provider.alias | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "keycloak-oidc.externalProviders" -}}
{{- $providers := list -}}
{{- range $index, $provider := .Values.keycloak.externalOidcProviders -}}
{{- $desired := dict "alias" (required "external OIDC provider alias is required" $provider.alias) "issuerUrl" $provider.issuerUrl "config" (include "keycloak-oidc.externalProviderConfig" $provider | fromJson) "groups" (default list $provider.hardcodedGroups) -}}
{{- if $provider.displayName -}}{{- $_ := set $desired "displayName" $provider.displayName -}}{{- end -}}
{{- $credentials := default dict $provider.credentials -}}
{{- if or $credentials.vaultKey $credentials.existingSecretName -}}
{{- $_ := set $desired "clientIdEnv" (printf "EXTERNAL_IDP_%d_CLIENT_ID" $index) -}}
{{- $_ := set $desired "clientSecretEnv" (printf "EXTERNAL_IDP_%d_CLIENT_SECRET" $index) -}}
{{- end -}}
{{- $providers = append $providers $desired -}}
{{- end -}}
{{- $providers | toJson -}}
{{- end -}}
