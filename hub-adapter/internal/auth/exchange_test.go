package auth

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// makeJWT builds a structurally-valid JWT with arbitrary claims (no valid
// signature — nothing in this package verifies signatures).
func makeJWT(t *testing.T, claims map[string]any) string {
	t.Helper()
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	payload, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal claims: %v", err)
	}
	return header + "." + base64.RawURLEncoding.EncodeToString(payload) + ".c2ln"
}

// captureLogger returns a logger writing to buf.
func captureLogger(buf *bytes.Buffer) *slog.Logger {
	return slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

// newFakeKeycloak returns an httptest TLS server recording the last received
// form and responding with the given status and body. TLS because the
// exchanger enforces https-only token endpoints (callers pass srv.Client()
// as HTTPClient to trust the self-signed test cert).
func newFakeKeycloak(t *testing.T, status int, body string, lastForm *url.Values) *httptest.Server {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse form: %v", err)
		}
		*lastForm = r.PostForm
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestExchangeRequestShapeSecretMode(t *testing.T) {
	var form url.Values
	srv := newFakeKeycloak(t, 200, `{"access_token":"token-h-value"}`, &form)

	ex, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:     "https://kc.example/realms/hub",
		TokenEndpoint: srv.URL + "/token",
		HTTPClient:    srv.Client(),
		ClientID:      "adapter-exchange",
		ClientSecret:  "s3cret",
		Audience:      "hub-api",
	})
	if err != nil {
		t.Fatal(err)
	}
	subject := makeJWT(t, map[string]any{"sub": "acme-agent", "exp": 12345.0})
	tokenH, err := ex.Exchange(context.Background(), subject)
	if err != nil {
		t.Fatal(err)
	}
	if tokenH != "token-h-value" {
		t.Fatalf("got %q", tokenH)
	}

	want := map[string]string{
		"grant_type":           GrantTypeTokenExchange,
		"subject_token":        subject,
		"subject_token_type":   TokenTypeAccessToken,
		"requested_token_type": TokenTypeAccessToken,
		"audience":             "hub-api",
		"client_id":            "adapter-exchange",
		"client_secret":        "s3cret",
	}
	for k, v := range want {
		if got := form.Get(k); got != v {
			t.Errorf("form %s = %q, want %q", k, got, v)
		}
	}
	if _, present := form["client_assertion"]; present {
		t.Error("client_assertion must not be sent in secret mode")
	}
}

func TestExchangeRequestShapeSVIDMode(t *testing.T) {
	var form url.Values
	srv := newFakeKeycloak(t, 200, `{"access_token":"token-h-svid"}`, &form)

	svidSource := SVIDSourceFunc(func(_ context.Context, audience string) (string, error) {
		if audience != "https://kc.example/realms/hub" {
			t.Errorf("SVID audience = %q, want the realm issuer", audience)
		}
		return "fake-svid-jwt", nil
	})

	ex, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:     "https://kc.example/realms/hub",
		TokenEndpoint: srv.URL + "/token",
		HTTPClient:    srv.Client(),
		SVIDSource:    svidSource,
		Audience:      "hub-api",
	})
	if err != nil {
		t.Fatal(err)
	}
	subject := makeJWT(t, map[string]any{"sub": "acme-agent"})
	if _, err := ex.Exchange(context.Background(), subject); err != nil {
		t.Fatal(err)
	}

	// The documented SPIFFE rules: client_id OMITTED, client_assertion
	// present with the jwt-spiffe assertion type (AUTHENTICATION.md:529-544).
	if _, present := form["client_id"]; present {
		t.Error("client_id must be omitted in SVID mode")
	}
	if got := form.Get("client_assertion"); got != "fake-svid-jwt" {
		t.Errorf("client_assertion = %q", got)
	}
	if got := form.Get("client_assertion_type"); got != ClientAssertionTypeSPIFFE {
		t.Errorf("client_assertion_type = %q", got)
	}
	if got := form.Get("subject_token"); got != subject {
		t.Error("subject_token not forwarded")
	}
}

