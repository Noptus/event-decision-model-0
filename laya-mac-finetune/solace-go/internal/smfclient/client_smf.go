//go:build smf

package smfclient

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"
	"unicode/utf8"

	"solace.dev/go/messaging"
	"solace.dev/go/messaging/pkg/solace"
	solaceconfig "solace.dev/go/messaging/pkg/solace/config"
	"solace.dev/go/messaging/pkg/solace/message"
	"solace.dev/go/messaging/pkg/solace/resource"

	"laya.local/solacebridge/internal/bridge"
	appconfig "laya.local/solacebridge/internal/config"
)

type Client struct {
	config    appconfig.Config
	service   solace.MessagingService
	receiver  solace.PersistentMessageReceiver
	publisher solace.PersistentMessagePublisher
	submit    func(bridge.Incoming) error
	logger    *slog.Logger
}

func Available() bool { return true }

func trustStore(explicit string) (string, error) {
	if explicit != "" {
		if info, err := os.Stat(explicit); err != nil || !info.IsDir() {
			return "", fmt.Errorf("SMF trust store directory is unavailable: %s", explicit)
		}
		return explicit, nil
	}
	for _, candidate := range []string{"/opt/homebrew/etc/openssl@3/certs", "/opt/homebrew/etc/openssl/certs", "/etc/ssl/certs"} {
		if info, err := os.Stat(candidate); err == nil && info.IsDir() {
			return candidate, nil
		}
	}
	return "", errors.New("no CA directory found; set SOLACE_SMF_TRUST_STORE")
}

func New(cfg appconfig.Config, submit func(bridge.Incoming) error, logger *slog.Logger) (*Client, error) {
	if logger == nil {
		logger = slog.Default()
	}
	properties := solaceconfig.ServicePropertyMap{
		solaceconfig.TransportLayerPropertyHost:                cfg.BrokerURL,
		solaceconfig.ServicePropertyVPNName:                    cfg.SMFVPN,
		solaceconfig.AuthenticationPropertySchemeBasicUserName: cfg.Username,
		solaceconfig.AuthenticationPropertySchemeBasicPassword: cfg.Password,
	}
	builder := messaging.NewMessagingServiceBuilder().
		FromConfigurationProvider(properties).
		WithConnectionRetryStrategy(solaceconfig.RetryStrategyParameterizedRetry(3, 2*time.Second)).
		WithReconnectionRetryStrategy(solaceconfig.RetryStrategyForeverRetryWithInterval(3 * time.Second))
	if strings.HasPrefix(cfg.BrokerURL, "tcps://") {
		trustDirectory, err := trustStore(cfg.SMFTrustStore)
		if err != nil {
			return nil, err
		}
		security := solaceconfig.NewTransportSecurityStrategy().
			WithMinimumProtocol(solaceconfig.TransportSecurityProtocolTLSv1_2).
			WithCertificateValidation(false, true, trustDirectory, "")
		builder = builder.WithTransportSecurityStrategy(security)
	}
	service, err := builder.BuildWithApplicationID(cfg.ClientID)
	if err != nil {
		return nil, fmt.Errorf("build SMF messaging service: %w", err)
	}
	client := &Client{config: cfg, service: service, submit: submit, logger: logger}
	service.AddReconnectionAttemptListener(func(event solace.ServiceEvent) {
		logger.Warn("SMF reconnecting", "cause", event.GetCause())
	})
	service.AddReconnectionListener(func(event solace.ServiceEvent) {
		logger.Info("SMF reconnected", "broker", event.GetBrokerURI())
	})
	service.AddServiceInterruptionListener(func(event solace.ServiceEvent) {
		logger.Error("SMF service interrupted", "cause", event.GetCause())
	})
	return client, nil
}

