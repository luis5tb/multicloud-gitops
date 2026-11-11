package auth

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func jwtWithClaims(t *testing.T, claims map[string]any) string {
	t.Helper()
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	payload, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	return header + "." + base64.RawURLEncoding.EncodeToString(payload) + ".c2ln"
}

func TestExpired(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	skew := 30 * time.Second
	cases := []struct {
		name    string
		exp     int64
		want    bool
		wantErr bool
	}{
		{"fresh token", now.Unix() + 3600, false, false},
		{"expiring exactly now", now.Unix(), false, false},               // within skew grace
		{"expired 10s ago (within skew)", now.Unix() - 10, false, false}, // grace after exp
		{"expired 31s ago (past skew)", now.Unix() - 31, true, false},
		{"expired an hour ago", now.Unix() - 3600, true, false},
		{"no exp claim", 0, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			claims := map[string]any{"sub": "acme-agent"}
			if tc.exp != 0 {
				claims["exp"] = tc.exp
			}
			token := jwtWithClaims(t, claims)
			got, err := Expired(token, now, skew)
			if tc.wantErr {
				if !errors.Is(err, ErrNoExpiry) {
					t.Fatalf("want ErrNoExpiry, got %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Errorf("Expired = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestExpiredZeroSkew(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	token := jwtWithClaims(t, map[string]any{"exp": now.Unix()})
	got, err := Expired(token, now, 0)
	if err != nil || got {
		t.Errorf("Expired(zero skew, exp==now) = %v, %v; want false (now.After(exp) is false)", got, err)
	}
	token2 := jwtWithClaims(t, map[string]any{"exp": now.Unix() - 1})
	got, err = Expired(token2, now, 0)
	if err != nil || !got {
		t.Errorf("Expired(zero skew, exp==now-1) = %v, %v; want true", got, err)
	}
}

func TestExpiredMalformed(t *testing.T) {
	for _, bad := range []string{"", "not-a-jwt", "a.b", "x.y.z"} {
		if _, err := Expired(bad, time.Now(), 0); !errors.Is(err, ErrMalformedToken) {
			// "x.y.z" has a non-base64 payload, also malformed.
			t.Errorf("Expired(%q) error = %v, want ErrMalformedToken", bad, err)
		}
	}
}

func TestTokenExpiryFloatExp(t *testing.T) {
	// JWT numeric dates may decode as float.
	token := jwtWithClaims(t, map[string]any{"exp": 1_000_100.5})
	exp, err := TokenExpiry(token)
	if err != nil {
		t.Fatal(err)
	}
	if exp.Unix() != 1_000_100 {
		t.Errorf("exp = %v", exp)
	}
}

func TestFingerprint(t *testing.T) {
	a, b := jwtWithClaims(t, nil), jwtWithClaims(t, map[string]any{"sub": "x"})
	fa, fb := Fingerprint(a), Fingerprint(b)
	if fa == fb {
		t.Error("fingerprints collide")
	}
	if len(fa) != 3+12 || fa[:3] != "fp:" {
		t.Errorf("fingerprint format = %q", fa)
	}
	if fa == a {
		t.Error("fingerprint must not be the token")
	}
}

func TestPrincipalID(t *testing.T) {
	p1 := PrincipalID("acme-agent", "https://kc/realms/hub")
	p2 := PrincipalID("acme-agent", "https://other/realms/x")
	p3 := PrincipalID("other-agent", "https://kc/realms/hub")
	if p1 == p2 || p1 == p3 || p2 == p3 {
		t.Error("principal ids must differ")
	}
	if p1 != PrincipalID("acme-agent", "https://kc/realms/hub") {
		t.Error("principal id must be deterministic")
	}
}

func TestUnverifiedClaims(t *testing.T) {
	claims, err := UnverifiedClaims(jwtWithClaims(t, map[string]any{"sub": "s", "iss": "i"}))
	if err != nil {
		t.Fatal(err)
	}
	if claims["sub"] != "s" || claims["iss"] != "i" {
		t.Errorf("claims = %v", claims)
	}
}
