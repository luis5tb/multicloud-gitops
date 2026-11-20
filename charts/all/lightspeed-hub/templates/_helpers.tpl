{{- define "lightspeed-hub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lightspeed-hub.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "lightspeed-hub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "lightspeed-hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lightspeed-hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "lightspeed-hub.labels" -}}
helm.sh/chart: {{ include "lightspeed-hub.chart" . }}
{{ include "lightspeed-hub.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "lightspeed-hub.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "lightspeed-hub.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Run namespace (design §8.3 / open question Q4): where AgenticRuns, run-token
Secrets and repo C sandbox pods live, and where the writer Role lands. Empty
adapter.runNamespace means the chart's own release namespace (the
single-namespace shape: adapter + operator + runs together).
*/}}
{{- define "lightspeed-hub.runNamespace" -}}
{{- .Values.adapter.runNamespace | default .Release.Namespace }}
{{- end }}

{{/*
Cluster-domain derivation, same anchor contract as charts/all/praxis-proxy
(see its _helpers.tpl): global.clusterBaseDomain, falling back to the
framework-injected global.localClusterDomain with "apps." stripped. Unlike
praxis-proxy this does NOT fail when unset -- the issuer can also come from
an explicit adapter.keycloak.issuerUrl; the Deployment template fails loudly
when neither is available.
*/}}
{{- define "lightspeed-hub.keycloakIssuerUrl" -}}
{{- if .Values.adapter.keycloak.issuerUrl -}}
{{- .Values.adapter.keycloak.issuerUrl -}}
{{- else -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- $realm := .Values.global.keycloakRealm | default "rca" -}}
{{- if $base -}}
{{- printf "https://keycloak.apps.%s/realms/%s" $base $realm -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "lightspeed-hub.keycloakTokenUrl" -}}
{{- if .Values.adapter.keycloak.tokenUrl -}}
{{- .Values.adapter.keycloak.tokenUrl -}}
{{- else -}}
{{- printf "%s/protocol/openid-connect/token" (trimSuffix "/" (include "lightspeed-hub.keycloakIssuerUrl" .)) -}}
{{- end }}
{{- end }}

{{/*
Agent-card URL: the public Praxis A2A endpoint callers send JSON-RPC to.
praxis-proxy routes discovery to THIS card verbatim, so the URL
must be the Praxis route, never the internal adapter Service. Derived with
the same anchor + default route host as praxis-proxy
(praxis-proxy.apps.<base>/a2a); set adapter.agentCard.url when praxis-proxy
overrides route.host. Empty only when no domain is
known -- and a2a-sdk then cannot select the 0.3 transport (it only maps a
legacy card's protocolVersion onto an interface when url is set).
*/}}
{{- define "lightspeed-hub.agentCardUrl" -}}
{{- if .Values.adapter.agentCard.url -}}
{{- .Values.adapter.agentCard.url -}}
{{- else -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- if $base -}}
{{- printf "https://praxis-proxy.apps.%s/a2a" $base -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "lightspeed-hub.adapterImage" -}}
{{- if .Values.adapter.image.digest -}}
{{- printf "%s@%s" .Values.adapter.image.repository .Values.adapter.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.adapter.image.repository .Values.adapter.image.tag -}}
{{- end }}
{{- end }}

{{- define "lightspeed-hub.operatorImage" -}}
{{- if .Values.agenticOperator.image.digest -}}
{{- printf "%s@%s" .Values.agenticOperator.image.repository .Values.agenticOperator.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.agenticOperator.image.repository .Values.agenticOperator.image.tag -}}
{{- end }}
{{- end }}

{{/*
Adapter serving-cert Secret name: the value of the Service annotation
service.beta.openshift.io/serving-cert-secret-name, into which OpenShift's
service-ca operator mints/rotates the adapter's TLS serving cert
(tls.crt/tls.key). Empty adapter.tls.servingCertSecretName means
<fullname>-serving-cert.
*/}}
{{- define "lightspeed-hub.servingCertSecretName" -}}
{{- default (printf "%s-serving-cert" (include "lightspeed-hub.fullname" .)) .Values.adapter.tls.servingCertSecretName -}}
{{- end }}

{{/*
Adapter port NAME (container port + Service targetPort): https when
adapter.tls.enabled (the default, production shape), http for the
plain-HTTP local-dev override.
*/}}
{{- define "lightspeed-hub.portName" -}}
{{- if .Values.adapter.tls.enabled -}}https{{- else -}}http{{- end -}}
{{- end }}
