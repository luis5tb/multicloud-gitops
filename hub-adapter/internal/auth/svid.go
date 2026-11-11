package auth

import (
	"context"
	"fmt"

	"github.com/spiffe/go-spiffe/v2/svid/jwtsvid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

// SVIDSource supplies a short-lived SPIFFE JWT-SVID to authenticate the
// adapter's Keycloak exchange client (design §3.2: "SVID client_assertion
// requested with the realm issuer as audience").
//
// The SVID is a client credential of the ADAPTER, not of the caller; it never
// leaves this process except as the client_assertion form field of the
// token-endpoint POST.
type SVIDSource interface {
	// FetchJWTSVID returns a JWT-SVID marshalled as a compact JWS for the
	// given audience.
	FetchJWTSVID(ctx context.Context, audience string) (string, error)
}

// SVIDSourceFunc adapts a function to SVIDSource (tests, fakes).
type SVIDSourceFunc func(ctx context.Context, audience string) (string, error)

func (f SVIDSourceFunc) FetchJWTSVID(ctx context.Context, audience string) (string, error) {
	return f(ctx, audience)
}

// WorkloadAPISVIDSource fetches JWT-SVIDs from the SPIFFE Workload API
// (ZTWIM/SPIRE) over a unix domain socket. This is the Go counterpart of
// ACME's spiffe.WorkloadApiClient usage in agents/acme_agent/src/acme_agent/auth.py
// (_spiffe_jwt_svid) — reference only, the shapes match by protocol, not by
// code.
type WorkloadAPISVIDSource struct {
	// SocketPath is the absolute path of the Workload API unix socket
	// (e.g. /run/spire/sockets/agent.sock or the ZTWIM equivalent).
	SocketPath string
}

var _ SVIDSource = (*WorkloadAPISVIDSource)(nil)

// FetchJWTSVID fetches a one-shot JWT-SVID for the audience from the
// Workload API. SVIDs live for minutes, so each exchange call fetches a
// fresh one — no caching, matching ACME's behavior.
func (s *WorkloadAPISVIDSource) FetchJWTSVID(ctx context.Context, audience string) (string, error) {
	svid, err := workloadapi.FetchJWTSVID(ctx, jwtsvid.Params{Audience: audience},
		workloadapi.WithAddr("unix://"+s.SocketPath))
	if err != nil {
		return "", fmt.Errorf("fetch JWT-SVID from workload API %s: %w", s.SocketPath, err)
	}
	marshalled := svid.Marshal()
	if marshalled == "" {
		return "", fmt.Errorf("workload API returned an empty JWT-SVID")
	}
	return marshalled, nil
}