func waitAsync(ctx context.Context, result <-chan error, operation string) error {
	select {
	case err := <-result:
		if err != nil {
			return fmt.Errorf("%s: %w", operation, err)
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("%s: %w", operation, ctx.Err())
	}
}

func (c *Client) Connect(ctx context.Context) error {
	if err := waitAsync(ctx, c.service.ConnectAsync(), "connect SMF service"); err != nil {
		return err
	}
	if c.config.SMFProvision {
		outcome := c.service.EndpointProvisioner().
			WithDurability(true).
			WithExclusiveAccess(c.config.SMFQueueExclusive).
			Provision(c.config.SMFQueue, true)
		if !outcome.GetStatus() {
			_ = c.service.Disconnect()
			if err := outcome.GetError(); err != nil {
				return fmt.Errorf("provision dedicated SMF queue %q: %w", c.config.SMFQueue, err)
			}
			return fmt.Errorf("provision dedicated SMF queue %q failed", c.config.SMFQueue)
		}
		c.logger.Info("SMF durable queue is available", "queue", c.config.SMFQueue)
	}

	publisher, err := c.service.CreatePersistentMessagePublisherBuilder().
		OnBackPressureReject(uint(c.config.QueueCapacity)).
		Build()
	if err != nil {
		_ = c.service.Disconnect()
		return fmt.Errorf("build SMF persistent publisher: %w", err)
	}
	if err := waitAsync(ctx, publisher.StartAsync(), "start SMF persistent publisher"); err != nil {
		_ = c.service.Disconnect()
		return err
	}
	c.publisher = publisher

	receiverBuilder := c.service.CreatePersistentMessageReceiverBuilder().
		WithMessageClientAcknowledgement().
		WithMissingResourcesCreationStrategy(solaceconfig.PersistentReceiverDoNotCreateMissingResources)
	if c.config.SMFAddSubscription {
		receiverBuilder = receiverBuilder.WithSubscriptions(resource.TopicSubscriptionOf(c.config.InputFilter))
	}
	var queue *resource.Queue
	if c.config.SMFQueueExclusive {
		queue = resource.QueueDurableExclusive(c.config.SMFQueue)
	} else {
		queue = resource.QueueDurableNonExclusive(c.config.SMFQueue)
	}
	receiver, err := receiverBuilder.Build(queue)
	if err != nil {
		_ = publisher.Terminate(0)
		_ = c.service.Disconnect()
		return fmt.Errorf("bind preprovisioned SMF queue %q: %w", c.config.SMFQueue, err)
	}
	if err := waitAsync(ctx, receiver.StartAsync(), "start SMF persistent receiver"); err != nil {
		_ = publisher.Terminate(0)
		_ = c.service.Disconnect()
		return err
	}
	c.receiver = receiver
	if err := receiver.ReceiveAsync(c.handleMessage); err != nil {
		_ = receiver.Terminate(0)
		_ = publisher.Terminate(0)
		_ = c.service.Disconnect()
		return fmt.Errorf("register SMF message handler: %w", err)
	}
	c.logger.Info(
		"SMF persistent receiver active",
		"queue", c.config.SMFQueue,
		"input_filter", c.config.InputFilter,
		"subscription_added", c.config.SMFAddSubscription,
	)
	return nil
}

type payloadAccessor interface {
	GetPayloadAsBytes() ([]byte, bool)
	GetPayloadAsString() (string, bool)
}

func (c *Client) handleMessage(inbound message.InboundMessage) {
	payload, rejectionCode, rejectionMessage := extractPayload(inbound)
	correlationID := ""
	if value, ok := inbound.GetCorrelationID(); ok {
		correlationID = value
	} else if value, ok := inbound.GetApplicationMessageID(); ok {
		correlationID = value
	}
	incoming := bridge.Incoming{
		Topic:         inbound.GetDestinationName(),
		Payload:       payload,
		CorrelationID: correlationID,
		Redelivered:   inbound.IsRedelivered(),
		RejectCode:    rejectionCode,
		RejectMessage: rejectionMessage,
		Ack: func() error {
			return c.receiver.Ack(inbound)
		},
		Release: inbound.Dispose,
	}
	if err := c.submit(incoming); err != nil {
		// Submit owns and disposes the message on every rejection path. It deliberately does
		// not acknowledge, leaving the durable queue eligible to redeliver the event.
		c.logger.Error("SMF event was not accepted or acknowledged", "topic", incoming.Topic, "error", err)
	}
}

func extractPayload(inbound payloadAccessor) ([]byte, string, string) {
	if payload, ok := inbound.GetPayloadAsBytes(); ok {
		if !utf8.Valid(payload) {
			return payload, "unsupported_payload", "SMF byte-array payload is not valid UTF-8 JSON"
		}
		return payload, "", ""
	}
	if payload, ok := inbound.GetPayloadAsString(); ok {
		if !utf8.ValidString(payload) {
			return []byte(payload), "unsupported_payload", "SMF string payload is not valid UTF-8"
		}
		return []byte(payload), "", ""
	}
	return nil, "unsupported_payload", "SMF payload must be a UTF-8 string or byte array containing JSON"
}

func durationWithinContext(ctx context.Context, fallback time.Duration) (time.Duration, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	if deadline, ok := ctx.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return 0, context.DeadlineExceeded
		}
		if remaining < fallback {
			return remaining, nil
		}
	}
	return fallback, nil
}

func (c *Client) Publish(ctx context.Context, topic string, _ byte, payload []byte) error {
	if c.publisher == nil {
		return errors.New("SMF publisher is not ready")
	}
	timeout, err := durationWithinContext(ctx, c.config.PublishTimeout)
	if err != nil {
		return err
	}
	builder := c.service.MessageBuilder().WithHTTPContentHeader("application/json", "utf-8")
	var envelope struct {
		CorrelationID string `json:"correlation_id"`
	}
	if json.Unmarshal(payload, &envelope) == nil && envelope.CorrelationID != "" {
		builder = builder.WithCorrelationID(envelope.CorrelationID)
	}
	outbound, err := builder.BuildWithByteArrayPayload(payload)
	if err != nil {
		return fmt.Errorf("build SMF output message: %w", err)
	}
	defer outbound.Dispose()
	if err := c.publisher.PublishAwaitAcknowledgement(
		outbound,
		resource.TopicOf(topic),
		timeout,
		nil,
	); err != nil {
		return fmt.Errorf("publish persistent SMF result: %w", err)
	}
	return nil
}

func (c *Client) StopIntake() error {
	if c.receiver == nil || !c.receiver.IsRunning() {
		return nil
	}
	if err := c.receiver.Pause(); err != nil {
		return fmt.Errorf("pause SMF receiver: %w", err)
	}
	return nil
}

func (c *Client) Disconnect(grace time.Duration) {
	if c.receiver != nil {
		if err := c.receiver.Terminate(grace); err != nil {
			c.logger.Warn("terminate SMF receiver", "error", err)
		}
	}
	if c.publisher != nil {
		if err := c.publisher.Terminate(grace); err != nil {
			c.logger.Warn("terminate SMF publisher", "error", err)
		}
	}
	if c.service != nil && c.service.IsConnected() {
		if err := c.service.Disconnect(); err != nil {
			c.logger.Warn("disconnect SMF service", "error", err)
		}
	}
}
