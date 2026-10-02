// Package ocpmcp reconciles the standalone OpenShift MCP server (ocp-mcp) operand.
package ocpmcp

import (
	"fmt"
	"net/url"
	"path"
	"strconv"
	"strings"

	monv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	olsv1alpha1 "github.com/openshift/lightspeed-operator/api/v1alpha1"
	"github.com/openshift/lightspeed-operator/internal/controller/reconciler"
	"github.com/openshift/lightspeed-operator/internal/controller/utils"
)

const defaultMCPToolset = olsv1alpha1.MCPToolset("core")

var supportedMCPToolsets = map[olsv1alpha1.MCPToolset]struct{}{
	"core": {}, "config": {}, "helm": {}, "observability/metrics": {}, "kubevirt": {},
}

// mcpSecuritySettings validates the authentication inputs before they reach TOML
// generation. Authentication is never downgraded to unauthenticated or
// skip-verification passthrough when configuration is missing or invalid.
func mcpSecuritySettings(cr *olsv1alpha1.OLSConfig) (*olsv1alpha1.MCPServerSecurityConfig, string, error) {
	if cr.Spec.OLSConfig.MCPServerSecurity == nil {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity is required while introspection is enabled")
	}
	security := cr.Spec.OLSConfig.MCPServerSecurity
	issuer, err := url.ParseRequestURI(strings.TrimSpace(security.AuthorizationURL))
	if err != nil || issuer.Scheme != "https" || issuer.Host == "" || issuer.User != nil || issuer.RawQuery != "" || issuer.Fragment != "" || strings.TrimSpace(security.AuthorizationURL) != security.AuthorizationURL {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.authorizationURL must be an absolute HTTPS issuer URL without userinfo, query, or fragment")
	}
	if security.CASecretRef == nil {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef is required for local OIDC verification")
	}
	if security.CASecretRef.Optional != nil && *security.CASecretRef.Optional {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.optional must be false")
	}
	if problems := validation.IsDNS1123Subdomain(security.CASecretRef.Name); len(problems) != 0 {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.name is invalid: %s", strings.Join(problems, ", "))
	}
	if problems := validation.IsConfigMapKey(security.CASecretRef.Key); len(problems) != 0 {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.key is invalid: %s", strings.Join(problems, ", "))
	}

	audience := security.OAuthAudience
	if audience == "" {
		audience = "openshift-mcp"
	}
	if audience != "openshift-mcp" {
		return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.oauthAudience must be openshift-mcp")
	}

	if len(security.Toolsets) == 0 {
		if security.Toolsets != nil {
			return nil, "", fmt.Errorf("spec.ols.mcpServerSecurity.toolsets must be omitted or contain at least one supported toolset")
		}
		securityCopy := *security
		securityCopy.Toolsets = []olsv1alpha1.MCPToolset{defaultMCPToolset}
		security = &securityCopy
	}
	seenToolsets := make(map[olsv1alpha1.MCPToolset]struct{}, len(security.Toolsets))
	for _, toolset := range security.Toolsets {
		if _, ok := supportedMCPToolsets[toolset]; !ok {
			return nil, "", fmt.Errorf("unsupported MCP toolset %q", toolset)
		}
		if _, ok := seenToolsets[toolset]; ok {
			return nil, "", fmt.Errorf("duplicate MCP toolset %q", toolset)
		}
		seenToolsets[toolset] = struct{}{}
	}
	return security, audience, nil
}

