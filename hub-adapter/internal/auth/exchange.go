package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// requireHTTPS enforces https-only exchange endpoints: the issuer and the
// token endpoint carry bearer tokens, so a plaintext leg would expose
// Token A/H in transit. Shared with internal/config (flag-side) so both
// layers fail loudly at startup, before any request is served.
func requireHTTPS(name, raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" {
		return fmt.Errorf("%s must be a valid https:// URL, got %q", name, raw)
	}
	if u.Scheme != "https" {
		return fmt.Errorf("%s must be an https:// URL, got %q (plaintext token endpoints would expose credentials in transit)", name, raw)
	}
	return nil
}

// RFC 8693 / OAuth constants used by the Keycloak exchange.
const (
	// GrantTypeTokenExchange is the RFC 8693 grant type.
	GrantTypeTokenExchange = "urn:ietf:params:oauth:grant-type:token-exchange"
	// TokenTypeAccessToken is the RFC 8693 access-token type indicator.
	TokenTypeAccessToken = "urn:ietf:params:oauth:token-type:access_token"
	// ClientAssertionTypeSPIFFE authenticates the exchanging client with a
	// SPIFFE JWT-SVID. This is the assertion type this repo's Keycloak
	// build expects for SPIFFE-authenticated clients
	// (agents/AUTHENTICATION.md:254; the value is configurable below only
	// via code change — it is a realm contract, not a deployment knob).
	ClientAssertionTypeSPIFFE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
)

// ErrorKind classifies exchange failures. Callers map kinds to caller-facing
// behavior; token values never appear in errors.
type ErrorKind string

const (
	// ErrorKindNetwork: the token endpoint could not be reached.
	ErrorKindNetwork ErrorKind = "network"
	// ErrorKindInvalidClient: client authentication failed (bad secret,
	// bad/unsatisfiable SVID, unknown client).
	ErrorKindInvalidClient ErrorKind = "invalid_client"
	// ErrorKindInvalidGrant: the subject token was rejected — typically it
	// does not carry the exchanging client as an audience (the documented
	// prerequisite, design §3.2), or it is otherwise unusable.
	ErrorKindInvalidGrant ErrorKind = "invalid_grant"
	// ErrorKindDenied: the exchange was refused for any other OAuth reason
	// (access_denied, invalid_scope, unauthorized_client, ...).
	ErrorKindDenied ErrorKind = "denied"
	// ErrorKindInvalidResponse: the endpoint answered in a way that is not
	// a usable OAuth error nor a usable token response.
	ErrorKindInvalidResponse ErrorKind = "invalid_response"
)

// ExchangeError is the typed error returned by TokenExchanger.Exchange.
type ExchangeError struct {
	Kind        ErrorKind
	Code        string // OAuth "error" field, if any
	Description string // OAuth "error_description" field, if any
	HTTPStatus  int
}

func (e *ExchangeError) Error() string {
	msg := fmt.Sprintf("token exchange failed (%s)", e.Kind)
	if e.HTTPStatus != 0 {
		msg += fmt.Sprintf(" http=%d", e.HTTPStatus)
	}
	if e.Code != "" {
		msg += fmt.Sprintf(" code=%q", e.Code)
	}
	if e.Description != "" {
		msg += fmt.Sprintf(" description=%q", redact(e.Description))
	}
	return msg
}

// redact defensively scrubs anything that looks like a JWT out of
// provider-supplied error descriptions. Keycloak descriptions do not
// normally contain tokens, but this is a credential path — fail safe.
func redact(s string) string {
	if looksLikeJWT(s) {
		return "<redacted>"
	}
	return s
}

func looksLikeJWT(s string) bool {
	parts := strings.Split(strings.TrimSpace(s), ".")
	return len(parts) == 3 && len(parts[0]) > 8 && len(parts[2]) > 8
}

// Exchanger is the seam through which the server layer performs exchanges.
// *TokenExchanger implements it; tests use stubs.
type Exchanger interface {
	// Exchange trades the caller's Token A (subject token) for a hub-audience
	// Token H. The returned value must never be logged.
	Exchange(ctx context.Context, subjectToken string) (string, error)
}

