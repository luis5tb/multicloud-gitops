// Package watch is the production glue between controller-runtime informers
// and the server.Watcher (design §7.1): shared cache informers on
// AgenticRun, the Result CR kinds, and AgenticRunApproval in the run
// namespace, authenticated as the adapter SA, feeding events into the task
// store so A2A tasks/get and message/stream SSE push updates.
//
// Watch drop/reconnect is controller-runtime's job: when a watch drops,
// the informer relists and re-delivers every object as an update. The
// watcher's projection is idempotent and the store dedupes unchanged
// projections, so a reconnect neither duplicates nor loses task updates.
// This package is deliberately thin (and therefore not unit-tested — the
// event handling it delegates to is); it contains no logic of its own
// beyond object dispatch.
package watch

import (
	"context"
	"fmt"
	"log/slog"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	ctrlcache "sigs.k8s.io/controller-runtime/pkg/cache"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"
)

// Target receives informer events. Implemented by *server.Watcher.
type Target interface {
	RunChanged(ctx context.Context, run *agenticv1alpha1.AgenticRun)
	RunDeleted(ctx context.Context, run *agenticv1alpha1.AgenticRun)
	ObjectChanged(ctx context.Context, obj interface{})
}

// Start builds the namespaced informer set and blocks until the caches
// sync (then returns; the caches run on ctx).
func Start(ctx context.Context, restCfg *rest.Config, scheme *runtime.Scheme, namespace string, target Target, logger *slog.Logger) error {
	cache, err := ctrlcache.New(restCfg, ctrlcache.Options{
		Scheme:            scheme,
		DefaultNamespaces: map[string]ctrlcache.Config{namespace: {}},
	})
	if err != nil {
		return fmt.Errorf("build informer cache: %w", err)
	}

	objects := []ctrlclient.Object{
		&agenticv1alpha1.AgenticRun{},
		&agenticv1alpha1.AnalysisResult{},
		&agenticv1alpha1.ExecutionResult{},
		&agenticv1alpha1.VerificationResult{},
		&agenticv1alpha1.EscalationResult{},
		&agenticv1alpha1.AgenticRunApproval{},
	}
	for _, obj := range objects {
		informer, err := cache.GetInformer(ctx, obj)
		if err != nil {
			return fmt.Errorf("get informer for %T: %w", obj, err)
		}
		if _, err := informer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
			AddFunc: func(o interface{}) { target.ObjectChanged(ctx, o) },
			UpdateFunc: func(_, cur interface{}) {
				target.ObjectChanged(ctx, cur)
			},
			DeleteFunc: func(o interface{}) {
				run, ok := o.(*agenticv1alpha1.AgenticRun)
				if !ok {
					if tombstone, ok := o.(toolscache.DeletedFinalStateUnknown); ok {
						run, ok = tombstone.Obj.(*agenticv1alpha1.AgenticRun)
					}
				}
				if ok {
					target.RunDeleted(ctx, run)
				}
			},
		}); err != nil {
			return fmt.Errorf("register event handler for %T: %w", obj, err)
		}
	}

	go func() {
		if err := cache.Start(ctx); err != nil && ctx.Err() == nil {
			logger.Error("informer cache stopped", "error", err)
		}
	}()
	if !cache.WaitForCacheSync(ctx) {
		return fmt.Errorf("informer caches failed to sync")
	}
	logger.Info("informer caches synced", "namespace", namespace)
	return nil
}