// generateConfigTOML writes an authenticated, read-only MCP configuration.
// There is deliberately no [token_exchange] block: the Lightspeed request path
// supplies the already-exchanged, request-scoped user token.
func generateConfigTOML(cr *olsv1alpha1.OLSConfig) (string, error) {
	security, audience, err := mcpSecuritySettings(cr)
	if err != nil {
		return "", err
	}
	toolsets := make([]string, 0, len(security.Toolsets))
	for _, toolset := range security.Toolsets {
		toolsets = append(toolsets, strconv.Quote(string(toolset)))
	}
	metricsConfig := ""
	if slicesContains(security.Toolsets, "observability/metrics") {
		metricsConfig = `
[toolset_configs."observability/metrics"]
prometheus_url = "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"
alertmanager_url = "https://alertmanager-main.openshift-monitoring.svc.cluster.local:9095"
# Query-safety PromQL checks (not RBAC). "!tsdb" disables TSDB-dependent guardrails that
# OpenShift Thanos Querier often lacks (/api/v1/status/tsdb); other guardrails stay on.
guardrails = "!tsdb"
`
	}
	return fmt.Sprintf(`# Require and independently validate the caller bearer before MCP requests.
require_oauth = true
authorization_url = %s
oauth_audience = %s
certificate_authority = %s
cluster_auth_mode = "passthrough"

# The service-ca serving cert below is distinct from the issuer CA above.
port = "%d"
tls_cert = %s
tls_key = %s
read_only = true
toolsets = [%s]
experimental_enable_target_compatibility_tool_filters = true

[[denied_resources]]
group = ""
version = "v1"
kind = "Secret"

[[denied_resources]]
group = "rbac.authorization.k8s.io"
version = "v1"
%s`,
		strconv.Quote(security.AuthorizationURL), strconv.Quote(audience),
		strconv.Quote(path.Join(utils.OpenShiftMCPServerOIDCCAMountPath, utils.OpenShiftMCPServerOIDCCAFilename)),
		utils.OpenShiftMCPServerHTTPSPort,
		strconv.Quote(path.Join(utils.OpenShiftMCPServerTLSMountPath, "tls.crt")),
		strconv.Quote(path.Join(utils.OpenShiftMCPServerTLSMountPath, "tls.key")),
		strings.Join(toolsets, ", "), metricsConfig,
	), nil
}

func slicesContains[T comparable](values []T, value T) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}

func selectorLabels() map[string]string {
	return map[string]string{
		"app":                          utils.OpenShiftMCPServerDeploymentName,
		"app.kubernetes.io/component":  utils.OpenShiftMCPServerComponentLabel,
		"app.kubernetes.io/managed-by": "lightspeed-operator",
		"app.kubernetes.io/name":       utils.OpenShiftMCPServerDeploymentName,
		"app.kubernetes.io/part-of":    "openshift-lightspeed",
	}
}

// GenerateServiceAccount generates the standalone MCP server ServiceAccount.
// The SA has no RBAC bindings; callers pass through their own token.
func GenerateServiceAccount(r reconciler.Reconciler, cr *olsv1alpha1.OLSConfig) (*corev1.ServiceAccount, error) {
	sa, err := utils.GenerateServiceAccount(r, cr, utils.OpenShiftMCPServerServiceAccountName)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", utils.ErrGenerateOpenShiftMCPServerServiceAccount, err)
	}
	return sa, nil
}

// GenerateConfigMap generates the TOML ConfigMap for openshift-mcp-server.
func GenerateConfigMap(r reconciler.Reconciler, cr *olsv1alpha1.OLSConfig) (*corev1.ConfigMap, error) {
	configTOML, err := generateConfigTOML(cr)
	if err != nil {
		return nil, err
	}
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      utils.OpenShiftMCPServerConfigCmName,
			Namespace: r.GetNamespace(),
			Labels:    selectorLabels(),
		},
		Data: map[string]string{
			utils.OpenShiftMCPServerConfigFilename: configTOML,
		},
	}
	if err := controllerutil.SetControllerReference(cr, cm, r.GetScheme()); err != nil {
		return nil, fmt.Errorf("%s: %w", utils.ErrSetOpenShiftMCPServerConfigMapOwnerReference, err)
	}
	return cm, nil
}

