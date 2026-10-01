{{- define "openshift-lightspeed-config.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openshift-lightspeed-config.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "openshift-lightspeed-config.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "openshift-lightspeed-config.labels" -}}
app.kubernetes.io/name: {{ include "openshift-lightspeed-config.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
UNCONFIRMED GUESS at the operator-managed app-server pod labels -- see the
appServer.podSelectorLabels comment in values.yaml. Sourced from values
(rather than hardcoded here) so a deployer can correct it without editing
templates once the real labels are confirmed against a live cluster.
*/}}
{{- define "openshift-lightspeed-config.appServerSelectorLabels" -}}
{{- toYaml .Values.appServer.podSelectorLabels }}
{{- end }}

{{- define "openshift-lightspeed-config.keycloakCaSyncPodSpec" -}}
serviceAccountName: {{ include "openshift-lightspeed-config.fullname" . }}-keycloak-ca-sync
securityContext:
{{ toYaml .Values.securityContext | nindent 2 }}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml . | nindent 2 }}
{{- end }}
restartPolicy: Never
containers:
  - name: sync-keycloak-ca
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command:
      - python3
      - -B
      - /opt/app-root/src/sync_keycloak_ca.py
    env:
      - name: SOURCE_NAMESPACE
        value: {{ .Values.identity.keycloak.caBundleSync.sourceNamespace | quote }}
      - name: SOURCE_CONFIGMAP
        value: {{ .Values.identity.keycloak.caBundleSync.sourceConfigMap | quote }}
      - name: SOURCE_KEY
        value: {{ .Values.identity.keycloak.caBundleSync.sourceKey | quote }}
      - name: TARGET_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: TARGET_SECRET
        value: {{ .Values.identity.keycloak.caBundleSecret.name | quote }}
      - name: BUNDLE_KEY
        value: {{ .Values.identity.keycloak.caBundleSecret.key | quote }}
      - name: PYTHONDONTWRITEBYTECODE
        value: "1"
    securityContext:
{{ toYaml .Values.containerSecurityContext | nindent 6 }}
    resources:
{{ toYaml .Values.identity.keycloak.caBundleSync.resources | nindent 6 }}
    volumeMounts:
      - name: sync-script
        mountPath: /opt/app-root/src/sync_keycloak_ca.py
        subPath: sync_keycloak_ca.py
        readOnly: true
volumes:
  - name: sync-script
    configMap:
      name: {{ include "openshift-lightspeed-config.fullname" . }}-keycloak-ca-sync-script
      defaultMode: 0444
{{- end }}
