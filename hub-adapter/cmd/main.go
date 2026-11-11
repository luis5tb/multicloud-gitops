// Command hub-adapter is the lightspeed-hub A2A front door (design:
// docs/LIGHTSPEED_HUB_ADAPTER_DESIGN.md, Task B Phase 1, milestones M1-M3).
//
// It terminates A2A JSON-RPC (message/send, message/stream, tasks/get,
// tasks/cancel, tasks/resubscribe) behind Praxis, maps messages onto
// AgenticRun CRs as the caller (Token H), writes the run-token Secrets
// that repo C's gateway credential mode consumes, watches AgenticRun /
// Result / Approval CRs to push task status, bridges the
// CredentialsExpired pause onto A2A auth-required with bounded renewal
// waits (design §6), and cleans run-token Secrets after terminal runs
// (§5.5). M4 (chart, admission webhook) and M5 (live acceptance matrix)
// are not in this binary.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/a2aproject/a2a-go/a2asrv"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/auth"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/config"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/run"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/server"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
	kubeWatch "github.com/luis5tb/multicloud-gitops/hub-adapter/internal/watch"
)

func main() {
	if err := mainRun(); err != nil {
		slog.Error("hub-adapter exiting", "error", err)
		os.Exit(1)
	}
}

func mainRun() error {
	cfg := &config.Config{}
	cfg.AddFlags(flag.CommandLine)
	flag.Parse()
	if err := cfg.Validate(); err != nil {
		return err
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	// Kubernetes clients. The adapter SA reads runs/secrets; run CREATION
	// happens as the caller (Token H) via the per-request factory.
	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	scheme := runtime.NewScheme()
	if err := agenticv1alpha1.AddToScheme(scheme); err != nil {
		return err
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		return err
	}

	kube, err := run.NewCtrlClient(restCfg, scheme)
	if err != nil {
		return err
	}

	// M1 auth wiring: adapter-fallback placement (design §3.2). Praxis
	// validates Token A and forwards it; the adapter exchanges A→H itself.
	// The Praxis-side placement (preferred, gated on design §10 Q2/Q3)
	// would swap HeaderSource for the protected-channel source.
	exchangeCfg := auth.ExchangerConfig{
		IssuerURL:     cfg.KeycloakIssuer,
		TokenEndpoint: cfg.KeycloakTokenEndpoint,
		ClientID:      cfg.KeycloakClientID,
		ClientSecret:  cfg.KeycloakClientSecret,
		Audience:      cfg.HubAudience,
		Logger:        logger,
	}
	if cfg.SVIDSocketPath != "" {
		exchangeCfg.SVIDSource = &auth.WorkloadAPISVIDSource{SocketPath: cfg.SVIDSocketPath}
	}
	exchanger, err := auth.NewTokenExchanger(exchangeCfg)
	if err != nil {
		return err
	}

	handler, err := server.New(server.Config{
		Namespace:            cfg.Namespace,
		TargetHeader:         cfg.TargetHeader,
		ClockSkew:            cfg.ClockSkew,
		StreamDeadline:       cfg.StreamDeadline,
		RenewalTimeout:       cfg.RenewalTimeout,
		MaxPauses:            cfg.MaxPauses,
		TerminalCleanupGrace: cfg.TerminalCleanupGrace,
		SweepInterval:        cfg.SweepInterval,
	}, server.Deps{
		Store:       store.New(),
		Credentials: auth.HeaderSource{},
		Exchanger:   exchanger,
		Readers:     kube,
		Results:     kube,
		Submitters:  run.NewCtrlRunClientFactory(restCfg, scheme),
		Runs: &run.Creator{
			Namespace: cfg.Namespace,
			AgentName: cfg.RunAgent,
			WrittenBy: cfg.WriterIdentity,
		},
		Tokens: &run.TokenWriter{
			Secrets:   kube,
			Namespace: cfg.Namespace,
			Identity:  cfg.WriterIdentity,
		},
		Logger: logger,
	})
	if err != nil {
		return err
	}

	// Restart recovery (design §2.3): rebuild task records from
	// AgenticRun annotations before serving traffic. The M3 informers
	// rehydrate anything this list misses as their caches sync.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := handler.Recover(ctx); err != nil {
		return err
	}

	// M3 watch wiring (design §7.1): informers on AgenticRun + Result CRs
	// + AgenticRunApproval feed the watcher, which projects changes into
	// the task store; the sweeper enforces the §6.4 pause deadlines and
	// the §5.5 terminal-run Secret cleanup.
	watcher, err := handler.NewWatcher()
	if err != nil {
		return err
	}
	if err := kubeWatch.Start(ctx, restCfg, scheme, cfg.Namespace, watcher, logger); err != nil {
		return fmt.Errorf("start informers: %w", err)
	}
	go watcher.Start(ctx)

	// HTTP surface: the agent card plus the JSON-RPC/SSE A2A endpoint.
	// Plain HTTP is deliberate — the Service is internal-only behind
	// Praxis, and TLS terminates at the mesh/Praxis boundary (design §1).
	mux := http.NewServeMux()
	// /healthz: unauthenticated liveness/readiness — safe because the
	// Service is internal-only behind the NetworkPolicy (Praxis-namespace
	// ingress); probes must not need a token.
	mux.Handle("/healthz", server.HealthHandler())
	mux.Handle("/.well-known/agent-card.json", server.ServeAgentCard(server.AgentCard(server.CardConfig{
		Name:             cfg.AgentName,
		Description:      cfg.AgentDescription,
		URL:              cfg.AgentURL,
		Version:          cfg.AgentVersion,
		ProtocolVersion:  cfg.AgentProtocolVersion,
		SkillID:          cfg.SkillID,
		SkillName:        cfg.SkillName,
		SkillDescription: cfg.SkillDescription,
		Tags:             cfg.SkillTags,
	})))
	mux.Handle("/", server.WithRequestCapture(a2asrv.NewJSONRPCHandler(handler, a2asrv.WithKeepAlive(30*time.Second))))

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Info("hub-adapter listening", "addr", cfg.ListenAddr, "namespace", cfg.Namespace)
		errCh <- srv.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	select {
	case sig := <-stop:
		logger.Info("shutting down", "signal", sig.String())
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	case err := <-errCh:
		return err
	}
}
