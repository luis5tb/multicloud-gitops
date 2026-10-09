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
Cluster domain derivation (I6 -- single anchor). See the identical block in
charts/all/keycloak-oidc/templates/_helpers.tpl for the full rationale: the
deployer edits one value (global.clusterBaseDomain, base domain without the
apps. prefix), or none when the framework injects global.localClusterDomain.
Each derived value stays explicitly overridable per app. Only reached when
appServerPatch / clusterSpiffeID is enabled, so the fail-if-unresolvable stays
dormant for the generic chart defaults.
*/}}
{{- define "openshift-lightspeed-config.clusterBaseDomain" -}}
{{- $base := .Values.global.clusterBaseDomain | default (trimPrefix "apps." (.Values.global.localClusterDomain | default "")) -}}
{{- if not $base -}}
{{- fail "global.clusterBaseDomain (or the framework-injected global.localClusterDomain) must be set: it is the single anchor the Keycloak issuer, SPIFFE trust domain and public A2A endpoint derive from. Set global.clusterBaseDomain in variants/standalone/values-standalone.yaml." -}}
{{- end -}}
{{- $base -}}
{{- end -}}

{{- define "openshift-lightspeed-config.appsDomain" -}}
{{- printf "apps.%s" (include "openshift-lightspeed-config.clusterBaseDomain" .) -}}
{{- end -}}

{{- define "openshift-lightspeed-config.keycloakRealm" -}}
{{- .Values.global.keycloakRealm | default "rca" -}}
{{- end -}}

{{- define "openshift-lightspeed-config.effectiveKeycloakIssuerURL" -}}
{{- if .Values.appServerPatch.a2a.keycloakIssuerURL -}}{{ .Values.appServerPatch.a2a.keycloakIssuerURL }}{{- else -}}{{ printf "https://keycloak.%s/realms/%s" (include "openshift-lightspeed-config.appsDomain" .) (include "openshift-lightspeed-config.keycloakRealm" .) }}{{- end -}}
{{- end -}}

{{- define "openshift-lightspeed-config.effectiveRpcUrl" -}}
{{- /* OLS's A2A JSON-RPC lives at POST /a2a (luis5tb/lightspeed-service@a2a); the agent card must advertise that path. */ -}}
{{- if .Values.appServerPatch.a2a.rpcUrl -}}{{ .Values.appServerPatch.a2a.rpcUrl }}{{- else -}}{{ printf "https://openshift-lightspeed.%s/a2a" (include "openshift-lightspeed-config.appsDomain" .) }}{{- end -}}
{{- end -}}

