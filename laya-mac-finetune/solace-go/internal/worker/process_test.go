package worker

import (
	"context"
	"encoding/json"
	"errors"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fakeProcess(t *testing.T, mode string) *Process {
	t.Helper()
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is required for subprocess lifecycle tests")
	}
	script, err := filepath.Abs(filepath.Join("..", "..", "testdata", "fake_worker.py"))
	if err != nil {
		t.Fatal(err)
	}
	process, err := New(Config{
		Python:           python,
		Script:           script,
		Model:            mode,
		Device:           "cpu",
		WorkingDirectory: filepath.Dir(script),
		StartupTimeout:   200 * time.Millisecond,
		StopTimeout:      200 * time.Millisecond,
		MaxEventBytes:    8 * 1024 * 1024,
		MaxResponseBytes: 64 * 1024,
	})
	if err != nil {
		t.Fatal(err)
	}
	return process
}

func TestBlockedWriteHonorsDeadlineAndReapsChild(t *testing.T) {
	process := fakeProcess(t, "blocked-write")
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	event := json.RawMessage(`{"blob":"` + strings.Repeat("x", 4*1024*1024) + `"}`)
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Millisecond)
	started := time.Now()
	_, err := process.Infer(ctx, "blocked", event)
	cancel()
	if err == nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected deadline error, got %v", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("blocked write exceeded bounded shutdown: %s", elapsed)
	}
	if process.state != nil {
		t.Fatal("timed-out child was not cleared after reap")
	}
}

func TestChildExitIsReportedAndCleaned(t *testing.T) {
	process := fakeProcess(t, "exit-after-ready")
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, err := process.Infer(ctx, "exit", json.RawMessage(`{"ok":true}`))
	if err == nil || !strings.Contains(err.Error(), "exited") {
		t.Fatalf("expected child exit error, got %v", err)
	}
	if process.state != nil {
		t.Fatal("exited child state was not cleared")
	}
}

func TestStartupTimeoutKillsAndReapsChild(t *testing.T) {
	process := fakeProcess(t, "no-ready")
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Millisecond)
	defer cancel()
	err := process.Start(ctx)
	if err == nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected startup deadline, got %v", err)
	}
	if process.state != nil {
		t.Fatal("startup failure retained process state")
	}
}

func TestCloseUnblocksScannerWithExtraOutput(t *testing.T) {
	process := fakeProcess(t, "extra-output")
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := process.Close(ctx); err == nil {
		// The fake child is killed after the short stop grace period, so a timeout error is expected.
		t.Fatal("expected forced-stop error")
	}
	if process.state != nil {
		t.Fatal("closed child state was not cleared")
	}
}
