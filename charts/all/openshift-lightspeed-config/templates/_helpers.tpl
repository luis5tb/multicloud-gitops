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

{{/*
TEMPORARY A2A bridge -- see values.yaml's appServerPatch comment and
README.md's "Temporary A2A bridge" section. Builds the CONTAINER_ENV_JSON
payload merged into the operator-managed app-server Deployment's named
container; only includes the optional fields when actually set, so leaving
them blank doesn't override anything the operator (or a future native
OLSConfig field) already set.
*/}}
{{- define "openshift-lightspeed-config.appServerPatchEnvJSON" -}}
{{- $a2a := .Values.appServerPatch.a2a -}}
{{- $env := list -}}
{{- if $a2a.keycloakIssuerURL -}}
{{- $env = append $env (dict "name" "A2A_KEYCLOAK_ISSUER_URL" "value" $a2a.keycloakIssuerURL) -}}
{{- end -}}
{{- if $a2a.clusterId -}}
{{- $env = append $env (dict "name" "A2A_CLUSTER_ID" "value" $a2a.clusterId) -}}
{{- end -}}
{{- if $a2a.rpcUrl -}}
{{- $env = append $env (dict "name" "A2A_RPC_URL" "value" $a2a.rpcUrl) -}}
{{- end -}}
{{- if $a2a.keycloakCaBundle -}}
{{- $env = append $env (dict "name" "A2A_KEYCLOAK_CA_BUNDLE" "value" $a2a.keycloakCaBundle) -}}
{{- end -}}
{{- $env = append $env (dict "name" "A2A_INBOUND_AUDIENCE" "value" $a2a.inboundAudience) -}}
{{- $env = append $env (dict "name" "A2A_INBOUND_AZP" "value" $a2a.inboundAzp) -}}
{{- $env = append $env (dict "name" "A2A_EXCHANGE_AUDIENCE" "value" $a2a.exchangeAudience) -}}
{{- $env = append $env (dict "name" "A2A_EXCHANGE_CLIENT_ASSERTION_TYPE" "value" $a2a.exchangeClientAssertionType) -}}
{{- if $a2a.spiffeJwtAudience -}}
{{- $env = append $env (dict "name" "A2A_SPIFFE_JWT_AUDIENCE" "value" $a2a.spiffeJwtAudience) -}}
{{- end -}}
{{- if $a2a.spiffeEndpointSocket -}}
{{- $env = append $env (dict "name" "A2A_SPIFFE_ENDPOINT_SOCKET" "value" $a2a.spiffeEndpointSocket) -}}
{{- end -}}
{{- range .Values.appServerPatch.extraEnv -}}
{{- $env = append $env (dict "name" .name "value" .value) -}}
{{- end -}}
{{- $env | toJson -}}
{{- end -}}

{{- define "openshift-lightspeed-config.appServerPatchImage" -}}
{{- if and .Values.appServerPatch.image.repository .Values.appServerPatch.image.tag -}}
{{- printf "%s:%s" .Values.appServerPatch.image.repository .Values.appServerPatch.image.tag -}}
{{- end -}}
{{- end -}}

{{- define "openshift-lightspeed-config.appServerPatchPodSpec" -}}
serviceAccountName: {{ include "openshift-lightspeed-config.fullname" . }}-appserver-patch
securityContext:
{{ toYaml .Values.securityContext | nindent 2 }}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml . | nindent 2 }}
{{- end }}
restartPolicy: Never
containers:
  - name: patch-appserver
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command:
      - python3
      - -B
      - /opt/app-root/src/patch_appserver.py
    env:
      - name: TARGET_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: DEPLOYMENT_NAME
        value: {{ .Values.appServerPatch.deploymentName | quote }}
      - name: CONTAINER_NAME
        value: {{ .Values.appServerPatch.containerName | quote }}
      - name: CONTAINER_IMAGE
        value: {{ include "openshift-lightspeed-config.appServerPatchImage" . | quote }}
      - name: CONTAINER_ENV_JSON
        value: {{ include "openshift-lightspeed-config.appServerPatchEnvJSON" . | quote }}
      - name: SPIFFE_VOLUME_MOUNT_PATH
        value: {{ .Values.appServerPatch.spiffeWorkloadApiMountPath | quote }}
      - name: POLL_INTERVAL_SECONDS
        value: {{ .Values.appServerPatch.poll.intervalSeconds | quote }}
      - name: POLL_DEADLINE_SECONDS
        value: {{ .Values.appServerPatch.poll.deadlineSeconds | quote }}
      - name: OPERATOR_SUBSCRIPTION_NAME
        value: {{ .Values.appServerPatch.operatorSubscriptionName | quote }}
      - name: OPERATOR_DEPLOYMENT_NAME
        value: {{ .Values.appServerPatch.operatorDeploymentName | quote }}
      - name: OPERATOR_MANAGEMENT_STATE_ANNOTATION
        value: {{ .Values.appServerPatch.operatorManagementStateAnnotation | quote }}
      - name: PYTHONDONTWRITEBYTECODE
        value: "1"
    securityContext:
{{ toYaml .Values.containerSecurityContext | nindent 6 }}
    resources:
{{ toYaml .Values.appServerPatch.resources | nindent 6 }}
    volumeMounts:
      - name: patch-script
        mountPath: /opt/app-root/src/patch_appserver.py
        subPath: patch_appserver.py
        readOnly: true
volumes:
  - name: patch-script
    configMap:
      name: {{ include "openshift-lightspeed-config.fullname" . }}-appserver-patch-script
      defaultMode: 0444
{{- end }}