func TestExchangeErrorMapping(t *testing.T) {
	subject := makeJWT(t, map[string]any{"sub": "acme-agent"})
	cases := []struct {
		name    string
		status  int
		body    string
		want    ErrorKind
		wantMsg string
	}{
		{"invalid_client", 400, `{"error":"invalid_client","error_description":"Invalid client credentials"}`, ErrorKindInvalidClient, ""},
		{"unauthorized_client", 400, `{"error":"unauthorized_client"}`, ErrorKindInvalidClient, ""},
		{"http401", 401, `{"error":"unauthorized"}`, ErrorKindInvalidClient, ""},
		{"invalid_grant", 400, `{"error":"invalid_grant","error_description":"Subject token is not valid"}`, ErrorKindInvalidGrant, ""},
		{"access_denied", 400, `{"error":"access_denied"}`, ErrorKindDenied, ""},
		{"invalid_scope", 400, `{"error":"invalid_scope"}`, ErrorKindDenied, ""},
		{"ok_but_error_body", 200, `{"error":"access_denied"}`, ErrorKindDenied, ""},
		{"garbage_200", 200, `not json`, ErrorKindInvalidResponse, ""},
		{"missing_token", 200, `{}`, ErrorKindInvalidResponse, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var form url.Values
			srv := newFakeKeycloak(t, tc.status, tc.body, &form)
			ex, err := NewTokenExchanger(ExchangerConfig{
				IssuerURL:     "https://kc.example/realms/hub",
				TokenEndpoint: srv.URL + "/token",
				HTTPClient:    srv.Client(),
				ClientID:      "c",
				ClientSecret:  "s",
				Audience:      "hub-api",
			})
			if err != nil {
				t.Fatal(err)
			}
			_, err = ex.Exchange(context.Background(), subject)
			if err == nil {
				t.Fatal("expected error")
			}
			var xe *ExchangeError
			if !errors.As(err, &xe) {
				t.Fatalf("error %v is not *ExchangeError", err)
			}
			if xe.Kind != tc.want {
				t.Errorf("kind = %q, want %q", xe.Kind, tc.want)
			}
		})
	}
}

func TestExchangeNetworkError(t *testing.T) {
	ex, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:     "https://kc.example/realms/hub",
		TokenEndpoint: "https://127.0.0.1:1/token", // nothing listens here
		ClientID:      "c",
		ClientSecret:  "s",
		Audience:      "hub-api",
		HTTPClient:    &http.Client{Timeout: 2 * time.Second},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = ex.Exchange(context.Background(), makeJWT(t, nil))
	var xe *ExchangeError
	if !errors.As(err, &xe) || xe.Kind != ErrorKindNetwork {
		t.Fatalf("want network ExchangeError, got %v", err)
	}
}

// TestExchangeNeverLogsTokens asserts that neither the subject token nor
// the exchanged token ever appears in the exchanger's log output — only
// fingerprints.
func TestExchangeNeverLogsTokens(t *testing.T) {
	var buf bytes.Buffer
	var form url.Values
	subject := makeJWT(t, map[string]any{"sub": "acme-agent"})
	exchanged := makeJWT(t, map[string]any{"sub": "acme-agent", "aud": "hub-api"})
	respBody, _ := json.Marshal(map[string]string{"access_token": exchanged})
	srv := newFakeKeycloak(t, 200, string(respBody), &form)

	ex, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:     "https://kc.example/realms/hub",
		TokenEndpoint: srv.URL + "/token",
		HTTPClient:    srv.Client(),
		ClientID:      "c",
		ClientSecret:  "s",
		Audience:      "hub-api",
		Logger:        captureLogger(&buf),
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ex.Exchange(context.Background(), subject); err != nil {
		t.Fatal(err)
	}

	// Failure path logs too.
	srv2 := newFakeKeycloak(t, 400, `{"error":"invalid_grant"}`, &form)
	ex2, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:     "https://kc.example/realms/hub",
		TokenEndpoint: srv2.URL + "/token",
		ClientID:      "c",
		ClientSecret:  "s",
		Audience:      "hub-api",
		Logger:        captureLogger(&buf),
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ex2.Exchange(context.Background(), subject); err == nil {
		t.Fatal("expected error")
	}

	out := buf.String()
	for _, forbidden := range []string{subject, exchanged, "c2ln"} {
		if bytes.Contains(buf.Bytes(), []byte(forbidden)) {
			t.Errorf("log output contains token material %q:\n%s", forbidden[:10]+"...", out)
		}
	}
	if !bytes.Contains(buf.Bytes(), []byte("fp:")) {
		t.Errorf("log output should carry fingerprints:\n%s", out)
	}
}