// ExchangerConfig configures a TokenExchanger.
//
// Client authentication is EITHER confidential-client secret
// (ClientID+ClientSecret set, SVIDSource nil) OR SPIFFE JWT-SVID
// (SVIDSource set; ClientID/ClientSecret ignored). In SVID mode the token
// request omits client_id entirely — a SPIFFE sub never equals a Keycloak
// client_id, and this repo's Keycloak build rejects the request outright if
// a non-matching client_id is present (agents/AUTHENTICATION.md:529-537).
type ExchangerConfig struct {
	// IssuerURL is the Keycloak realm issuer (also the audience requested
	// on the SVID client assertion — FederatedJWTClientValidator defaults
	// to the realm issuer, agents/AUTHENTICATION.md:538-544).
	IssuerURL string
	// TokenEndpoint is the full token endpoint URL. Empty => derived from
	// IssuerURL as "<issuer>/protocol/openid-connect/token" (Keycloak
	// convention).
	TokenEndpoint string
	// ClientID / ClientSecret: confidential-client mode.
	ClientID     string
	ClientSecret string
	// SVIDSource: SPIFFE client-assertion mode (takes precedence).
	SVIDSource SVIDSource
	// Audience is the requested audience for the exchanged Token H (the
	// hub audience Token H must carry).
	Audience string
	// HTTPClient overrides the default client (tests inject httptest
	// transports here).
	HTTPClient *http.Client
	// Logger receives debug lines. Never receives token values.
	Logger *slog.Logger
}

func (c ExchangerConfig) endpoint() string {
	if c.TokenEndpoint != "" {
		return c.TokenEndpoint
	}
	return strings.TrimSuffix(c.IssuerURL, "/") + "/protocol/openid-connect/token"
}

// TokenExchanger performs RFC 8693 token exchange against Keycloak: a port
// of the external a2a-split fork's KeycloakTokenExchanger logic to Go, as
// plain HTTP form POSTs (no gRPC on this leg; the only gRPC in the package
// is the Workload API SVID fetch).
//
// Documented Keycloak prerequisites (charts/all/keycloak-oidc/README.md
// "Spoke-gateway token exchanger", agents/AUTHENTICATION.md):
//   - the exchanging client needs standard.token.exchange.enabled;
//   - the subject token (Token A) must already carry the exchanging client
//     as an audience (audience mappers on the caller's client) — the
//     "audience" request parameter only narrows, never adds;
//   - in SVID mode, the JWT-SVID must be requested with the realm issuer
//     URL as its audience, and client_id must be omitted;
//   - after a successful exchange sub is preserved but azp becomes the
//     exchanger — hub-side policy keys on sub/groups, azp is provenance.
type TokenExchanger struct {
	cfg ExchangerConfig
}

var _ Exchanger = (*TokenExchanger)(nil)

// NewTokenExchanger validates the configuration and returns an exchanger.
func NewTokenExchanger(cfg ExchangerConfig) (*TokenExchanger, error) {
	if cfg.IssuerURL == "" {
		return nil, errors.New("auth: issuer URL is required")
	}
	if err := requireHTTPS("auth: issuer URL", cfg.IssuerURL); err != nil {
		return nil, err
	}
	if cfg.TokenEndpoint != "" {
		if err := requireHTTPS("auth: token endpoint", cfg.TokenEndpoint); err != nil {
			return nil, err
		}
	}
	if cfg.Audience == "" {
		return nil, errors.New("auth: exchange audience is required")
	}
	if cfg.SVIDSource == nil {
		if cfg.ClientID == "" || cfg.ClientSecret == "" {
			return nil, errors.New("auth: either client id+secret or an SVID source is required")
		}
	} else if cfg.ClientID != "" {
		return nil, errors.New("auth: client_id is not sent in SVID mode; leave ClientID empty to avoid misconfiguration")
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 30 * time.Second}
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	return &TokenExchanger{cfg: cfg}, nil
}