{{/*
Normalize appServerPatch.a2a.enabled across bool and string clustergroup
overrides ("true"/"false"). Non-empty strings other than an explicit falsey
value count as enabled -- matching how this chart already treats other
enable flags set via values-standalone's `value: "true"` form.
*/}}
{{- define "openshift-lightspeed-config.a2aEnabled" -}}
{{- $v := .Values.appServerPatch.a2a.enabled -}}
{{- if kindIs "bool" $v -}}{{- if $v -}}true{{- else -}}false{{- end -}}
{{- else -}}
{{- $s := $v | toString | lower -}}
{{- if or (eq $s "false") (eq $s "0") (eq $s "no") (eq $s "") -}}false{{- else -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{- define "openshift-lightspeed-config.effectiveTrustDomain" -}}
{{- if .Values.identity.clusterSpiffeID.trustDomain -}}{{ .Values.identity.clusterSpiffeID.trustDomain }}{{- else -}}{{ include "openshift-lightspeed-config.appsDomain" . }}{{- end -}}
{{- end -}}

{{/*
VALIDATION-ONLY cluster-id derivation. This is the single Helm implementation of
derive_cluster_id (AGENTS.md), used only to re-derive the expected id from an
olsClusters entry's apiURL and fail the render if the hand-authored map key
doesn't match -- it never GENERATES a key (acme/praxis consume the authored key,
never derive it), so there is no three-way drift hazard. Algorithm must match
acme_agent.cluster_registry.derive_cluster_id exactly: f"{host}:{port}" with
lowercase host and every '.'/':' replaced by '-'. The apiURL is scheme://host:port,
so stripping the scheme already yields "host:port".
*/}}
{{- define "openshift-lightspeed-config.deriveClusterId" -}}
{{- $hostport := . | trimSuffix "/" | trimPrefix "https://" | trimPrefix "http://" -}}
{{- $hostport | lower | replace "." "-" | replace ":" "-" -}}
{{- end -}}

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
{{- /* A2A routes are gated off by default in the OLS image (a2a.enabled / A2A_ENABLED); this bridge exists to turn them on. */ -}}
{{- $env = append $env (dict "name" "A2A_ENABLED" "value" (include "openshift-lightspeed-config.a2aEnabled" .)) -}}
{{- /* keycloakIssuerURL and rpcUrl are derived from global.clusterBaseDomain when not explicitly set (see effective* helpers); both A2A_* vars are required, so always emit them. */ -}}
{{- $env = append $env (dict "name" "A2A_KEYCLOAK_ISSUER_URL" "value" (include "openshift-lightspeed-config.effectiveKeycloakIssuerURL" .)) -}}
{{- if $a2a.clusterId -}}
{{- $env = append $env (dict "name" "A2A_CLUSTER_ID" "value" $a2a.clusterId) -}}
{{- end -}}
{{- $env = append $env (dict "name" "A2A_RPC_URL" "value" (include "openshift-lightspeed-config.effectiveRpcUrl" .)) -}}
{{- if $a2a.keycloakCaBundle -}}
{{- $env = append $env (dict "name" "A2A_KEYCLOAK_CA_BUNDLE" "value" $a2a.keycloakCaBundle) -}}
{{- end -}}
{{- $env = append $env (dict "name" "A2A_INBOUND_AUDIENCE" "value" $a2a.inboundAudience) -}}
{{- /* inboundAzp is a list; render the allowed caller(s) comma-joined, no spaces (the contract the OLS A2A_INBOUND_AZP parser expects). */ -}}
{{- $env = append $env (dict "name" "A2A_INBOUND_AZP" "value" (join "," $a2a.inboundAzp)) -}}
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
      # Provisioning gate: wait for this operator-managed postgres Deployment to
      # be available before stopping the operator (proves the bootstrap Secret
      # exists and postgres is up), and un-pause a previously-frozen operator to
      # recover. Empty disables the gate (old, race-prone behavior).
      - name: POSTGRES_DEPLOYMENT_NAME
        value: {{ .Values.appServerPatch.postgresDeploymentName | quote }}
      - name: OPERATOR_REPLICAS
        value: {{ .Values.appServerPatch.operatorReplicas | quote }}
      - name: PROVISION_DEADLINE_SECONDS
        value: {{ .Values.appServerPatch.poll.provisionDeadlineSeconds | quote }}
      # OLSConfig CR to annotate as the PRIMARY (supported) operator-stop
      # mechanism -- see values.yaml's appServerPatch operator-stop comment.
      - name: OLS_CONFIG_NAME
        value: {{ .Values.olsConfigName | quote }}
      # Mode A MCP hardening (see values.yaml appServerPatch.mcpHardening and
      # README.md "MCP hardening"). Only acts when MCP_HARDENING_ENABLED=true.
      - name: MCP_HARDENING_ENABLED
        value: {{ .Values.appServerPatch.mcpHardening.enabled | quote }}
      - name: MCP_CONFIGMAP_NAME
        value: {{ .Values.appServerPatch.mcpHardening.configMapName | quote }}
      - name: MCP_CONFIGMAP_KEY
        value: {{ .Values.appServerPatch.mcpHardening.configMapKey | quote }}
      - name: MCP_DEPLOYMENT_NAME
        value: {{ .Values.appServerPatch.mcpHardening.deploymentName | quote }}
      - name: MCP_REQUIRE_OAUTH
        value: {{ .Values.appServerPatch.mcpHardening.requireOauth | quote }}
      - name: MCP_SKIP_JWT_VERIFICATION
        value: {{ .Values.appServerPatch.mcpHardening.skipJwtVerification | quote }}
      - name: MCP_CLUSTER_AUTH_MODE
        value: {{ .Values.appServerPatch.mcpHardening.clusterAuthMode | quote }}
      # Correct the operator-generated alertmanager_url (wrong port by default;
      # see values.yaml). Empty leaves the operator's value untouched.
      - name: MCP_ALERTMANAGER_URL
        value: {{ .Values.appServerPatch.mcpHardening.alertmanagerUrl | quote }}
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
