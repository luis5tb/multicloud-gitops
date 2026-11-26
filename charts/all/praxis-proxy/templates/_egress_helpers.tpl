{{/*
External-IdP egress gateways (egressGateways map). Kept in their own helper
file so the feature stays self-contained. See values.yaml egressGateways and
docs/EXTERNAL_IDP_EGRESS.md.
*/}}

{{- define "praxis-proxy.egressGatewayName" -}}
{{- printf "praxis-egress-%s" . | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Distinct app name so the main praxis-proxy Service never selects these pods. */}}
{{- define "praxis-proxy.egressGatewayAppName" -}}
praxis-egress-gateway
{{- end }}

{{- define "praxis-proxy.egressGatewaySelectorLabels" -}}
app.kubernetes.io/name: {{ include "praxis-proxy.egressGatewayAppName" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
praxis-proxy.egress-gateway: {{ .key | quote }}
{{- end }}

{{- define "praxis-proxy.egressGatewayLabels" -}}
{{ include "praxis-proxy.egressGatewaySelectorLabels" . }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version | replace "+" "_" }}
{{- end }}

{{/* Secret holding the egress client's secret: the existing one, or the ExternalSecret's target. */}}
{{- define "praxis-proxy.egressGatewaySecretName" -}}
{{- $cs := default dict .cfg.clientSecret -}}
{{- if $cs.existingSecret -}}
{{- $cs.existingSecret.name -}}
{{- else -}}
{{- printf "%s-client" (include "praxis-proxy.egressGatewayName" .key) -}}
{{- end -}}
{{- end }}

{{- define "praxis-proxy.egressGatewaySecretKey" -}}
{{- $cs := default dict .cfg.clientSecret -}}
{{- if $cs.existingSecret -}}
{{- $cs.existingSecret.key | default "client-secret" -}}
{{- else -}}
client-secret
{{- end -}}
{{- end }}

{{/*
Parsed upstream: a dict with host, port, hostHeader and pathPrefix (no
trailing slash). Validation happens in egress-gateway-configmap.yaml.
*/}}
{{- define "praxis-proxy.egressGatewayUpstream" -}}
{{- $u := urlParse (.upstreamURL | toString) -}}
{{- $hostPort := $u.host | toString -}}
{{- $host := $hostPort -}}
{{- $port := "443" -}}
{{- if contains ":" $hostPort -}}
{{- $host = index (splitList ":" $hostPort) 0 -}}
{{- $port = index (splitList ":" $hostPort) 1 -}}
{{- end -}}
{{- $hostHeader := $host -}}
{{- if ne $port "443" -}}
{{- $hostHeader = printf "%s:%s" $host $port -}}
{{- end -}}
{{- $path := trimSuffix "/" ($u.path | toString) -}}
{{- dict "host" $host "port" $port "hostHeader" $hostHeader "pathPrefix" $path | toJson -}}
{{- end }}