// buildForm assembles the token-endpoint form fields. Split out so tests can
// assert the exact request shape without an HTTP round trip.
func (t *TokenExchanger) buildForm(ctx context.Context, subjectToken string) (url.Values, error) {
	form := url.Values{}
	form.Set("grant_type", GrantTypeTokenExchange)
	form.Set("subject_token", subjectToken)
	form.Set("subject_token_type", TokenTypeAccessToken)
	form.Set("requested_token_type", TokenTypeAccessToken)
	form.Set("audience", t.cfg.Audience)

	if t.cfg.SVIDSource != nil {
		// The SVID must be requested with the realm issuer as audience
		// (documented prerequisite). client_id is deliberately omitted.
		assertion, err := t.cfg.SVIDSource.FetchJWTSVID(ctx, t.cfg.IssuerURL)
		if err != nil {
			return nil, &ExchangeError{Kind: ErrorKindInvalidClient, Code: "svid_fetch_failed"}
		}
		form.Set("client_assertion_type", ClientAssertionTypeSPIFFE)
		form.Set("client_assertion", assertion)
	} else {
		form.Set("client_id", t.cfg.ClientID)
		form.Set("client_secret", t.cfg.ClientSecret)
	}
	return form, nil
}

type tokenResponse struct {
	AccessToken      string `json:"access_token"`
	Error            string `json:"error"`
	ErrorDescription string `json:"error_description"`
}

// Exchange performs the A→H exchange. On failure it returns an
// *ExchangeError; the subject and exchanged token values never appear in
// any error or log line.
func (t *TokenExchanger) Exchange(ctx context.Context, subjectToken string) (string, error) {
	form, err := t.buildForm(ctx, subjectToken)
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.cfg.endpoint(),
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", &ExchangeError{Kind: ErrorKindNetwork, Code: "build_request_failed"}
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := t.cfg.HTTPClient.Do(req)
	if err != nil {
		return "", &ExchangeError{Kind: ErrorKindNetwork, Code: "request_failed"}
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", &ExchangeError{Kind: ErrorKindNetwork, Code: "read_body_failed", HTTPStatus: resp.StatusCode}
	}

	var parsed tokenResponse
	parseErr := json.Unmarshal(body, &parsed)

	// A 200 with an OAuth error body still means failure.
	if resp.StatusCode == http.StatusOK && parseErr == nil && parsed.Error == "" {
		if parsed.AccessToken == "" {
			return "", &ExchangeError{Kind: ErrorKindInvalidResponse, HTTPStatus: resp.StatusCode}
		}
		t.cfg.Logger.Debug("token exchange succeeded",
			"endpoint", t.cfg.endpoint(),
			"subject_fp", Fingerprint(subjectToken),
			"exchanged_fp", Fingerprint(parsed.AccessToken))
		return parsed.AccessToken, nil
	}

	xerr := &ExchangeError{HTTPStatus: resp.StatusCode}
	if parseErr == nil {
		xerr.Code = parsed.Error
		xerr.Description = parsed.ErrorDescription
	}
	switch {
	case xerr.Code == "invalid_client" || xerr.Code == "unauthorized_client" || resp.StatusCode == http.StatusUnauthorized:
		xerr.Kind = ErrorKindInvalidClient
	case xerr.Code == "invalid_grant":
		xerr.Kind = ErrorKindInvalidGrant
	case parseErr != nil && resp.StatusCode >= 200 && resp.StatusCode < 300:
		xerr.Kind = ErrorKindInvalidResponse
	default:
		// access_denied and every other OAuth error code.
		xerr.Kind = ErrorKindDenied
	}
	t.cfg.Logger.Warn("token exchange failed",
		"endpoint", t.cfg.endpoint(),
		"subject_fp", Fingerprint(subjectToken),
		"kind", string(xerr.Kind),
		"http", resp.StatusCode,
		"code", xerr.Code)
	return "", xerr
}
