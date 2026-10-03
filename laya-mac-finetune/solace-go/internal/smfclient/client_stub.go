//go:build !smf

package smfclient

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"laya.local/solacebridge/internal/bridge"
	"laya.local/solacebridge/internal/config"
)

var errSMFBuildRequired = errors.New("SMF transport is not included in the lightweight build; rebuild with 'make build-smf'")

type Client struct{}

func Available() bool { return false }

func New(config.Config, func(bridge.Incoming) error, *slog.Logger) (*Client, error) {
	return nil, errSMFBuildRequired
}

func (c *Client) Connect(context.Context) error { return errSMFBuildRequired }

func (c *Client) Publish(context.Context, string, byte, []byte) error {
	return errSMFBuildRequired
}

func (c *Client) StopIntake() error { return nil }

func (c *Client) Disconnect(time.Duration) {}
