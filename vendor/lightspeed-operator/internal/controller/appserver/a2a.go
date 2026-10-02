package appserver

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"

	olsv1alpha1 "github.com/openshift/lightspeed-operator/api/v1alpha1"
	"github.com/openshift/lightspeed-operator/internal/controller/reconciler"
	"github.com/openshift/lightspeed-operator/internal/controller/utils"
)

const (
	spiffeCSIDriver             = "csi.spiffe.io"
	a2aDefaultInboundAudience   = "lightspeed-a2a"
	a2aDefaultCallerClientID    = "acme-agent"
	a2aDefaultExchangeClientID  = "lightspeed-mcp"
	a2aDefaultMCPAudience       = "openshift-mcp"
	a2aKeycloakCAFilename       = utils.AppServerA2AKeycloakCAFile
	a2aKeycloakCAMountDirectory = utils.OLSAppCertsMountRoot + "/" + utils.AppServerA2AKeycloakCADir
	a2aKeycloakCAFilePath       = a2aKeycloakCAMountDirectory + "/" + a2aKeycloakCAFilename

	envA2AClusterID             = "OLS_A2A_CLUSTER_ID"
	envA2APublicURL             = "OLS_A2A_PUBLIC_URL"
	envA2AKeycloakIssuer        = "OLS_A2A_KEYCLOAK_ISSUER_URL"
	envA2AInboundAudience       = "OLS_A2A_INBOUND_AUDIENCE"
	envA2AAllowedCallerClientID = "OLS_A2A_ALLOWED_CALLER_CLIENT_ID"
	envA2AExchangeClientID      = "OLS_A2A_EXCHANGE_CLIENT_ID"
	envA2AMCPAudience           = "OLS_A2A_MCP_AUDIENCE"
	envA2ASpiffeJWTAudience     = "OLS_A2A_SPIFFE_JWT_AUDIENCE"
	envA2AKeycloakCABundle      = "OLS_A2A_KEYCLOAK_CA_BUNDLE"
	envSpiffeEndpointSocket     = "SPIFFE_ENDPOINT_SOCKET"
)

