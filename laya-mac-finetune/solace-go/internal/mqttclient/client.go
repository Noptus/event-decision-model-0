package mqttclient

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"

	"laya.local/solacebridge/internal/bridge"
	"laya.local/solacebridge/internal/config"
)

type Client struct {
	client           mqtt.Client
	inputFilter      string
	publishTimeout   time.Duration
	subscribeTimeout time.Duration
	ready            chan error
	logger           *slog.Logger
}

func New(cfg config.Config, submit func(bridge.Incoming) error, logger *slog.Logger) (*Client, error) {
	if err := os.MkdirAll(cfg.StoreDirectory, 0o700); err != nil {
		return nil, fmt.Errorf("create MQTT state directory: %w", err)
	}
	if logger == nil {
		logger = slog.Default()
	}
	ready := make(chan error, 1)
	messageHandler := func(_ mqtt.Client, message mqtt.Message) {
		err := submit(bridge.Incoming{
			Topic:     message.Topic(),
			Payload:   message.Payload(),
			QoS:       message.Qos(),
			Retained:  message.Retained(),
			Duplicate: message.Duplicate(),
			Ack:       message.Ack,
		})
		if err != nil {
			// Manual acknowledgement is enabled. A rejected message is deliberately left
			// unacknowledged so a QoS 1 session can redeliver it after capacity recovers.
			logger.Error("inbound event was not accepted or acknowledged", "topic", message.Topic(), "error", err)
		}
	}

	options := mqtt.NewClientOptions().
		AddBroker(cfg.BrokerURL).
		SetClientID(cfg.ClientID).
		SetProtocolVersion(4).
		SetCleanSession(cfg.CleanSession).
		SetAutoReconnect(true).
		SetConnectRetry(true).
		SetConnectRetryInterval(2 * time.Second).
		SetMaxReconnectInterval(30 * time.Second).
		SetConnectTimeout(15 * time.Second).
		SetWriteTimeout(cfg.PublishTimeout).
		SetKeepAlive(30 * time.Second).
		SetPingTimeout(10 * time.Second).
		SetOrderMatters(false).
		SetAutoAckDisabled(true).
		SetMessageChannelDepth(uint(cfg.QueueCapacity)).
		SetResumeSubs(false).
		SetStore(mqtt.NewFileStore(cfg.StoreDirectory)).
		SetDefaultPublishHandler(messageHandler)
	if cfg.Username != "" {
		options.SetUsername(cfg.Username)
	}
	if cfg.Password != "" {
		options.SetPassword(cfg.Password)
	}
	options.SetConnectionLostHandler(func(_ mqtt.Client, err error) {
		logger.Warn("MQTT connection lost; automatic reconnect remains enabled", "error", err)
	})
	options.SetReconnectingHandler(func(_ mqtt.Client, _ *mqtt.ClientOptions) {
		logger.Info("MQTT reconnecting")
	})
	options.SetOnConnectHandler(func(client mqtt.Client) {
		token := client.Subscribe(cfg.InputFilter, cfg.QoS, messageHandler)
		err := waitToken(context.Background(), token, 15*time.Second)
		if err != nil {
			logger.Error("MQTT subscription failed", "filter", cfg.InputFilter, "error", err)
		} else {
			logger.Info("MQTT subscription active", "filter", cfg.InputFilter, "qos", cfg.QoS)
		}
		select {
		case ready <- err:
		default:
		}
	})

	return &Client{
		client:           mqtt.NewClient(options),
		inputFilter:      cfg.InputFilter,
		publishTimeout:   cfg.PublishTimeout,
		subscribeTimeout: 15 * time.Second,
		ready:            ready,
		logger:           logger,
	}, nil
}

func waitToken(ctx context.Context, token mqtt.Token, fallback time.Duration) error {
	timeout := fallback
	if deadline, ok := ctx.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return ctx.Err()
		}
		if remaining < timeout {
			timeout = remaining
		}
	}
	if !token.WaitTimeout(timeout) {
		if err := ctx.Err(); err != nil {
			return err
		}
		return fmt.Errorf("MQTT operation timed out after %s", timeout)
	}
	return token.Error()
}

func (c *Client) Connect(ctx context.Context) error {
	if err := waitToken(ctx, c.client.Connect(), 30*time.Second); err != nil {
		return fmt.Errorf("connect MQTT client: %w", err)
	}
	select {
	case err := <-c.ready:
		if err != nil {
			return fmt.Errorf("initial MQTT subscription: %w", err)
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(c.subscribeTimeout):
		return errors.New("timed out waiting for initial MQTT subscription")
	}
}

func (c *Client) Publish(ctx context.Context, topic string, qos byte, payload []byte) error {
	token := c.client.Publish(topic, qos, false, payload)
	return waitToken(ctx, token, c.publishTimeout)
}

func (c *Client) Unsubscribe(ctx context.Context) error {
	if !c.client.IsConnected() {
		return nil
	}
	return waitToken(ctx, c.client.Unsubscribe(c.inputFilter), c.subscribeTimeout)
}

func (c *Client) Disconnect(quiesce time.Duration) {
	milliseconds := uint(quiesce / time.Millisecond)
	c.client.Disconnect(milliseconds)
}
