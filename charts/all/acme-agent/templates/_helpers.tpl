{{- define "acme-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "acme-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "acme-agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "acme-agent.labels" -}}
helm.sh/chart: {{ include "acme-agent.chart" . }}
{{ include "acme-agent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "acme-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "acme-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "acme-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "acme-agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/*
Cluster domain derivation (I6 -- single anchor). See the identical block in
charts/all/keycloak-oidc/templates/_helpers.tpl for the full rationale: the
deployer edits one value (global.clusterBaseDomain, base domain without the
apps. prefix), or none when the framework injects global.localClusterDomain.
Each derived value stays explicitly overridable per app. These helpers are only
reached when the relevant feature is enabled (keycloak auth mode, SPIFFE
ClusterSPIFFEID, a remote agent), so the fail-if-unresolvable stays dormant for
the generic chart defaults.
*/}}
{{- define "acme-agent.clusterBaseDomain" -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- if not $base -}}
{{- fail "global.clusterBaseDomain (or the framework-injected global.localClusterDomain) must be set: it is the single anchor the Keycloak issuer, SPIFFE trust domain and public A2A endpoint derive from. Set global.clusterBaseDomain in variants/standalone/values-standalone.yaml." -}}
{{- end -}}
{{- $base -}}
{{- end -}}

{{- define "acme-agent.appsDomain" -}}
{{- printf "apps.%s" (include "acme-agent.clusterBaseDomain" .) -}}
{{- end -}}

{{- define "acme-agent.keycloakRealm" -}}
{{- .Values.global.keycloakRealm | default "rca" -}}
{{- end -}}

{{- define "acme-agent.effectiveIssuerUrl" -}}
{{- if .Values.auth.keycloak.issuerUrl -}}{{ .Values.auth.keycloak.issuerUrl }}{{- else -}}{{ printf "https://keycloak.%s/realms/%s" (include "acme-agent.appsDomain" .) (include "acme-agent.keycloakRealm" .) }}{{- end -}}
{{- end -}}

{{/*
KEYCLOAK_TOKEN_URL is rendered unconditionally, so only derive (which reaches
the domain anchor) in keycloak auth mode; otherwise stay empty, matching the
previous default and keeping the generic chart render anchor-free.
*/}}
{{- define "acme-agent.effectiveTokenUrl" -}}
{{- if .Values.auth.keycloak.tokenUrl -}}{{ .Values.auth.keycloak.tokenUrl }}{{- else if eq .Values.auth.mode "keycloak" -}}{{ printf "%s/protocol/openid-connect/token" (trimSuffix "/" (include "acme-agent.effectiveIssuerUrl" .)) }}{{- end -}}
{{- end -}}

{{- define "acme-agent.effectiveTrustDomain" -}}
{{- if .Values.identity.clusterSpiffeID.trustDomain -}}{{ .Values.identity.clusterSpiffeID.trustDomain }}{{- else -}}{{ include "acme-agent.appsDomain" . }}{{- end -}}
{{- end -}}

{{- define "acme-agent.publicA2AHost" -}}
{{- printf "openshift-lightspeed.%s" (include "acme-agent.appsDomain" .) -}}
{{- end -}}

{{/*
Projects a2a.remoteAgents, filling any entry whose endpoint is empty with the
derived public A2A URL (https://openshift-lightspeed.apps.<base>). Lets the
clustergroup override omit the domain-bearing endpoint and supply only
name/description. Returns the JSON array consumed as REMOTE_A2A_AGENTS_JSON.
*/}}
{{- define "acme-agent.remoteAgentsJSON" -}}
{{- $root := . -}}
{{- $out := list -}}
{{- range .Values.a2a.remoteAgents -}}
{{- $agent := deepCopy . -}}
{{- if not $agent.endpoint -}}{{- $_ := set $agent "endpoint" (printf "https://%s" (include "acme-agent.publicA2AHost" $root)) -}}{{- end -}}
{{- $out = append $out $agent -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- define "acme-agent.caSyncPodSpec" -}}
serviceAccountName: {{ include "acme-agent.fullname" . }}-ca-sync
securityContext:
{{ toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . | nindent 2 }}
{{- end }}
restartPolicy: Never
containers:
  - name: sync-ingress-ca
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command: ["python3", "-B", "/opt/app-root/src/sync_ingress_ca.py"]
    env:
      - name: SOURCE_NAMESPACE
        value: {{ .Values.auth.caBundleSync.sourceNamespace | quote }}
      - name: SOURCE_CONFIGMAP
        value: {{ .Values.auth.caBundleSync.sourceConfigMap | quote }}
      - name: SOURCE_KEY
        value: {{ .Values.auth.caBundleSync.sourceKey | quote }}
      - name: TARGET_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: TARGET_CONFIGMAP
        value: {{ .Values.auth.caBundleConfigMap.name | quote }}
      - name: BUNDLE_KEY
        value: {{ .Values.auth.caBundleConfigMap.key | quote }}
      - name: TARGET_DEPLOYMENT
        value: {{ include "acme-agent.fullname" . | quote }}
      - name: PYTHONDONTWRITEBYTECODE
        value: "1"
    securityContext:
{{ toYaml .Values.securityContext | nindent 6 }}
    resources:
{{ toYaml .Values.auth.caBundleSync.resources | nindent 6 }}
    volumeMounts:
      - name: sync-script
        mountPath: /opt/app-root/src/sync_ingress_ca.py
        subPath: sync_ingress_ca.py
        readOnly: true
volumes:
  - name: sync-script
    configMap:
      name: {{ include "acme-agent.fullname" . }}-ca-sync-script
      defaultMode: 0444
{{- end }}
