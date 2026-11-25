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

{{- define "praxis-proxy.hubExchangeTokenUrl" -}}
{{- printf "%s/protocol/openid-connect/token" (trimSuffix "/" (include "praxis-proxy.effectiveIssuerUrl" .)) -}}
{{- end -}}

{{/*
A2A upstream (Task B request leg, disabled by default). Derived host and
endpoint for the hub adapter Service, plus the render-time validation that
gates the whole feature: the target allow-list IS the spokeGateways map,
so enabling the hub gateway with no registered spokes would forward nothing
while looking configured -- fail loudly instead. Validation lives here so
any template touching a2aUpstream (configmap.yaml via files/praxis.yaml,
the CA ConfigMap, the Deployment mount) triggers it.
*/}}

{{- define "praxis-proxy.a2aUpstreamHost" -}}
{{- printf "%s.%s.svc.cluster.local" .Values.a2aUpstream.service .Values.a2aUpstream.namespace -}}
{{- end -}}

{{- define "praxis-proxy.a2aUpstreamEndpoint" -}}
{{- printf "%s:%s" (include "praxis-proxy.a2aUpstreamHost" .) (.Values.a2aUpstream.port | toString) -}}
{{- end -}}

{{- define "praxis-proxy.a2aUpstreamValidation" -}}
{{- if .Values.a2aUpstream.exchangeAtPraxis -}}
{{- /* Hub-gateway A->H exchange: requires the upstream, the policy filter
      (delegate() needs the jwt identity step first), and the exchange
      client contract. */}}
{{- if not .Values.a2aUpstream.enabled -}}
{{- fail "a2aUpstream.exchangeAtPraxis=true requires a2aUpstream.enabled=true (the exchanged token H is forwarded to the hub adapter)" -}}
{{- end -}}
{{- if not .Values.policy.enabled -}}
{{- fail "a2aUpstream.exchangeAtPraxis=true requires policy.enabled=true: delegate() must run after the jwt identity step" -}}
{{- end -}}
{{- if not (and .Values.a2aUpstream.exchange.clientId .Values.a2aUpstream.exchange.audience .Values.a2aUpstream.exchange.secretName) -}}
{{- fail "a2aUpstream.exchangeAtPraxis=true requires a2aUpstream.exchange.clientId (the exchanger client = Token A's audience, e.g. mas-a2a), .audience (Token H's audience, e.g. lightspeed-hub-a2a) and .secretName" -}}
{{- end -}}
{{- end -}}
{{- if .Values.a2aUpstream.externalExchange.enabled -}}
{{- /* External-issuer (SSO) A->H exchange: rides the hub-gateway exchange,
      so it needs everything that needs, plus the external issuers whose
      claim.iss the conditional delegate steps match. */}}
{{- if not .Values.a2aUpstream.exchangeAtPraxis -}}
{{- fail "a2aUpstream.externalExchange.enabled=true requires a2aUpstream.exchangeAtPraxis=true (it adds a second delegator to the hub-gateway A->H exchange)" -}}
{{- end -}}
{{- if or (not .Values.policy.enabled) (not .Values.identity.oidcIssuers) -}}
{{- fail "a2aUpstream.externalExchange.enabled=true requires policy.enabled=true and a non-empty identity.oidcIssuers (the external issuers whose claim.iss selects the jwt-typed delegator)" -}}
{{- end -}}
{{- range .Values.identity.oidcIssuers -}}
{{- if not (regexMatch "^https://[A-Za-z0-9._:/-]+$" (toString .issuerUrl)) -}}
{{- fail (printf "identity.oidcIssuers entry %q must be a plain https:// URL (letters, digits, '.', '_', ':', '/', '-' only): it is interpolated into a single-quoted APL literal in the conditional delegate steps" (toString .issuerUrl)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if .Values.a2aUpstream.enabled -}}
{{- if not .Values.a2aUpstream.targetHeader -}}
{{- fail "a2aUpstream.targetHeader is required when a2aUpstream.enabled is true (must match the lightspeed-hub chart's adapter.targetHeader / the adapter's --target-header flag)" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9-]+$" .Values.a2aUpstream.targetHeader) -}}
{{- fail (printf "a2aUpstream.targetHeader %q must be a valid HTTP header name (letters, digits, '-' only)" .Values.a2aUpstream.targetHeader) -}}
{{- end -}}
{{- if empty .Values.spokeGateways -}}
{{- fail "a2aUpstream.enabled requires at least one spokeGateways entry: the caller x target allow-list IS the spokeGateways map (a caller may only target spokes that have gateways), so an enabled hub gateway with no registered spokes would route nothing while looking configured" -}}
{{- end -}}
{{- if not .Values.a2aUpstream.service -}}
{{- fail "a2aUpstream.service is required when a2aUpstream.enabled is true" -}}
{{- end -}}
{{- if not .Values.a2aUpstream.namespace -}}
{{- fail "a2aUpstream.namespace is required when a2aUpstream.enabled is true" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9]+$" (.Values.a2aUpstream.port | toString)) -}}
{{- fail (printf "a2aUpstream.port must be numeric, got %q" (.Values.a2aUpstream.port | toString)) -}}
{{- end -}}
{{- if not .Values.policy.enabled -}}
{{- fail "a2aUpstream.enabled requires policy.enabled=true: this leg's design is \"Praxis stays the hub gateway\" (validate Token A + azp allow-list BEFORE routing to the adapter); forwarding A2A traffic to the adapter without validation would move the hub gateway, not keep it" -}}
{{- end -}}
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