func TestExchangeSVIDFetchFailureIsInvalidClient(t *testing.T) {
	ex, err := NewTokenExchanger(ExchangerConfig{
		IssuerURL:  "https://kc.example/realms/hub",
		SVIDSource: SVIDSourceFunc(func(context.Context, string) (string, error) { return "", errors.New("spire down") }),
		Audience:   "hub-api",
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = ex.Exchange(context.Background(), makeJWT(t, nil))
	var xe *ExchangeError
	if !errors.As(err, &xe) || xe.Kind != ErrorKindInvalidClient {
		t.Fatalf("want invalid_client ExchangeError, got %v", err)
	}
}

func TestNewTokenExchangerValidation(t *testing.T) {
	if _, err := NewTokenExchanger(ExchangerConfig{ClientID: "c", ClientSecret: "s"}); err == nil {
		t.Error("missing issuer must fail")
	}
	if _, err := NewTokenExchanger(ExchangerConfig{IssuerURL: "https://x", ClientID: "c"}); err == nil {
		t.Error("missing secret must fail")
	}
	if _, err := NewTokenExchanger(ExchangerConfig{IssuerURL: "https://x", SVIDSource: SVIDSourceFunc(nil), ClientID: "c"}); err == nil {
		t.Error("client_id in SVID mode must fail (it would be silently wrong at runtime)")
	}
}

// TestNewTokenExchangerHTTPSOnly: the exchange endpoints carry bearer
// tokens, so anything but https is rejected at construction — never
// discovered mid-flight.
func TestNewTokenExchangerHTTPSOnly(t *testing.T) {
	cases := []struct {
		name   string
		cfg    ExchangerConfig
		wantOK bool
	}{
		{
			name: "https issuer and endpoint ok",
			cfg: ExchangerConfig{
				IssuerURL:     "https://kc.example/realms/hub",
				TokenEndpoint: "https://kc.example/realms/hub/protocol/openid-connect/token",
				ClientID:      "c", ClientSecret: "s", Audience: "a",
			},
			wantOK: true,
		},
		{
			name: "http issuer rejected",
			cfg: ExchangerConfig{
				IssuerURL: "http://kc.example/realms/hub",
				ClientID:  "c", ClientSecret: "s", Audience: "a",
			},
		},
		{
			name: "http token endpoint rejected",
			cfg: ExchangerConfig{
				IssuerURL:     "https://kc.example/realms/hub",
				TokenEndpoint: "http://kc.example/realms/hub/protocol/openid-connect/token",
				ClientID:      "c", ClientSecret: "s", Audience: "a",
			},
		},
		{
			name: "garbage scheme rejected",
			cfg: ExchangerConfig{
				IssuerURL: "ftp://kc.example/realms/hub",
				ClientID:  "c", ClientSecret: "s", Audience: "a",
			},
		},
		{
			name: "schemeless garbage rejected",
			cfg: ExchangerConfig{
				IssuerURL: "not a url at all",
				ClientID:  "c", ClientSecret: "s", Audience: "a",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewTokenExchanger(tc.cfg)
			if tc.wantOK && err != nil {
				t.Fatalf("valid https config rejected: %v", err)
			}
			if !tc.wantOK {
				if err == nil {
					t.Fatal("non-https config must be rejected")
				}
				if !strings.Contains(err.Error(), "https") {
					t.Errorf("error must name the https requirement, got: %v", err)
				}
			}
		})
	}
}
