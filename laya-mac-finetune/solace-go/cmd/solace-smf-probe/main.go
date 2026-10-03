//go:build smf

package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"time"

	"laya.local/solacebridge/internal/bridge"
	"laya.local/solacebridge/internal/config"
	"laya.local/solacebridge/internal/smfclient"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg, err := config.Parse(os.Args[1:], os.Getenv, os.Stderr)
	if err != nil {
		logger.Error("invalid configuration", "error", err)
		os.Exit(2)
	}
	if cfg.Transport != "smf" {
		logger.Error("probe requires --transport smf")
		os.Exit(2)
	}
	client, err := smfclient.New(cfg, func(message bridge.Incoming) error {
		message.Release()
		return errors.New("probe does not consume messages")
	}, logger)
	if err != nil {
		logger.Error("create SMF client", "error", err)
		os.Exit(1)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cfg.StartupTimeout)
	err = client.Connect(ctx)
	cancel()
	if err != nil {
		logger.Error("SMF queue probe failed", "queue", cfg.SMFQueue, "error", err)
		os.Exit(1)
	}
	if err := client.StopIntake(); err != nil {
		logger.Warn("pause probe receiver", "error", err)
	}
	client.Disconnect(2 * time.Second)
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
		"status": "ok",
		"transport": "smf",
		"queue": cfg.SMFQueue,
		"input_filter": cfg.InputFilter,
		"provision_requested": cfg.SMFProvision,
		"subscription_requested": cfg.SMFAddSubscription,
	})
}
