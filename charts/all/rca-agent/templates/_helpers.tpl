{{- define "rca-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "rca-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "rca-agent.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "rca-agent.labels" -}}
app.kubernetes.io/name: {{ include "rca-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "rca-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "rca-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "rca-agent.keycloakCaSyncPodSpec" -}}
serviceAccountName: {{ include "rca-agent.fullname" . }}-keycloak-ca-sync
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
      - python
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
      - name: TARGET_CONFIGMAP
        value: {{ .Values.identity.keycloak.caBundleConfigMap.name | quote }}
      - name: BUNDLE_KEY
        value: {{ .Values.identity.keycloak.caBundleConfigMap.key | quote }}
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
      name: {{ include "rca-agent.fullname" . }}-keycloak-ca-sync-script
      defaultMode: 0444
{{- end }}
