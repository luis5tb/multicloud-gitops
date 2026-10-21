{{/*
Generate the name of the Service.
*/}}
{{- define "keycloak.service.name" -}}
{{- if eq .Values.keycloak.tls.serviceServing true }}
{{- printf "%s-service-serving" .Values.keycloak.name }}
{{- else }}
{{- printf "%s-service" .Values.keycloak.name }}
{{- end }}
{{- end }}

{{/*
Generate the hostname for the Ingress.
*/}}

{{- define "keycloak.ingress.hostname" -}}
{{- if or (not .Values.keycloak.ingress.hostname) (eq .Values.keycloak.ingress.hostname "") }}
{{- printf "%s.%s" .Values.keycloak.name .Values.global.localClusterDomain }}
{{- else }}
{{- print .Values.keycloak.ingress.hostname }}
{{- end }}
{{- end }}

{{/*
Generate the lifecycle for the ExternalSecrets resource.
*/}}
{{- define "keycloak.externalSecrets.lifecycle" -}}
refreshPolicy: {{ .local.refreshPolicy | default .global.refreshPolicy }}
refreshInterval: {{ .local.refreshInterval | default .global.refreshInterval }}
{{- end }}

{{/*
Generate the target lifecycle for the ExternalSecrets resource.
*/}}
{{- define "keycloak.externalSecrets.targetLifecycle" -}}
creationPolicy: {{ .local.creationPolicy | default .global.creationPolicy }}
deletionPolicy: {{ .local.deletionPolicy | default .global.deletionPolicy }}
{{- end }}

{{/*
Generate the lifecycle for an extraSecrets entry.
Per-entry fields override global defaults.
*/}}
{{- define "keycloak.extraSecrets.lifecycle" -}}
refreshPolicy: {{ .local.refreshPolicy | default .global.refreshPolicy }}
refreshInterval: {{ .local.refreshInterval | default .global.refreshInterval }}
{{- end }}

{{/*
Generate the target lifecycle for an extraSecrets entry.
Per-entry fields override global defaults.
*/}}
{{- define "keycloak.extraSecrets.targetLifecycle" -}}
creationPolicy: {{ .local.creationPolicy | default .global.creationPolicy }}
deletionPolicy: {{ .local.deletionPolicy | default .global.deletionPolicy }}
{{- end }}