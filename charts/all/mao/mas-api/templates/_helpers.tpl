{{/*
Expand the name of the chart.
*/}}
{{- define "mas-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "mas-api.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "mas-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "mas-api.labels" -}}
helm.sh/chart: {{ include "mas-api.chart" . }}
{{ include "mas-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "mas-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mas-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "mas-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mas-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
MAS API container image
*/}}
{{- define "mas-api.image" -}}
{{- $base := .Values.image.repository -}}
{{- if .Values.image.registry -}}
{{- $base = printf "%s/%s" .Values.image.registry .Values.image.repository -}}
{{- end -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $base .Values.image.digest -}}
{{- else }}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" $base $tag -}}
{{- end }}
{{- end }}

{{- define "mas-api.keycloakIssuerUri" -}}
{{- if .Values.identity.keycloakIssuerUri -}}
{{- .Values.identity.keycloakIssuerUri -}}
{{- else -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- $realm := .Values.global.keycloakRealm -}}
{{- if and $base $realm -}}
{{- printf "https://keycloak.apps.%s/realms/%s" $base $realm -}}
{{- end -}}
{{- end -}}
{{- end -}}
