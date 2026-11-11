package run

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// MetadataTargetKey is the structured-data key carrying the target cluster
// (design §4.2: the caller passes the target as structured data — message
// metadata or a DataPart — NEVER as an LLM-constructed header value; the
// mapping from tool-argument value to transport header is deterministic
// code in the caller, not prompt text).
const MetadataTargetKey = "targetCluster"

// Target resolution errors (design §4.2 rule 2: reject duplicate/conflicting
// target headers — the pinned router OR-matches repeated header values, so
// a multi-valued header must be a protocol error, and the adapter check is
// defense-in-depth behind the gateway's own rejection).
var (
	ErrTargetDuplicate = errors.New("duplicate target values")
	ErrTargetConflict  = errors.New("conflicting target values")
	ErrTargetInvalid   = errors.New("invalid target value")
)

// targetPattern mirrors the AgenticRun CRD's spec.targetCluster CEL rule
// (agenticrun_types.go:335): a DNS subdomain.
var targetPattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)

// ResolveTarget applies the deterministic target rules (design §4.2):
//
//   - headerValues: the inbound transport header (X-Hub-Cluster by
//     default). MORE THAN ONE header value is always rejected — repeated
//     values are the OR-match hazard, even if equal.
//   - metadataTarget: message.Metadata[targetCluster], if present.
//   - dataParts: data parts of the message; each may carry a
//     targetCluster key. More than one data part carrying the key is a
//     duplicate.
//
// Across sources: equal values are accepted (a caller may set both header
// and metadata deterministically from the same tool argument); differing
// values are a conflict. No value anywhere → "" (hub-local run, valid per
// the CRD; the A2A flow always carries a spoke target, but hub-local is a
// legal AgenticRun shape).
func ResolveTarget(headerValues []string, metadataTarget any, dataParts []map[string]any) (string, error) {
	var candidates []string

	if len(headerValues) > 1 {
		return "", fmt.Errorf("%w: %d values in the target header", ErrTargetDuplicate, len(headerValues))
	}
	for _, hv := range headerValues {
		if v := trimNonEmpty(hv); v != "" {
			candidates = append(candidates, v)
		}
	}

	switch mt := metadataTarget.(type) {
	case nil:
	case string:
		if v := trimNonEmpty(mt); v != "" {
			candidates = append(candidates, v)
		}
	default:
		return "", fmt.Errorf("%w: metadata %q must be a string, got %T", ErrTargetInvalid, MetadataTargetKey, metadataTarget)
	}

	seenInParts := 0
	for _, dp := range dataParts {
		raw, ok := dp[MetadataTargetKey]
		if !ok {
			continue
		}
		seenInParts++
		v, ok := raw.(string)
		if !ok {
			return "", fmt.Errorf("%w: data part %q must be a string, got %T", ErrTargetInvalid, MetadataTargetKey, raw)
		}
		if v = trimNonEmpty(v); v != "" {
			candidates = append(candidates, v)
		}
	}
	if seenInParts > 1 {
		return "", fmt.Errorf("%w: %d data parts carry %q", ErrTargetDuplicate, seenInParts, MetadataTargetKey)
	}

	if len(candidates) == 0 {
		return "", nil // hub-local run
	}
	first := candidates[0]
	for _, c := range candidates[1:] {
		if c != first {
			return "", fmt.Errorf("%w: %q vs %q", ErrTargetConflict, first, c)
		}
	}
	if !targetPattern.MatchString(first) {
		return "", fmt.Errorf("%w: %q is not a DNS subdomain", ErrTargetInvalid, first)
	}
	return first, nil
}

func trimNonEmpty(s string) string {
	return strings.TrimSpace(s)
}