var immutableImageReference = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._:/+-]*@sha256:[a-f0-9]{64}$`)

type a2aWorkloadSettings struct {
	clusterID           string
	publicURL           string
	issuerURL           string
	inboundAudience     string
	allowedCallerClient string
	exchangeClient      string
	mcpAudience         string
	caSecretName        string
	caSecretKey         string
}

func getA2AWorkloadSettings(cr *olsv1alpha1.OLSConfig) (*a2aWorkloadSettings, error) {
	a2a := cr.Spec.OLSConfig.A2A
	if a2a == nil || !a2a.Enabled {
		return nil, nil
	}
	if !utils.BoolDeref(cr.Spec.OLSConfig.IntrospectionEnabled, true) {
		return nil, fmt.Errorf("spec.ols.a2a.enabled requires spec.ols.introspectionEnabled=true")
	}

	security := cr.Spec.OLSConfig.MCPServerSecurity
	if security == nil {
		return nil, fmt.Errorf("spec.ols.a2a.enabled requires spec.ols.mcpServerSecurity")
	}
	if security.CASecretRef == nil {
		return nil, fmt.Errorf("spec.ols.a2a.enabled requires spec.ols.mcpServerSecurity.caSecretRef")
	}
	if security.CASecretRef.Optional != nil && *security.CASecretRef.Optional {
		return nil, fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.optional must be false")
	}
	if problems := validation.IsDNS1123Subdomain(security.CASecretRef.Name); len(problems) != 0 {
		return nil, fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.name is invalid: %s", strings.Join(problems, ", "))
	}
	if problems := validation.IsConfigMapKey(security.CASecretRef.Key); len(problems) != 0 {
		return nil, fmt.Errorf("spec.ols.mcpServerSecurity.caSecretRef.key is invalid: %s", strings.Join(problems, ", "))
	}

	issuerURL := security.AuthorizationURL
	issuer, err := url.Parse(issuerURL)
	if err != nil || issuer.Scheme != "https" || issuer.Host == "" || issuer.User != nil || issuer.RawQuery != "" || issuer.Fragment != "" || issuer.Opaque != "" || strings.TrimSpace(issuerURL) != issuerURL {
		return nil, fmt.Errorf("spec.ols.mcpServerSecurity.authorizationURL must be an absolute HTTPS issuer URL")
	}
	mcpAudience := security.OAuthAudience
	if mcpAudience == "" {
		mcpAudience = a2aDefaultMCPAudience
	}
	if mcpAudience != a2aDefaultMCPAudience {
		return nil, fmt.Errorf("spec.ols.mcpServerSecurity.oauthAudience must be %q", a2aDefaultMCPAudience)
	}

	if problems := validation.IsDNS1123Label(a2a.TargetClusterID); len(problems) != 0 {
		return nil, fmt.Errorf("spec.ols.a2a.targetClusterID is invalid: %s", strings.Join(problems, ", "))
	}
	publicURL, err := url.Parse(a2a.PublicURL)
	if err != nil || publicURL.Scheme != "https" || publicURL.Host == "" || publicURL.User != nil || publicURL.Path != "" || publicURL.RawQuery != "" || publicURL.Fragment != "" || publicURL.Opaque != "" || publicURL.ForceQuery || strings.TrimSpace(a2a.PublicURL) != a2a.PublicURL {
		return nil, fmt.Errorf("spec.ols.a2a.publicURL must be an HTTPS origin without path, userinfo, query, or fragment")
	}

	inboundAudience := a2a.InboundAudience
	if inboundAudience == "" {
		inboundAudience = a2aDefaultInboundAudience
	}
	callerClientID := a2a.AllowedCallerClientID
	if callerClientID == "" {
		callerClientID = a2aDefaultCallerClientID
	}
	exchangeClientID := a2a.ExchangeClientID
	if exchangeClientID == "" {
		exchangeClientID = a2aDefaultExchangeClientID
	}
	if inboundAudience != a2aDefaultInboundAudience || callerClientID != a2aDefaultCallerClientID || exchangeClientID != a2aDefaultExchangeClientID {
		return nil, fmt.Errorf("spec.ols.a2a audience and client IDs must use the fixed lightspeed-a2a/acme-agent/lightspeed-mcp contract")
	}

	return &a2aWorkloadSettings{
		clusterID:           a2a.TargetClusterID,
		publicURL:           a2a.PublicURL,
		issuerURL:           issuerURL,
		inboundAudience:     inboundAudience,
		allowedCallerClient: callerClientID,
		exchangeClient:      exchangeClientID,
		mcpAudience:         mcpAudience,
		caSecretName:        security.CASecretRef.Name,
		caSecretKey:         security.CASecretRef.Key,
	}, nil
}

func validateA2AKeycloakCA(r reconciler.Reconciler, ctx context.Context, settings *a2aWorkloadSettings) error {
	if settings == nil {
		return nil
	}
	secret := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Name: settings.caSecretName, Namespace: r.GetNamespace()}, secret); err != nil {
		return fmt.Errorf("A2A Keycloak CA Secret %q is required in namespace %q: %w", settings.caSecretName, r.GetNamespace(), err)
	}
	if err := utils.ValidatePEMCABundle(secret, settings.caSecretKey); err != nil {
		return fmt.Errorf("A2A Keycloak CA validation failed: %w", err)
	}
	return nil
}

func (settings *a2aWorkloadSettings) envVars() []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: envA2AClusterID, Value: settings.clusterID},
		{Name: envA2APublicURL, Value: settings.publicURL},
		{Name: envA2AKeycloakIssuer, Value: settings.issuerURL},
		{Name: envA2AInboundAudience, Value: settings.inboundAudience},
		{Name: envA2AAllowedCallerClientID, Value: settings.allowedCallerClient},
		{Name: envA2AExchangeClientID, Value: settings.exchangeClient},
		{Name: envA2AMCPAudience, Value: settings.mcpAudience},
		{Name: envA2ASpiffeJWTAudience, Value: settings.issuerURL},
		{Name: envA2AKeycloakCABundle, Value: a2aKeycloakCAFilePath},
		{Name: envSpiffeEndpointSocket, Value: utils.AppServerA2ASpiffeSocket},
	}
}

func (settings *a2aWorkloadSettings) volumesAndMounts() ([]corev1.Volume, []corev1.VolumeMount) {
	mode := utils.VolumeDefaultMode
	return []corev1.Volume{
			{
				Name: utils.AppServerA2ASpiffeVolumeName,
				VolumeSource: corev1.VolumeSource{CSI: &corev1.CSIVolumeSource{
					Driver:   spiffeCSIDriver,
					ReadOnly: utils.BoolPtr(true),
				}},
			},
			{
				Name: utils.AppServerA2AKeycloakCAVolumeName,
				VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
					SecretName:  settings.caSecretName,
					DefaultMode: &mode,
					Items:       []corev1.KeyToPath{{Key: settings.caSecretKey, Path: a2aKeycloakCAFilename}},
				}},
			},
		}, []corev1.VolumeMount{
			{Name: utils.AppServerA2ASpiffeVolumeName, MountPath: utils.AppServerA2ASpiffeMountPath, ReadOnly: true},
			{Name: utils.AppServerA2AKeycloakCAVolumeName, MountPath: a2aKeycloakCAMountDirectory, ReadOnly: true},
		}
}

func validateServiceImage(image string) error {
	if image == "" {
		return nil
	}
	if strings.TrimSpace(image) != image || !immutableImageReference.MatchString(image) {
		return fmt.Errorf("spec.ols.serviceImage must be an image reference pinned with @sha256:<64 lowercase hex characters>")
	}
	return nil
}

func dataCollectorVolumeMounts(mounts []corev1.VolumeMount) []corev1.VolumeMount {
	filtered := make([]corev1.VolumeMount, 0, len(mounts))
	for _, mount := range mounts {
		if mount.Name == utils.AppServerA2ASpiffeVolumeName || mount.Name == utils.AppServerA2AKeycloakCAVolumeName {
			continue
		}
		filtered = append(filtered, mount)
	}
	return filtered
}
