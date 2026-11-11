// Package config loads the hub adapter's deployment configuration from
// flags (with env fallbacks for values that ride better in env, like
// secrets and the namespace).
package config

import (
	"flag"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"
)

// requireHTTPS rejects non-https URLs on the credential-exchange endpoints.
// The issuer and token endpoint carry (and authenticate the exchange of)
// bearer tokens; a plaintext leg would expose Token A/H to anyone on the
// path. Garbage that does not parse as a URL at all is rejected with the
// same clarity.
func requireHTTPS(name, raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("%s must be a valid URL, got %q", name, raw)
	}
	if u.Scheme != "https" {
		return fmt.Errorf("%s must be an https:// URL, got %q (plaintext token endpoints would expose credentials in transit)", name, raw)
	}
	return nil
}

// Config is the fully-resolved adapter configuration.
type Config struct {
	ListenAddr string

	// Namespace: the repo C operator namespace where AgenticRuns and
	// run-token Secrets live (design §8.3 / Q4).
	Namespace string

	// Keycloak settings for the adapter-fallback A→H exchange (design
	// §3.2). Client auth is EITHER secret OR SVID (socket path set).
	KeycloakIssuer        string
	KeycloakTokenEndpoint string // empty => <issuer>/protocol/openid-connect/token
	KeycloakClientID      string
	KeycloakClientSecret  string
	SVIDSocketPath        string

	// HubAudience is the audience requested on Token H.
	HubAudience string

	// TargetHeader is the hub-leg target transport header (design §4.2).
	TargetHeader string

	// RunAgent is spec.analysis.agent on created runs (the CRD requires
	// the analysis step to be non-empty; "default" is the CRD's own
	// documented default agent).
	RunAgent string

	// WriterIdentity stamps a2a.lightspeed.io/written-by on run-token
	// Secrets (design §5.6 provenance).
	WriterIdentity string

	ClockSkew          time.Duration
	StreamDeadline     time.Duration
	StreamPollInterval time.Duration // retained for rollback compat; unused since M3 (watch-driven streams)

	// M3 renewal-bridge / cleanup bounds (design §6.4, §5.5).
	RenewalTimeout       time.Duration // per-pause auth-required window before the task fails
	MaxPauses            int           // total credential pauses per run before the task fails
	TerminalCleanupGrace time.Duration // grace after terminal phase before the run-token Secret is deleted
	SweepInterval        time.Duration // watcher sweeper cadence (deadlines + cleanup due-times)

	// Agent card fields (design §2.1).
	AgentName            string
	AgentDescription     string
	AgentURL             string
	AgentVersion         string
	AgentProtocolVersion string
	SkillID              string
	SkillName            string
	SkillDescription     string
	SkillTags            []string
}