// GenerateService generates the ClusterIP Service on HTTPS port 8443 with a
// service-ca serving-cert annotation.
func GenerateService(r reconciler.Reconciler, cr *olsv1alpha1.OLSConfig) (*corev1.Service, error) {
	service := corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      utils.OpenShiftMCPServerServiceName,
			Namespace: r.GetNamespace(),
			Labels:    selectorLabels(),
			Annotations: map[string]string{
				utils.ServingCertSecretAnnotationKey: utils.OpenShiftMCPServerCertsSecretName,
			},
		},
		Spec: corev1.ServiceSpec{
			Selector: selectorLabels(),
			Type:     corev1.ServiceTypeClusterIP,
			Ports: []corev1.ServicePort{
				{
					Name:       "https",
					Port:       utils.OpenShiftMCPServerHTTPSPort,
					Protocol:   corev1.ProtocolTCP,
					TargetPort: intstr.FromString("https"),
				},
			},
		},
	}
	if err := controllerutil.SetControllerReference(cr, &service, r.GetScheme()); err != nil {
		return nil, fmt.Errorf("%s: %w", utils.ErrSetOpenShiftMCPServerServiceOwnerReference, err)
	}
	return &service, nil
}

// GenerateNetworkPolicy allows ingress to MCP only from the app-server pods and,
// when enabled, the cluster Prometheus pods for metrics scraping.
func GenerateNetworkPolicy(r reconciler.Reconciler, cr *olsv1alpha1.OLSConfig) (*networkingv1.NetworkPolicy, error) {
	tcp := corev1.ProtocolTCP
	httpsPort := intstr.FromInt32(utils.OpenShiftMCPServerHTTPSPort)
	security, _, err := mcpSecuritySettings(cr)
	if err != nil {
		return nil, err
	}
	ingress := []networkingv1.NetworkPolicyIngressRule{{
		From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: utils.GenerateAppServerSelectorLabels()},
		}},
		Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &httpsPort}},
	}}
	if security.AllowPrometheusMetrics == nil || *security.AllowPrometheusMetrics {
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{
			From: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{
					{Key: "app.kubernetes.io/name", Operator: metav1.LabelSelectorOpIn, Values: []string{"prometheus"}},
					{Key: "prometheus", Operator: metav1.LabelSelectorOpIn, Values: []string{"k8s"}},
				}},
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
					"kubernetes.io/metadata.name": utils.ClientCACmNamespace,
				}},
			}},
			Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &httpsPort}},
		})
	}
	np := networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{
			Name:      utils.OpenShiftMCPServerNetworkPolicyName,
			Namespace: r.GetNamespace(),
			Labels:    selectorLabels(),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: selectorLabels(),
			},
			Ingress: ingress,
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
			},
		},
	}
	if err := controllerutil.SetControllerReference(cr, &np, r.GetScheme()); err != nil {
		return nil, fmt.Errorf("%s: %w", utils.ErrSetOpenShiftMCPServerNetworkPolicyOwnerReference, err)
	}
	return &np, nil
}

// ValidateOIDCCABundle checks the referenced CA bytes before adding the secret
// to a Deployment. It prevents an empty or malformed mounted trust bundle from
// silently disabling OIDC verification at runtime.
func validateOIDCCABundle(secret *corev1.Secret, key string) error {
	return utils.ValidatePEMCABundle(secret, key)
}

// GetOIDCCAVolumeAndMount mounts only the configured Secret key at the distinct
// path referenced by certificate_authority in the MCP TOML.
func GetOIDCCAVolumeAndMount(cr *olsv1alpha1.OLSConfig) (corev1.Volume, corev1.VolumeMount, error) {
	security, _, err := mcpSecuritySettings(cr)
	if err != nil {
		return corev1.Volume{}, corev1.VolumeMount{}, err
	}
	mode := utils.VolumeRestrictedMode
	volume := corev1.Volume{
		Name: utils.OpenShiftMCPServerOIDCCAVolumeName,
		VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
			SecretName:  security.CASecretRef.Name,
			Items:       []corev1.KeyToPath{{Key: security.CASecretRef.Key, Path: utils.OpenShiftMCPServerOIDCCAFilename}},
			DefaultMode: &mode,
		}},
	}
	mount := corev1.VolumeMount{
		Name: utils.OpenShiftMCPServerOIDCCAVolumeName, MountPath: utils.OpenShiftMCPServerOIDCCAMountPath, ReadOnly: true,
	}
	return volume, mount, nil
}

