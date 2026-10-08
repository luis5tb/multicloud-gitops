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
