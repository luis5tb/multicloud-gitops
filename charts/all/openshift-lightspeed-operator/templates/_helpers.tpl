{{- define "openshift-lightspeed-operator.validate" -}}
{{- $catalog := .Values.catalogSource -}}
{{- $subscription := .Values.subscription -}}
{{- if and $subscription.enabled (not $catalog.enabled) -}}
  {{- fail "subscription.enabled requires catalogSource.enabled so the OLM source is managed and pinned by this chart" -}}
{{- end -}}
{{- if $catalog.enabled -}}
  {{- if empty $catalog.name -}}{{ fail "catalogSource.name is required when catalogSource.enabled is true" }}{{- end -}}
  {{- if empty $catalog.namespace -}}{{ fail "catalogSource.namespace is required when catalogSource.enabled is true" }}{{- end -}}
  {{- if empty $catalog.image -}}{{ fail "catalogSource.image is required when catalogSource.enabled is true" }}{{- end -}}
  {{- if not (regexMatch "^[^[:space:]@]+@sha256:[a-f0-9]{64}$" $catalog.image) -}}
    {{- fail "catalogSource.image must be an immutable image reference ending in @sha256:<64 lowercase hexadecimal characters>; tag-only and floating references are not allowed" -}}
  {{- end -}}
{{- end -}}
{{- if $subscription.enabled -}}
  {{- if empty $subscription.name -}}{{ fail "subscription.name is required when subscription.enabled is true" }}{{- end -}}
  {{- if empty $subscription.packageName -}}{{ fail "subscription.packageName must be supplied from the published downstream bundle; no package is assumed" }}{{- end -}}
  {{- if empty $subscription.channel -}}{{ fail "subscription.channel must be supplied from the published downstream catalog; no channel is assumed" }}{{- end -}}
  {{- if empty $subscription.startingCSV -}}{{ fail "subscription.startingCSV must be supplied from the published downstream bundle; no CSV is assumed" }}{{- end -}}
  {{- if empty $.Values.installNamespace.name -}}{{ fail "installNamespace.name is required when subscription.enabled is true" }}{{- end -}}
  {{- if empty $.Values.operatorGroup.name -}}{{ fail "operatorGroup.name is required when subscription.enabled is true" }}{{- end -}}
  {{- if not (has $subscription.installPlanApproval (list "Manual" "Automatic")) -}}
    {{- fail "subscription.installPlanApproval must be Manual or Automatic" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