// GetConfigVolumeAndMount returns the Volume and VolumeMount for the
// openshift-mcp-server TOML configuration. The config file is mounted as a subPath
// so the MCP container can reference it via --config.
func GetConfigVolumeAndMount() (corev1.Volume, corev1.VolumeMount) {
	volumeDefaultMode := utils.VolumeDefaultMode
	volume := corev1.Volume{
		Name: utils.OpenShiftMCPServerConfigVolumeName,
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{
					Name: utils.OpenShiftMCPServerConfigCmName,
				},
				DefaultMode: &volumeDefaultMode,
			},
		},
	}

	volumeMount := corev1.VolumeMount{
		Name:      utils.OpenShiftMCPServerConfigVolumeName,
		MountPath: path.Join(utils.OpenShiftMCPServerConfigMountPath, utils.OpenShiftMCPServerConfigFilename),
		SubPath:   utils.OpenShiftMCPServerConfigFilename,
		ReadOnly:  true,
	}

	return volume, volumeMount
}

// GetConfigPath returns the full path to the MCP server config file inside the container.
func GetConfigPath() string {
	return path.Join(utils.OpenShiftMCPServerConfigMountPath, utils.OpenShiftMCPServerConfigFilename)
}

// generateServiceMonitor generates a ServiceMonitor for HTTPS scraping of
// MCP server metrics on :8443, path /metrics. Server TLS only (service-ca).
func generateServiceMonitor(r reconciler.Reconciler, cr *olsv1alpha1.OLSConfig) (*monv1.ServiceMonitor, error) {
	metaLabels := selectorLabels()
	metaLabels["monitoring.openshift.io/collection-profile"] = "full"
	metaLabels["app.kubernetes.io/component"] = "metrics"
	metaLabels["openshift.io/user-monitoring"] = "false"

	valFalse := false
	serverName := strings.Join([]string{utils.OpenShiftMCPServerServiceName, r.GetNamespace(), "svc"}, ".")
	var schemeHTTPS monv1.Scheme = "https"

	serviceMonitor := monv1.ServiceMonitor{
		ObjectMeta: metav1.ObjectMeta{
			Name:      utils.OpenShiftMCPServerServiceMonitorName,
			Namespace: r.GetNamespace(),
			Labels:    metaLabels,
		},
		Spec: monv1.ServiceMonitorSpec{
			Endpoints: []monv1.Endpoint{
				{
					Port:     "https",
					Path:     utils.OpenShiftMCPServerMetricsPath,
					Interval: "30s",
					Scheme:   &schemeHTTPS,
					HTTPConfigWithProxyAndTLSFiles: monv1.HTTPConfigWithProxyAndTLSFiles{
						HTTPConfigWithTLSFiles: monv1.HTTPConfigWithTLSFiles{
							TLSConfig: &monv1.TLSConfig{
								TLSFilesConfig: monv1.TLSFilesConfig{
									CAFile: "/etc/prometheus/configmaps/serving-certs-ca-bundle/service-ca.crt",
								},
								SafeTLSConfig: monv1.SafeTLSConfig{
									InsecureSkipVerify: &valFalse,
									ServerName:         &serverName,
								},
							},
						},
					},
				},
			},
			JobLabel: "app.kubernetes.io/name",
			Selector: metav1.LabelSelector{
				MatchLabels: selectorLabels(),
			},
		},
	}

	if err := controllerutil.SetControllerReference(cr, &serviceMonitor, r.GetScheme()); err != nil {
		return nil, fmt.Errorf("%s: %w", utils.ErrSetOpenShiftMCPServerServiceMonitorOwnerReference, err)
	}
	return &serviceMonitor, nil
}
