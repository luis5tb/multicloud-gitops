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
{{- if .Values.route.host -}}{{ .Values.route.host }}{{- else -}}{{ printf "praxis-proxy.%s" (include "praxis-proxy.appsDomain" .) }}{{- end -}}
{{- end -}}

{{- define "praxis-proxy.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end }}

{{/*
Spoke gateways (Phase 2 B1, proposed). Per-cluster entries are the keys of
the spokeGateways map, which is PURELY per-spoke (literally {} by default);
shared cross-spoke settings live in the top-level spokeGatewayConfig block
(exchangeClient, networkPolicy, servingCA). No reserved sibling keys are
skipped anywhere: a spoke named like a former reserved key renders (or
fails validation) as a spoke.
*/}}

{{- define "praxis-proxy.spokeGatewayName" -}}
{{- printf "praxis-spoke-%s" . -}}
{{- end -}}

{{/*
Per-spoke validation + shared derived values. Sits in its own template file
(spoke-gateway-configmap.yaml) so it only runs when gateways are configured.
This helper returns the Keycloak token endpoint for the exchange, derived
from the effective issuer URL when not explicitly overridden. An explicitly
overridden tokenUrl must be https:// — the endpoint carries the exchange
client's secret and the exchanged token U, so a plaintext leg would expose
credentials in transit (mirrors the remoteAPIServer https-only check above).
*/}}
{{- define "praxis-proxy.spokeExchangeTokenUrl" -}}
{{- if .Values.spokeGatewayConfig.exchangeClient.tokenUrl -}}
{{- if not (hasPrefix "https://" .Values.spokeGatewayConfig.exchangeClient.tokenUrl) -}}
{{- fail (printf "spokeGatewayConfig.exchangeClient.tokenUrl must be an https:// URL, got %q: the token endpoint carries the exchange client secret and the exchanged token in transit (see the remoteAPIServer https-only rule)" .Values.spokeGatewayConfig.exchangeClient.tokenUrl) -}}
{{- end -}}
{{- .Values.spokeGatewayConfig.exchangeClient.tokenUrl -}}
{{- else -}}
{{- printf "%s/protocol/openid-connect/token" (trimSuffix "/" (include "praxis-proxy.effectiveIssuerUrl" .)) -}}
{{- end -}}
{{- end -}}

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
      - name: INCLUDE_SYSTEM_ROOTS
        value: {{ .Values.policy.caBundleSync.includeSystemRoots | quote }}
      - name: SYSTEM_ROOTS_PATH
        value: {{ .Values.policy.caBundleSync.systemRootsPath | quote }}
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

{{/*
Serving-CA sync bridge (B1): repo C's gatewayCASecret expects a Secret with
key ca.crt (controller/agenticrun/gateway.go), but the service CA only
arrives via a ConfigMap (inject-cabundle, key service-ca.crt). This pod
spec runs the stdlib-only Python copier; the bootstrap Job and CronJob in
spoke-ca-sync-{job,cronjob}.yaml include it. The Secret itself is created
by the job (not rendered as a manifest, so Argo never fights the job's
writes).
*/}}
{{- define "praxis-proxy.spokeCaSyncPodSpec" -}}
serviceAccountName: {{ include "praxis-proxy.fullname" . }}-spoke-ca-sync
securityContext:
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
restartPolicy: Never
containers:
  - name: sync-service-ca
    image: {{ .Values.spokeGatewayConfig.servingCA.image | quote }}
    imagePullPolicy: IfNotPresent
    command: ["python3", "-B", "/opt/app-root/src/sync_service_ca.py"]
    env:
      - name: SOURCE_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: SOURCE_CONFIGMAP
        value: {{ .Values.spokeGatewayConfig.servingCA.sourceConfigMap | quote }}
      - name: SOURCE_KEY
        value: {{ .Values.spokeGatewayConfig.servingCA.sourceKey | quote }}
      - name: TARGET_NAMESPACE
        value: {{ .Values.spokeGatewayConfig.servingCA.targetNamespace | default .Release.Namespace | quote }}
      - name: TARGET_SECRET
        value: {{ .Values.spokeGatewayConfig.servingCA.secretName | quote }}
      - name: TARGET_KEY
        value: {{ .Values.spokeGatewayConfig.servingCA.secretKey | quote }}
      - name: PYTHONDONTWRITEBYTECODE
        value: "1"
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
    resources:
      {{- toYaml .Values.spokeGatewayConfig.servingCA.resources | nindent 6 }}
    volumeMounts:
      - name: sync-script
        mountPath: /opt/app-root/src/sync_service_ca.py
        subPath: sync_service_ca.py
        readOnly: true
volumes:
  - name: sync-script
    configMap:
      name: {{ include "praxis-proxy.fullname" . }}-spoke-ca-sync-script
      defaultMode: 0444
{{- end }}
