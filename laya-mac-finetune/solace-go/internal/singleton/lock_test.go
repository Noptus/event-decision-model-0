//go:build darwin || linux

package singleton

import (
	"errors"
	"testing"
)

func TestSameIdentityCannotRunTwice(t *testing.T) {
	directory := t.TempDir()
	first, err := Acquire(directory, "smf", "broker", "vpn", "client", "store")
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if _, err := Acquire(directory, "smf", "broker", "vpn", "client", "store"); !errors.Is(err, ErrAlreadyRunning) {
		t.Fatalf("expected ErrAlreadyRunning, got %v", err)
	}
}

func TestDifferentIdentityCanRunConcurrentlyAndLockCanBeReacquired(t *testing.T) {
	directory := t.TempDir()
	first, err := Acquire(directory, "mqtt", "broker-a", "vpn", "client", "store")
	if err != nil {
		t.Fatal(err)
	}
	second, err := Acquire(directory, "mqtt", "broker-b", "vpn", "client", "store")
	if err != nil {
		t.Fatal(err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Acquire(directory, "mqtt", "broker-a", "vpn", "client", "store")
	if err != nil {
		t.Fatal(err)
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
}
