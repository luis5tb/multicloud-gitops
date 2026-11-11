// Package auth implements the hub adapter's M1 authentication contract:
// the RFC 8693 A→H token exchange (design §3.2, adapter-fallback placement),
// the local Token A expiry pre-check (design §6.2, write side), and the
// protected Token A handoff sources (design §3.3).
//
// Invariant enforced throughout this package: token VALUES are never logged,
// never embedded in errors, and never returned in API responses. Logging uses
// short SHA-256 fingerprints only.
package auth

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// ErrNoExpiry indicates the JWT payload carries no exp claim, so a local
// expiry pre-check is impossible. Callers should treat this as "cannot
// pre-check" and let the authoritative validators (Praxis / Keycloak)
// decide, rather than failing the request.
var ErrNoExpiry = fmt.Errorf("jwt has no exp claim")

// ErrMalformedToken indicates the string is not a decodable JWS compact
// serialization.
var ErrMalformedToken = fmt.Errorf("malformed jwt")

// UnverifiedClaims decodes a JWT's payload without verifying the signature.
//
// SECURITY: this is a cheap, local inspection ONLY. It is used to read
// binding metadata (sub, iss, exp) for pre-checks and record binding — the
// authoritative signature/issuer/audience validation happens upstream at
// Praxis and at Keycloak. Never make an authorization decision from these
// claims alone.
func UnverifiedClaims(token string) (map[string]any, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, ErrMalformedToken
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("%w: payload base64: %v", ErrMalformedToken, err)
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		return nil, fmt.Errorf("%w: payload json: %v", ErrMalformedToken, err)
	}
	return claims, nil
}

// TokenExpiry returns the exp claim as a time.Time. It performs NO signature
// validation (see UnverifiedClaims).
func TokenExpiry(token string) (time.Time, error) {
	claims, err := UnverifiedClaims(token)
	if err != nil {
		return time.Time{}, err
	}
	raw, ok := claims["exp"]
	if !ok {
		return time.Time{}, ErrNoExpiry
	}
	// JWT numeric dates decode as float64 through encoding/json.
	expFloat, ok := raw.(float64)
	if !ok {
		return time.Time{}, fmt.Errorf("%w: exp claim is %T, not a numeric date", ErrMalformedToken, raw)
	}
	return time.Unix(int64(expFloat), 0), nil
}

// Expired is the local Token A pre-start expiry check (design §6.2, write
// side): before a token is written into a run-token Secret (or an AgenticRun
// is created behind it), check whether it is already dead.
//
// Skew allowance: the token is considered expired only once now is past
// exp + skew, i.e. skew grants a grace period AFTER exp. Rationale: this is
// a pre-check, not the validator — its job is to avoid parking tokens that
// are certainly dead (pod starts hours later behind approvals). A small
// grace after exp avoids spuriously rejecting tokens minted on a host whose
// clock runs slightly behind ours; a token that is genuinely stale is still
// caught by Praxis / the spoke gateway, and repo C's CredentialsExpired
// pause machinery handles the residual case. The authoritative decision is
// never made here.
func Expired(token string, now time.Time, skew time.Duration) (bool, error) {
	exp, err := TokenExpiry(token)
	if err != nil {
		return false, err
	}
	return now.After(exp.Add(skew)), nil
}

// Fingerprint returns a short, non-reversible identifier for a token, safe
// for logs and audit lines. Format: "fp:" + first 12 hex chars of SHA-256.
// This mirrors ACME's token_fingerprint convention (agents/acme_agent).
func Fingerprint(token string) string {
	sum := sha256.Sum256([]byte(token))
	return "fp:" + hex.EncodeToString(sum[:])[:12]
}

// PrincipalID derives the stable principal identifier used to bind tasks,
// runs, and Secrets to the verified caller: "<sub>@<issuer-hash>". The issuer
// is hashed because full issuer URLs are long and sometimes sensitive-ish in
// logs; the sub is kept verbatim because it IS the principal. Two callers
// with the same sub from different issuers get different PrincipalIDs.
func PrincipalID(sub, issuer string) string {
	sum := sha256.Sum256([]byte(issuer))
	return sub + "@" + hex.EncodeToString(sum[:])[:12]
}
