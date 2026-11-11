package server

import (
	"encoding/json"
	"net/http"

	"github.com/a2aproject/a2a-go/a2a"
)

// CardConfig holds the agent-card fields derived from deployment config
// (design §2.1). The card advertises one skill — Red Hat cluster
// investigation via lightspeed-hub — text input, text + structured output,
// and streaming support.
type CardConfig struct {
	Name             string
	Description      string
	URL              string // the externally reachable origin (through Praxis)
	Version          string
	ProtocolVersion  string
	SkillID          string
	SkillName        string
	SkillDescription string
	Tags             []string
}

// AgentCard builds the a2a.AgentCard from config.
func AgentCard(cfg CardConfig) *a2a.AgentCard {
	protocolVersion := cfg.ProtocolVersion
	if protocolVersion == "" {
		protocolVersion = "0.3.0"
	}
	return &a2a.AgentCard{
		Name:               cfg.Name,
		Description:        cfg.Description,
		URL:                cfg.URL,
		Version:            cfg.Version,
		ProtocolVersion:    protocolVersion,
		PreferredTransport: a2a.TransportProtocolJSONRPC,
		Capabilities: a2a.AgentCapabilities{
			Streaming:              true,
			PushNotifications:      false,
			StateTransitionHistory: false,
		},
		DefaultInputModes:  []string{"text/plain"},
		DefaultOutputModes: []string{"text/plain", "application/json"},
		Skills: []a2a.AgentSkill{
			{
				ID:          cfg.SkillID,
				Name:        cfg.SkillName,
				Description: cfg.SkillDescription,
				Tags:        cfg.Tags,
			},
		},
	}
}

// ServeAgentCard returns an http.Handler serving the agent card JSON at
// /.well-known/agent-card.json (design §2.1).
func ServeAgentCard(card *a2a.AgentCard) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		if err := enc.Encode(card); err != nil {
			http.Error(w, "encode agent card: "+err.Error(), http.StatusInternalServerError)
		}
	})
}
