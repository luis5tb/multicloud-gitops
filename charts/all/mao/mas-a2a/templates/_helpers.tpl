{{- define "mas-a2a.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mas-a2a.fullname" -}}
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

{{- define "mas-a2a.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mas-a2a.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mas-a2a.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mas-a2a.labels" -}}
helm.sh/chart: {{ include "mas-a2a.chart" . }}
{{ include "mas-a2a.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mas-a2a.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mas-a2a.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "mas-a2a.image" -}}
{{- $repository := required "image.repository must name the built MAS image when a2a.enabled=true" .Values.image.repository }}
{{- $base := $repository }}
{{- if .Values.image.registry }}
{{- $base = printf "%s/%s" .Values.image.registry $repository }}
{{- end }}
{{- if .Values.image.digest }}
{{- printf "%s@%s" $base .Values.image.digest }}
{{- else }}
{{- $tag := required "image.tag must be a pinned, non-latest tag when image.digest is empty" .Values.image.tag }}
{{- if eq (lower $tag) "latest" }}
{{- fail "image.tag=latest is not permitted for an enabled MAS A2A deployment; pin a release tag or digest" }}
{{- end }}
{{- printf "%s:%s" $base $tag }}
{{- end }}
{{- end }}

{{- define "mas-a2a.keycloakIssuerUri" -}}
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
