package auth

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
)

// ErrNoCredential indicates the inbound request carried no bearer token.
// Praxis is the caller gate; if this fires, the request bypassed Praxis or
// Praxis is misconfigured — fail closed.
var ErrNoCredential = fmt.Errorf("no bearer credential on inbound request")

// ErrProtectedChannelUnimplemented is returned by ProtectedChannelSource.
var ErrProtectedChannelUnimplemented = errors.New("protected-channel Token A handoff is not implemented yet (design §3.3 / §10 Q3; lands with the Praxis-side exchange placement)")

// Credential is the protected Token A handoff (design §3.3) plus its
// binding metadata.
//
// BINDING CONTRACT (design §3.3, enforced by every consumer):
//   - TokenA is the caller's ORIGINAL token. It must reach the run
//     credential path (the ls-run-token-{runUID} Secret) and nowhere else:
//     never in CR fields, never in logs, never in Result CRs, never in
//     responses, and never in requests to either kube API.
//   - TokenA is bound to the verified principal (Subject/Issuer as
//     validated by Praxis upstream), and once a run exists, to that run's
//     immutable UID and immutable spec.targetCluster. Consumers must reject
//     or overwrite spoofed binding metadata, never merge it.
//   - TokenH is the hub-audience exchange of A. Empty when the source
//     itself does not exchange (adapter-fallback mode: the server layer
//     exchanges A→H via the Exchanger).
type Credential struct {
	TokenA      string
	TokenH      string // optional; set only by sources that exchange upstream
	Subject     string // verified sub (per Praxis validation)
	Issuer      string
	Fingerprint string // safe log/audit identifier for TokenA
}

// CredentialSource extracts the protected Token A from an inbound request.
//
// The adapter-fallback placement (design §3.2): Praxis validates Token A
// (signature/issuer/audience/allow-list) and forwards it unchanged in the
// Authorization header; the adapter then exchanges A→H itself. The
// Praxis-side placement (preferred, gated on design §10 Q2/Q3) will hand
// both A (protected channel) and H (outbound header) to the adapter.
type CredentialSource interface {
	Credential(ctx context.Context, r *http.Request) (*Credential, error)
}

// HeaderSource is the adapter-fallback CredentialSource: Token A arrives in
// the inbound Authorization header, already validated by Praxis. This
// implementation performs NO signature validation itself — it only parses
// (unverified) the sub/iss claims to bind the credential to a principal.
// The authoritative validation remains Praxis's; if Praxis is bypassed this
// source trusts a forged token, which is why the adapter Service is
// internal-only behind Praxis (design §1).
type HeaderSource struct{}

var _ CredentialSource = HeaderSource{}

// Credential reads the Bearer token from the request and derives its
// binding metadata.
func (HeaderSource) Credential(_ context.Context, r *http.Request) (*Credential, error) {
	header := r.Header.Get("Authorization")
	token, ok := cutBearer(header)
	if !ok || token == "" {
		return nil, ErrNoCredential
	}
	claims, err := UnverifiedClaims(token)
	if err != nil {
		return nil, fmt.Errorf("inbound credential: %w", err)
	}
	sub, _ := claims["sub"].(string)
	iss, _ := claims["iss"].(string)
	if sub == "" {
		// A bearer token without sub cannot be bound to a principal.
		return nil, fmt.Errorf("inbound credential has no sub claim")
	}
	return &Credential{
		TokenA:      token,
		Subject:     sub,
		Issuer:      iss,
		Fingerprint: Fingerprint(token),
	}, nil
}

func cutBearer(header string) (string, bool) {
	prefix, token, found := strings.Cut(header, " ")
	if !found || !strings.EqualFold(prefix, "Bearer") {
		if header != "" && !strings.Contains(header, " ") {
			// Some clients send the bare token.
			return header, true
		}
		return "", false
	}
	return strings.TrimSpace(token), true
}

// ProtectedChannelSource is the PLACEHOLDER for the Praxis-side protected
// Token A handoff (design §3.3): Praxis performs the A→H exchange, replaces
// the outbound Authorization header with H, and delivers A to the adapter's
// run-credential path through a confidential side channel (mechanism gated
// on design §10 Q3 / the PPE version gate Q2 — NOT built in M1/M2).
//
// It exists so the CredentialSource interface documents both placements of
// the exchange behind one contract (design §11, M1 row), and so wiring in
// main.go makes the choice explicit. When implemented, it will populate
// both TokenA (from the side channel) and TokenH (from the inbound header),
// and the server layer will skip its own exchange.
type ProtectedChannelSource struct{}

var _ CredentialSource = ProtectedChannelSource{}

// Credential always fails until the protected channel exists.
func (ProtectedChannelSource) Credential(context.Context, *http.Request) (*Credential, error) {
	return nil, ErrProtectedChannelUnimplemented
}