// AddFlags registers the flags on the given FlagSet.
func (c *Config) AddFlags(fs *flag.FlagSet) {
	fs.StringVar(&c.ListenAddr, "listen", ":8080", "HTTP listen address (internal-only Service; TLS terminates at the mesh/Praxis)")
	fs.StringVar(&c.Namespace, "namespace", envOr("NAMESPACE", ""), "namespace for AgenticRuns and run-token Secrets (env NAMESPACE; defaults to the pod's namespace)")
	fs.StringVar(&c.KeycloakIssuer, "keycloak-issuer", "", "Keycloak realm issuer URL (also the SVID client-assertion audience)")
	fs.StringVar(&c.KeycloakTokenEndpoint, "keycloak-token-endpoint", "", "full token endpoint URL (default: <issuer>/protocol/openid-connect/token)")
	fs.StringVar(&c.KeycloakClientID, "keycloak-client-id", "", "confidential exchange client id (secret mode; leave empty in SVID mode)")
	fs.StringVar(&c.KeycloakClientSecret, "keycloak-client-secret", envOr("KEYCLOAK_CLIENT_SECRET", ""), "confidential exchange client secret (env KEYCLOAK_CLIENT_SECRET)")
	fs.StringVar(&c.SVIDSocketPath, "svid-socket", envOr("SPIFFE_SOCKET", ""), "SPIFFE Workload API unix socket (SVID client-assertion mode; env SPIFFE_SOCKET)")
	fs.StringVar(&c.HubAudience, "hub-audience", "", "requested audience for the exchanged hub token (Token H)")
	fs.StringVar(&c.TargetHeader, "target-header", "X-Hub-Cluster", "inbound transport header carrying the target cluster (design §4.2)")
	fs.StringVar(&c.RunAgent, "run-agent", "default", "spec.analysis.agent on created AgenticRuns")
	fs.StringVar(&c.WriterIdentity, "writer-identity", "", "value for the a2a.lightspeed.io/written-by provenance annotation (default: adapter-sa)")
	fs.DurationVar(&c.ClockSkew, "clock-skew", 30*time.Second, "grace after exp in the pre-start Token A expiry check")
	fs.DurationVar(&c.StreamDeadline, "stream-deadline", 30*time.Minute, "wall-clock bound on message/stream (design §2.3; renewal pauses do not count against it)")
	fs.DurationVar(&c.StreamPollInterval, "stream-poll-interval", 5*time.Second, "deprecated: streams are watch-driven since M3; kept for config compatibility, unused")
	fs.DurationVar(&c.RenewalTimeout, "renewal-timeout", 900*time.Second, "how long a task may stay auth-required before failing (design §6.4; aligned with MAS's worker-side 900 s bound)")
	fs.IntVar(&c.MaxPauses, "max-credential-pauses", 3, "total credential pauses per run before the task fails (design §6.4)")
	fs.DurationVar(&c.TerminalCleanupGrace, "terminal-cleanup-grace", time.Minute, "grace after a terminal phase before ls-run-token-{runUID} is deleted (design §5.5)")
	fs.DurationVar(&c.SweepInterval, "sweep-interval", 5*time.Second, "watcher sweeper cadence for pause deadlines and Secret cleanup")

	fs.StringVar(&c.AgentName, "card-name", "lightspeed-hub", "agent card name")
	fs.StringVar(&c.AgentDescription, "card-description", "Red Hat cluster investigation via lightspeed-hub", "agent card description")
	fs.StringVar(&c.AgentURL, "card-url", "", "agent card URL (the Praxis-fronted origin callers use)")
	fs.StringVar(&c.AgentVersion, "card-version", "0.1.0", "agent card version")
	fs.StringVar(&c.AgentProtocolVersion, "card-protocol-version", "0.3.0", "A2A protocol version advertised in the card")
	fs.StringVar(&c.SkillID, "card-skill-id", "cluster-investigation", "agent card skill id")
	fs.StringVar(&c.SkillName, "card-skill-name", "Red Hat cluster investigation", "agent card skill name")
	fs.StringVar(&c.SkillDescription, "card-skill-description", "Investigate a Red Hat cluster (or spoke) via the lightspeed-hub agentic stack: analysis-only AgenticRuns with per-run requester credentials.", "agent card skill description")
}

// Validate fills defaults and checks required values.
func (c *Config) Validate() error {
	if c.Namespace == "" {
		ns, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
		if err != nil {
			return fmt.Errorf("namespace is required (flag --namespace or env NAMESPACE)")
		}
		c.Namespace = strings.TrimSpace(string(ns))
	}
	if c.KeycloakIssuer == "" {
		return fmt.Errorf("--keycloak-issuer is required")
	}
	if err := requireHTTPS("--keycloak-issuer", c.KeycloakIssuer); err != nil {
		return err
	}
	if c.KeycloakTokenEndpoint != "" {
		if err := requireHTTPS("--keycloak-token-endpoint", c.KeycloakTokenEndpoint); err != nil {
			return err
		}
	}
	if c.HubAudience == "" {
		return fmt.Errorf("--hub-audience is required")
	}
	if c.SVIDSocketPath == "" && (c.KeycloakClientID == "" || c.KeycloakClientSecret == "") {
		return fmt.Errorf("exchange client auth requires either --keycloak-client-id+secret or --svid-socket")
	}
	if c.WriterIdentity == "" {
		c.WriterIdentity = "adapter-sa"
	}
	return nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
