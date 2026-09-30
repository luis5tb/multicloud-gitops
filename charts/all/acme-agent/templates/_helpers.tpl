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
