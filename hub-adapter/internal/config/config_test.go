package config

import (
	"strings"
	"testing"
)

// validCfg is the baseline: every required field set, exchange client in
// secret mode, https endpoints.
func validCfg() *Config {
	return &Config{
		Namespace:            "test-ns",
		KeycloakIssuer:       "https://kc.example/realms/hub",
		KeycloakClientID:     "adapter-exchange",
		KeycloakClientSecret: "s3cret",
		HubAudience:          "hub-api",
	}
}

// TestValidate covers the flag-contract rules: SVID-vs-secret exclusivity,
// the namespace fallback, and the https-only exchange endpoints.
func TestValidate(t *testing.T) {
	t.Run("baseline secret mode ok", func(t *testing.T) {
		c := validCfg()
		if err := c.Validate(); err != nil {
			t.Fatalf("valid config rejected: %v", err)
		}
		if c.WriterIdentity != "adapter-sa" {
			t.Errorf("WriterIdentity default = %q, want adapter-sa", c.WriterIdentity)
		}
	})

	t.Run("svid mode ok without client id or secret", func(t *testing.T) {
		c := validCfg()
		c.KeycloakClientID = ""
		c.KeycloakClientSecret = ""
		c.SVIDSocketPath = "unix:///run/spire/sockets/agent.sock"
		if err := c.Validate(); err != nil {
			t.Fatalf("SVID mode rejected: %v", err)
		}
	})

	t.Run("neither svid nor secret rejected", func(t *testing.T) {
		c := validCfg()
		c.KeycloakClientID = ""
		c.KeycloakClientSecret = ""
		err := c.Validate()
		if err == nil || !strings.Contains(err.Error(), "svid-socket") {
			t.Fatalf("want exclusivity error naming --svid-socket, got %v", err)
		}
	})

	t.Run("client id without secret rejected", func(t *testing.T) {
		c := validCfg()
		c.KeycloakClientSecret = ""
		if err := c.Validate(); err == nil {
			t.Fatal("client id without secret must fail")
		}
	})

	t.Run("missing issuer rejected", func(t *testing.T) {
		c := validCfg()
		c.KeycloakIssuer = ""
		if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "keycloak-issuer") {
			t.Fatalf("want issuer-required error, got %v", err)
		}
	})

	t.Run("missing hub audience rejected", func(t *testing.T) {
		c := validCfg()
		c.HubAudience = ""
		if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "hub-audience") {
			t.Fatalf("want audience-required error, got %v", err)
		}
	})

	t.Run("namespace fallback", func(t *testing.T) {
		c := validCfg()
		c.Namespace = ""
		err := c.Validate()
		// Two legal outcomes, both correct: the pod-namespace file exists
		// (in-cluster) and fills the namespace, or it does not and the
		// flag requirement is named. What must NEVER happen: an empty
		// namespace sailing through.
		if err == nil {
			if c.Namespace == "" {
				t.Fatal("namespace still empty after successful Validate")
			}
		} else if !strings.Contains(err.Error(), "namespace is required") {
			t.Fatalf("want namespace-required error, got %v", err)
		}
	})

	httpsCases := []struct {
		name    string
		mutate  func(*Config)
		wantErr string
	}{
		{
			name:    "http issuer rejected",
			mutate:  func(c *Config) { c.KeycloakIssuer = "http://kc.example/realms/hub" },
			wantErr: "--keycloak-issuer must be an https:// URL",
		},
		{
			name:    "http token endpoint rejected",
			mutate:  func(c *Config) { c.KeycloakTokenEndpoint = "http://kc.example/token" },
			wantErr: "--keycloak-token-endpoint must be an https:// URL",
		},
		{
			name:    "garbage scheme rejected",
			mutate:  func(c *Config) { c.KeycloakIssuer = "gopher://kc.example" },
			wantErr: "--keycloak-issuer must be an https:// URL",
		},
		{
			name: "derived token endpoint is fine when empty",
			mutate: func(c *Config) {
				c.KeycloakTokenEndpoint = "" // derived from the https issuer
			},
			wantErr: "",
		},
	}
	for _, tc := range httpsCases {
		t.Run(tc.name, func(t *testing.T) {
			c := validCfg()
			tc.mutate(c)
			err := c.Validate()
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("valid config rejected: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("want error %q, got %v", tc.wantErr, err)
			}
		})
	}
}
