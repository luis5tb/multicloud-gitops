package ocpmcp

import (
	"fmt"
	"path"
	"strings"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	monv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	olsv1alpha1 "github.com/openshift/lightspeed-operator/api/v1alpha1"
	"github.com/openshift/lightspeed-operator/internal/controller/utils"
)

var _ = Describe("OpenShift MCP Server assets", func() {
	var testCR *olsv1alpha1.OLSConfig
	labels := selectorLabels()

	BeforeEach(func() {
		testCR = utils.GetDefaultOLSConfigCR()
		testCR.Spec.OLSConfig.IntrospectionEnabled = utils.BoolPtr(true)
	})

	It("should generate an authenticated, read-only TOML config with denied Secret and RBAC resources", func() {
		cm, err := GenerateConfigMap(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(cm.Name).To(Equal(utils.OpenShiftMCPServerConfigCmName))
		Expect(cm.Namespace).To(Equal(utils.OLSNamespaceDefault))
		Expect(cm.Labels).To(Equal(labels))

		toml := cm.Data[utils.OpenShiftMCPServerConfigFilename]
		Expect(toml).To(ContainSubstring(fmt.Sprintf(`port = "%d"`, utils.OpenShiftMCPServerHTTPSPort)))
		Expect(toml).To(ContainSubstring(fmt.Sprintf(`tls_cert = "%s"`, path.Join(utils.OpenShiftMCPServerTLSMountPath, "tls.crt"))))
		Expect(toml).To(ContainSubstring(fmt.Sprintf(`tls_key = "%s"`, path.Join(utils.OpenShiftMCPServerTLSMountPath, "tls.key"))))
		Expect(toml).To(ContainSubstring("require_oauth = true"))
		Expect(toml).To(ContainSubstring(`authorization_url = "https://issuer.example.test/realms/ols"`))
		Expect(toml).To(ContainSubstring(`oauth_audience = "openshift-mcp"`))
		Expect(toml).To(ContainSubstring(`cluster_auth_mode = "passthrough"`))
		Expect(toml).To(ContainSubstring(`certificate_authority = "/etc/mcp-server/oidc-ca/ca-bundle.crt"`))
		Expect(toml).To(ContainSubstring("read_only = true"))
		Expect(toml).To(ContainSubstring(`toolsets = ["core"]`))
		Expect(toml).To(ContainSubstring(`experimental_enable_target_compatibility_tool_filters = true`))
		Expect(toml).To(ContainSubstring(`kind = "Secret"`))
		Expect(toml).To(ContainSubstring(`group = ""`))
		Expect(toml).To(ContainSubstring(`group = "rbac.authorization.k8s.io"`))
		Expect(toml).To(ContainSubstring("[[denied_resources]]"))
		Expect(toml).NotTo(ContainSubstring("skip_jwt_verification"))
		Expect(toml).NotTo(ContainSubstring("token_exchange"))
		Expect(toml).NotTo(ContainSubstring("read_only = false"))
		Expect(toml).NotTo(ContainSubstring(`[toolset_configs."observability/metrics"]`))
		Expect(strings.Count(toml, "[[denied_resources]]")).To(Equal(2))
	})

	It("should enable only configured, allow-listed toolsets", func() {
		testCR.Spec.OLSConfig.MCPServerSecurity.Toolsets = []olsv1alpha1.MCPToolset{"core", "observability/metrics"}
		cm, err := GenerateConfigMap(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		toml := cm.Data[utils.OpenShiftMCPServerConfigFilename]
		Expect(toml).To(ContainSubstring(`toolsets = ["core", "observability/metrics"]`))
		Expect(toml).To(ContainSubstring(`[toolset_configs."observability/metrics"]`))
		Expect(toml).To(ContainSubstring(`prometheus_url = "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"`))
		Expect(toml).To(ContainSubstring(`alertmanager_url = "https://alertmanager-main.openshift-monitoring.svc.cluster.local:9095"`))
		Expect(toml).To(ContainSubstring(`guardrails = "!tsdb"`))
	})

	It("should fail closed for missing or malformed OIDC configuration and unsafe toolsets", func() {
		cases := []struct {
			name   string
			mutate func()
			want   string
		}{
			{"missing security config", func() { testCR.Spec.OLSConfig.MCPServerSecurity = nil }, "mcpServerSecurity is required"},
			{"insecure issuer", func() { testCR.Spec.OLSConfig.MCPServerSecurity.AuthorizationURL = "http://issuer.example.test" }, "absolute HTTPS issuer URL"},
			{"wrong OAuth audience", func() { testCR.Spec.OLSConfig.MCPServerSecurity.OAuthAudience = "other-audience" }, "must be openshift-mcp"},
			{"missing CA key", func() { testCR.Spec.OLSConfig.MCPServerSecurity.CASecretRef.Key = "" }, "caSecretRef.key is invalid"},
			{"optional CA secret", func() {
				optional := true
				testCR.Spec.OLSConfig.MCPServerSecurity.CASecretRef.Optional = &optional
			}, "caSecretRef.optional must be false"},
			{"unknown toolset", func() { testCR.Spec.OLSConfig.MCPServerSecurity.Toolsets = []olsv1alpha1.MCPToolset{"core", "unsafe"} }, "unsupported MCP toolset"},
			{"empty toolset list", func() { testCR.Spec.OLSConfig.MCPServerSecurity.Toolsets = []olsv1alpha1.MCPToolset{} }, "must be omitted or contain at least one supported toolset"},
		}
		for _, tc := range cases {
			By(tc.name)
			original := testCR.DeepCopy()
			tc.mutate()
			_, err := GenerateConfigMap(testReconcilerInstance, testCR)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring(tc.want))
			testCR = original
		}
	})

	It("should generate the Service with HTTPS port and serving-cert annotation", func() {
		svc, err := GenerateService(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(svc.Name).To(Equal(utils.OpenShiftMCPServerServiceName))
		Expect(svc.Labels).To(Equal(labels))
		Expect(svc.Annotations[utils.ServingCertSecretAnnotationKey]).To(Equal(utils.OpenShiftMCPServerCertsSecretName))
		Expect(svc.Spec.Selector).To(Equal(labels))
		Expect(svc.Spec.Ports).To(HaveLen(1))
		Expect(svc.Spec.Ports[0].Name).To(Equal("https"))
		Expect(svc.Spec.Ports[0].Port).To(Equal(int32(utils.OpenShiftMCPServerHTTPSPort)))
		Expect(svc.Spec.Ports[0].TargetPort).To(Equal(intstr.FromString("https")))
	})

	It("should generate the NetworkPolicy for the app server and restricted metrics peer", func() {
		np, err := GenerateNetworkPolicy(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(np.Name).To(Equal(utils.OpenShiftMCPServerNetworkPolicyName))
		Expect(np.Labels).To(Equal(labels))
		Expect(np.Spec.PodSelector.MatchLabels).To(Equal(labels))

		tcp := corev1.ProtocolTCP
		httpsPort := intstr.FromInt32(utils.OpenShiftMCPServerHTTPSPort)
		Expect(np.Spec.Ingress).To(ConsistOf(
			networkingv1.NetworkPolicyIngressRule{
				From:  []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchLabels: utils.GenerateAppServerSelectorLabels()}}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &httpsPort}},
			},
			networkingv1.NetworkPolicyIngressRule{
				From: []networkingv1.NetworkPolicyPeer{{
					PodSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{
						{Key: "app.kubernetes.io/name", Operator: metav1.LabelSelectorOpIn, Values: []string{"prometheus"}},
						{Key: "prometheus", Operator: metav1.LabelSelectorOpIn, Values: []string{"k8s"}},
					}},
					NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": "openshift-monitoring"}},
				}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &httpsPort}},
			},
		))
	})

	It("should omit the Prometheus NetworkPolicy peer when disabled", func() {
		allowMetrics := false
		testCR.Spec.OLSConfig.MCPServerSecurity.AllowPrometheusMetrics = &allowMetrics
		np, err := GenerateNetworkPolicy(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(np.Spec.Ingress).To(HaveLen(1))
		Expect(np.Spec.Ingress[0].From).To(ConsistOf(networkingv1.NetworkPolicyPeer{
			PodSelector: &metav1.LabelSelector{MatchLabels: utils.GenerateAppServerSelectorLabels()},
		}))
	})

	It("should generate the ServiceAccount", func() {
		sa, err := GenerateServiceAccount(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(sa.Name).To(Equal(utils.OpenShiftMCPServerServiceAccountName))
		Expect(sa.Namespace).To(Equal(utils.OLSNamespaceDefault))
	})

	It("should return config volume and mount for --config", func() {
		volume, mount := GetConfigVolumeAndMount()
		Expect(volume.Name).To(Equal(utils.OpenShiftMCPServerConfigVolumeName))
		Expect(volume.ConfigMap).NotTo(BeNil())
		Expect(volume.ConfigMap.Name).To(Equal(utils.OpenShiftMCPServerConfigCmName))
		Expect(volume.ConfigMap.DefaultMode).NotTo(BeNil())
		Expect(*volume.ConfigMap.DefaultMode).To(Equal(utils.VolumeDefaultMode))
		Expect(mount.Name).To(Equal(utils.OpenShiftMCPServerConfigVolumeName))
		Expect(mount.MountPath).To(Equal(GetConfigPath()))
		Expect(mount.SubPath).To(Equal(utils.OpenShiftMCPServerConfigFilename))
		Expect(mount.ReadOnly).To(BeTrue())
		Expect(GetConfigPath()).To(Equal("/etc/mcp-server/config.toml"))
	})

	It("should generate the ServiceMonitor with HTTPS scrape config", func() {
		sm, err := generateServiceMonitor(testReconcilerInstance, testCR)
		Expect(err).NotTo(HaveOccurred())
		Expect(sm.Name).To(Equal(utils.OpenShiftMCPServerServiceMonitorName))
		Expect(sm.Namespace).To(Equal(utils.OLSNamespaceDefault))
		Expect(sm.Labels).To(HaveKeyWithValue("monitoring.openshift.io/collection-profile", "full"))
		Expect(sm.Labels).To(HaveKeyWithValue("openshift.io/user-monitoring", "false"))

		Expect(sm.Spec.Endpoints).To(HaveLen(1))
		ep := sm.Spec.Endpoints[0]
		Expect(ep.Port).To(Equal("https"))
		Expect(ep.Path).To(Equal(utils.OpenShiftMCPServerMetricsPath))
		Expect(ep.Interval).To(Equal(monv1.Duration("30s")))
		Expect(string(*ep.Scheme)).To(Equal("https"))
		Expect(ep.TLSConfig).NotTo(BeNil())
		Expect(ep.TLSConfig.CAFile).To(Equal("/etc/prometheus/configmaps/serving-certs-ca-bundle/service-ca.crt"))
		Expect(*ep.TLSConfig.InsecureSkipVerify).To(BeFalse())
		expectedServerName := utils.OpenShiftMCPServerServiceName + "." + utils.OLSNamespaceDefault + ".svc"
		Expect(*ep.TLSConfig.ServerName).To(Equal(expectedServerName))

		Expect(sm.Spec.Selector.MatchLabels).To(Equal(selectorLabels()))
		Expect(sm.Spec.JobLabel).To(Equal("app.kubernetes.io/name"))
	})
})
