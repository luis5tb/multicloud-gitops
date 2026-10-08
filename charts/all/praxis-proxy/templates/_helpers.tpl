{{- define "praxis-proxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "praxis-proxy.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "praxis-proxy.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "praxis-proxy.labels" -}}
app.kubernetes.io/name: {{ include "praxis-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Cluster domain derivation (I6 -- single anchor). See the identical block in
charts/all/keycloak-oidc/templates/_helpers.tpl for the full rationale: the
deployer edits one value (global.clusterBaseDomain, the base domain without the
apps. prefix), or none when the framework injects global.localClusterDomain.
Each derived value stays explicitly overridable per app.
*/}}
{{- define "praxis-proxy.clusterBaseDomain" -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- if not $base -}}
{{- fail "global.clusterBaseDomain (or the framework-injected global.localClusterDomain) must be set: it is the single anchor the Keycloak issuer and public A2A host derive from. Set global.clusterBaseDomain in variants/standalone/values-standalone.yaml." -}}
{{- end -}}
{{- $base -}}
{{- end -}}

{{- define "praxis-proxy.appsDomain" -}}
{{- printf "apps.%s" (include "praxis-proxy.clusterBaseDomain" .) -}}
{{- end -}}

{{- define "praxis-proxy.keycloakRealm" -}}
{{- .Values.global.keycloakRealm | default "rca" -}}
{{- end -}}

{{- define "praxis-proxy.effectiveIssuerUrl" -}}
{{- if .Values.identity.keycloak.issuerUrl -}}{{ .Values.identity.keycloak.issuerUrl }}{{- else -}}{{ printf "https://keycloak.%s/realms/%s" (include "praxis-proxy.appsDomain" .) (include "praxis-proxy.keycloakRealm" .) }}{{- end -}}
{{- end -}}

{{- define "praxis-proxy.effectiveRouteHost" -}}
{{- if .Values.route.host -}}{{ .Values.route.host }}{{- else -}}{{ printf "openshift-lightspeed.%s" (include "praxis-proxy.appsDomain" .) }}{{- end -}}
{{- end -}}

{{- define "praxis-proxy.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end }}

{{- define "praxis-proxy.caSyncPodSpec" -}}
serviceAccountName: {{ include "praxis-proxy.fullname" . }}-ca-sync
securityContext:
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
restartPolicy: Never
containers:
  - name: sync-ingress-ca
    image: {{ .Values.policy.caBundleSync.image | quote }}
    imagePullPolicy: IfNotPresent
    command: ["python3", "-B", "/opt/app-root/src/sync_ingress_ca.py"]
    env:
      - name: SOURCE_NAMESPACE
        value: {{ .Values.policy.caBundleSync.sourceNamespace | quote }}
      - name: SOURCE_CONFIGMAP
        value: {{ .Values.policy.caBundleSync.sourceConfigMap | quote }}
      - name: SOURCE_KEY
        value: {{ .Values.policy.caBundleSync.sourceKey | quote }}
      - name: TARGET_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: TARGET_CONFIGMAP
        value: {{ .Values.policy.caBundleConfigMap.name | quote }}
      - name: BUNDLE_KEY
        value: {{ .Values.policy.caBundleConfigMap.key | quote }}
      - name: TARGET_DEPLOYMENT
        value: {{ include "praxis-proxy.fullname" . | quote }}
      - name: PYTHONDONTWRITEBYTECODE
        value: "1"
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
    resources:
      {{- toYaml .Values.policy.caBundleSync.resources | nindent 6 }}
    volumeMounts:
      - name: sync-script
        mountPath: /opt/app-root/src/sync_ingress_ca.py
        subPath: sync_ingress_ca.py
        readOnly: true
volumes:
  - name: sync-script
    configMap:
      name: {{ include "praxis-proxy.fullname" . }}-ca-sync-script
      defaultMode: 0444
{{- end }}
