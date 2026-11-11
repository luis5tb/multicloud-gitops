package run

import (
	"errors"
	"testing"
)

func TestResolveTarget(t *testing.T) {
	cases := []struct {
		name      string
		headers   []string
		metadata  any
		dataParts []map[string]any
		want      string
		wantErr   error
	}{
		{name: "header only", headers: []string{"spoke-a"}, want: "spoke-a"},
		{name: "metadata only", metadata: "spoke-b", want: "spoke-b"},
		{name: "data part only", dataParts: []map[string]any{{"targetCluster": "spoke-c"}}, want: "spoke-c"},
		{
			name:     "header and metadata agree",
			headers:  []string{"spoke-a"},
			metadata: "spoke-a",
			want:     "spoke-a",
		},
		{name: "none → hub-local", want: ""},
		{
			name:    "duplicate header values (OR-match hazard)",
			headers: []string{"spoke-a", "spoke-a"},
			wantErr: ErrTargetDuplicate,
		},
		{
			name:    "conflicting header values",
			headers: []string{"spoke-a", "spoke-b"},
			wantErr: ErrTargetDuplicate,
		},
		{
			name:     "header vs metadata conflict",
			headers:  []string{"spoke-a"},
			metadata: "spoke-b",
			wantErr:  ErrTargetConflict,
		},
		{
			name:      "header vs data part conflict",
			headers:   []string{"spoke-a"},
			dataParts: []map[string]any{{"targetCluster": "spoke-b"}},
			wantErr:   ErrTargetConflict,
		},
		{
			name:      "two data parts carrying target",
			dataParts: []map[string]any{{"targetCluster": "spoke-a"}, {"targetCluster": "spoke-a"}},
			wantErr:   ErrTargetDuplicate,
		},
		{
			name:     "metadata wrong type",
			metadata: 42,
			wantErr:  ErrTargetInvalid,
		},
		{
			name:      "data part wrong type",
			dataParts: []map[string]any{{"targetCluster": 42}},
			wantErr:   ErrTargetInvalid,
		},
		{
			name:    "not a DNS subdomain",
			headers: []string{"Spoke_A"},
			wantErr: ErrTargetInvalid,
		},
		{
			name:    "empty header value ignored",
			headers: []string{"  "},
			want:    "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ResolveTarget(tc.headers, tc.metadata, tc.dataParts)
			if tc.wantErr != nil {
				if !errors.Is(err, tc.wantErr) {
					t.Fatalf("err = %v, want %v", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Errorf("target = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestTruncateLabelValueMatchesRepoC(t *testing.T) {
	// Exact port of repo C spoke.go:69-75 — the adapter's label values
	// must byte-match what repo C computes from spec.targetCluster.
	short := "spoke-a"
	if got := TruncateLabelValue(short); got != short {
		t.Errorf("short value changed: %q", got)
	}
	long := "spoke-with-a-very-long-name-exceeding-the-kubernetes-label-value-limit-of-63-characters"
	got := TruncateLabelValue(long)
	if len(got) != 63 {
		t.Fatalf("len = %d, want 63", len(got))
	}
	if got[:55] != long[:55] || got[55] != '-' {
		t.Errorf("truncation shape wrong: %q", got)
	}
	if got == long[:63] {
		t.Error("must carry a hash suffix, not a plain truncation")
	}
	// Deterministic.
	if TruncateLabelValue(long) != got {
		t.Error("truncation must be deterministic")
	}
	// Empty target (hub-local run) passes through as the empty value,
	// matching repo C's label write for empty spec.targetCluster.
	if got := TruncateLabelValue(""); got != "" {
		t.Errorf("empty value changed: %q", got)
	}
}
