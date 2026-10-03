package worker

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRealSavedModelWorker(t *testing.T) {
	if os.Getenv("LAYA_REAL_WORKER_TEST") != "1" {
		t.Skip("set LAYA_REAL_WORKER_TEST=1 to load the local checkpoint")
	}
	projectRoot := os.Getenv("LAYA_PROJECT_ROOT")
	if projectRoot == "" {
		t.Fatal("LAYA_PROJECT_ROOT is required")
	}
	process, err := New(Config{
		Python:           filepath.Join(projectRoot, ".venv", "bin", "python"),
		Script:           filepath.Join(projectRoot, "solace-go", "python_worker.py"),
		Model:            filepath.Join(projectRoot, "outputs", "best-model"),
		Device:           "mps",
		WorkingDirectory: filepath.Join(projectRoot, "solace-go"),
		StartupTimeout:   90 * time.Second,
		StopTimeout:      10 * time.Second,
		MaxEventBytes:    256 * 1024,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	event := json.RawMessage(`{"topic":"acme/prod/eu/payments/failed/v1","schema_name":"PaymentFailed","schema_version":"1.0","event_type":"payment.failed","payload":{"event_id":"worker-smoke-1","amount":120,"currency":"EUR","message":"Le paiement a été refusé"}}`)
	inferenceContext, inferenceCancel := context.WithTimeout(context.Background(), 10*time.Second)
	decision, err := process.Infer(inferenceContext, "worker-smoke-1", event)
	inferenceCancel()
	if err != nil {
		t.Fatal(err)
	}
	if decision.SelectedRoute == "" || len(decision.Probabilities) != 6 || decision.Device != "mps" {
		t.Fatalf("unexpected decision: %#v", decision)
	}
	if decision.Usage.InputTokens == 0 || decision.Usage.Truncated || decision.Usage.StateTokensDropped != 0 {
		t.Fatalf("missing or unexpected native truncation metadata: %#v", decision.Usage)
	}
	total := 0.0
	for _, probability := range decision.Probabilities {
		total += probability
	}
	if total < 0.999 || total > 1.001 {
		t.Fatalf("probabilities sum to %f", total)
	}
	closeContext, closeCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer closeCancel()
	if err := process.Close(closeContext); err != nil {
		t.Fatal(err)
	}
}
