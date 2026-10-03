package bridge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"laya.local/solacebridge/internal/protocol"
	"laya.local/solacebridge/internal/worker"
)

const producer = "laya-solace-bridge"

var (
	ErrClosed          = errors.New("bridge is closed")
	ErrQueueFull       = errors.New("bridge queue is full")
	ErrPayloadTooLarge = errors.New("event payload exceeds configured maximum")
	ErrUnexpectedTopic = errors.New("message topic does not match configured input filter")
)

type Publisher interface {
	Publish(context.Context, string, byte, []byte) error
}

type Incoming struct {
	Topic         string
	Payload       []byte
	CorrelationID string
	QoS           byte
	HasMQTTQoS    bool
	Retained      bool
	Duplicate     bool
	Redelivered   bool
	RejectCode    string
	RejectMessage string
	Ack           func() error
	Release       func()
}

type Config struct {
	Transport         string
	TopicSyntax       string
	DeliverySemantics string
	InputFilter       string
	OutputTopic       string
	QoS               byte
	QueueCapacity     int
	MaxEventBytes     int
	EnqueueTimeout    time.Duration
	InferenceTimeout  time.Duration
	PublishTimeout    time.Duration
	MaxMessages       uint64
}

type queued struct {
	message     Incoming
	payloadHash string
	correlation string
	rejection   *protocol.BridgeError
}

type Stats struct {
	Submitted           uint64 `json:"submitted"`
	Published           uint64 `json:"published"`
	SuccessfulDecisions uint64 `json:"successful_decisions"`
	ErrorEnvelopes      uint64 `json:"error_envelopes"`
	Acknowledged        uint64 `json:"acknowledged"`
	Failed              uint64 `json:"failed"`
	Looped              uint64 `json:"looped"`
	CorrelationsTracked bool   `json:"correlations_tracked"`
	UniqueCorrelations  int    `json:"unique_correlations,omitempty"`
}

type Bridge struct {
	config        Config
	inferencer    worker.Inferencer
	publisher     Publisher
	logger        *slog.Logger
	queue         chan queued
	errors        chan error
	closed        atomic.Bool
	submitted     atomic.Uint64
	published          atomic.Uint64
	successfulDecisions atomic.Uint64
	errorEnvelopes     atomic.Uint64
	acknowledged       atomic.Uint64
	failed        atomic.Uint64
	looped        atomic.Uint64
	correlationMu sync.RWMutex
	correlations  map[string]struct{}
	submitMu      sync.RWMutex
	closeOnce     sync.Once
	limitOnce     sync.Once
	done          chan struct{}
	limitReached  chan struct{}
}

func New(config Config, inferencer worker.Inferencer, publisher Publisher, logger *slog.Logger) (*Bridge, error) {
	if inferencer == nil || publisher == nil {
		return nil, errors.New("inferencer and publisher are required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	if config.QueueCapacity < 1 || config.MaxEventBytes < 1 {
		return nil, errors.New("queue capacity and maximum event size must be positive")
	}
	if config.EnqueueTimeout <= 0 || config.InferenceTimeout <= 0 || config.PublishTimeout <= 0 {
		return nil, errors.New("bridge timeouts must be positive")
	}
	if config.Transport == "" {
		config.Transport = "mqtt"
	}
	if config.TopicSyntax == "" {
		config.TopicSyntax = config.Transport
	}
	if config.DeliverySemantics == "" {
		config.DeliverySemantics = config.Transport
	}
	if err := ValidateTopicFilterFor(config.TopicSyntax, config.InputFilter); err != nil {
		return nil, fmt.Errorf("input filter: %w", err)
	}
	renderedSample, err := RenderTopicFor(config.TopicSyntax, config.OutputTopic, "sample-route", "sample-id", "sample/input")
	if err != nil {
		return nil, fmt.Errorf("output topic: %w", err)
	}
	if !strings.Contains(config.OutputTopic, "{") && TopicMatchesFor(config.TopicSyntax, config.InputFilter, renderedSample) {
		return nil, fmt.Errorf("output topic %q matches input filter %q", renderedSample, config.InputFilter)
	}
	var correlations map[string]struct{}
	if config.MaxMessages > 0 {
		correlations = make(map[string]struct{}, min(int(config.MaxMessages), config.QueueCapacity*2))
	}
	return &Bridge{
		config:       config,
		inferencer:   inferencer,
		publisher:    publisher,
		logger:       logger,
		queue:        make(chan queued, config.QueueCapacity),
		errors:       make(chan error, config.QueueCapacity),
		done:         make(chan struct{}),
		limitReached: make(chan struct{}),
		correlations: correlations,
	}, nil
}

func (b *Bridge) Errors() <-chan error          { return b.errors }
func (b *Bridge) Done() <-chan struct{}         { return b.done }
func (b *Bridge) LimitReached() <-chan struct{} { return b.limitReached }
func (b *Bridge) Stats() Stats {
	tracked := b.correlations != nil
	unique := 0
	if tracked {
		b.correlationMu.RLock()
		unique = len(b.correlations)
		b.correlationMu.RUnlock()
	}
	return Stats{
		Submitted: b.submitted.Load(), Published: b.published.Load(),
		SuccessfulDecisions: b.successfulDecisions.Load(), ErrorEnvelopes: b.errorEnvelopes.Load(),
		Acknowledged: b.acknowledged.Load(), Failed: b.failed.Load(), Looped: b.looped.Load(),
		CorrelationsTracked: tracked, UniqueCorrelations: unique,
	}
}

func (b *Bridge) markPublished(correlation string) {
	b.published.Add(1)
	if b.correlations != nil {
		b.correlationMu.Lock()
		b.correlations[correlation] = struct{}{}
		b.correlationMu.Unlock()
	}
}

func (b *Bridge) markAcknowledged() {
	count := b.acknowledged.Add(1)
	if count%1000 == 0 {
		b.logger.Info(
			"bridge progress",
			"acknowledged", count,
			"published", b.published.Load(),
			"unique_correlations", b.Stats().UniqueCorrelations,
		)
	}
	if b.config.MaxMessages > 0 && count >= b.config.MaxMessages {
		b.limitOnce.Do(func() { close(b.limitReached) })
	}
}

func (b *Bridge) Submit(message Incoming) error {
	if message.Ack == nil {
		message.Ack = func() error { return nil }
	}
	if message.Release == nil {
		message.Release = func() {}
	}
	if b.closed.Load() {
		message.Release()
		return ErrClosed
	}
	b.submitMu.RLock()
	defer b.submitMu.RUnlock()
	if b.closed.Load() {
		message.Release()
		return ErrClosed
	}
	if !TopicMatchesFor(b.config.TopicSyntax, b.config.InputFilter, message.Topic) {
		message.Release()
		return fmt.Errorf("%w: %q", ErrUnexpectedTopic, message.Topic)
	}
	payloadHash := sha256.Sum256(message.Payload)
	correlation := strings.TrimSpace(message.CorrelationID)
	if correlation == "" {
		correlation = CorrelationID(message.Payload, payloadHash)
	} else {
		correlation = truncate(correlation, 128)
	}
	item := queued{
		message:     message,
		payloadHash: hex.EncodeToString(payloadHash[:]),
		correlation: correlation,
	}
	if message.RejectCode != "" {
		item.rejection = &protocol.BridgeError{
			Code: message.RejectCode, Message: message.RejectMessage, Retryable: false,
		}
		item.message.Payload = nil
	} else if len(message.Payload) > b.config.MaxEventBytes {
		item.rejection = &protocol.BridgeError{
			Code:      "payload_too_large",
			Message:   fmt.Sprintf("%v: got %d bytes, maximum %d", ErrPayloadTooLarge, len(message.Payload), b.config.MaxEventBytes),
			Retryable: false,
		}
		item.message.Payload = nil
	} else {
		item.message.Payload = append([]byte(nil), message.Payload...)
	}

	timer := time.NewTimer(b.config.EnqueueTimeout)
	defer timer.Stop()
	select {
	case b.queue <- item:
		b.submitted.Add(1)
		return nil
	case <-timer.C:
		message.Release()
		return ErrQueueFull
	}
}

func (b *Bridge) Close() {
	b.closeOnce.Do(func() {
		b.submitMu.Lock()
		defer b.submitMu.Unlock()
		b.closed.Store(true)
		close(b.queue)
	})
}

func (b *Bridge) Run(ctx context.Context) {
	defer close(b.done)
	defer close(b.errors)
	for item := range b.queue {
		if err := ctx.Err(); err != nil {
			item.message.Release()
			b.reportError(fmt.Errorf("event %s left unacknowledged during forced shutdown: %w", item.correlation, err))
			continue
		}
		if err := b.process(ctx, item); err != nil {
			b.reportError(err)
		}
	}
}

func (b *Bridge) reportError(err error) {
	b.failed.Add(1)
	select {
	case b.errors <- err:
	default:
		b.logger.Error("dropping bridge error because error channel is full", "error", err)
	}
}

func (b *Bridge) process(ctx context.Context, item queued) error {
	defer item.message.Release()
	started := time.Now()
	if item.rejection == nil && IsBridgeOutput(item.message.Payload) {
		if err := item.message.Ack(); err != nil {
			return fmt.Errorf("acknowledge looped message: %w", err)
		}
		b.looped.Add(1)
		b.logger.Warn("acknowledged self-produced message to prevent a routing loop", "topic", item.message.Topic)
		return nil
	}

	var decision *protocol.Decision
	var bridgeError *protocol.BridgeError
	if item.rejection != nil {
		bridgeError = item.rejection
	} else if !validJSONObject(item.message.Payload) {
		bridgeError = &protocol.BridgeError{
			Code: "invalid_event_json", Message: "payload must be one JSON object", Retryable: false,
		}
	} else {
		inferenceContext, cancel := context.WithTimeout(ctx, b.config.InferenceTimeout)
		var err error
		decision, err = b.inferencer.Infer(inferenceContext, item.correlation, item.message.Payload)
		cancel()
		if err != nil {
			bridgeError = &protocol.BridgeError{
				Code: "inference_failed", Message: err.Error(), Retryable: true,
			}
		}
	}

	route := "review"
	if decision != nil && decision.SelectedRoute != "" && !decision.ReviewRequired {
		route = decision.SelectedRoute
	}
	outputTopic, err := RenderTopicFor(b.config.TopicSyntax, b.config.OutputTopic, route, item.correlation, item.message.Topic)
	if err != nil {
		return fmt.Errorf("render output topic for %s: %w", item.correlation, err)
	}
	if TopicMatchesFor(b.config.TopicSyntax, b.config.InputFilter, outputTopic) {
		return fmt.Errorf(
			"loop prevention refused output topic %q because it matches input filter %q",
			outputTopic,
			b.config.InputFilter,
		)
	}

	var sourceQoS *byte
	var retained *bool
	if item.message.HasMQTTQoS {
		qos := item.message.QoS
		isRetained := item.message.Retained
		sourceQoS = &qos
		retained = &isRetained
	}
	envelope := protocol.OutputEnvelope{
		SchemaVersion: "1.0",
		Producer:      producer,
		CorrelationID: item.correlation,
		ProcessedAt:   time.Now().UTC(),
		Source: protocol.Source{
			Transport:         b.config.Transport,
			DeliverySemantics: b.config.DeliverySemantics,
			Topic:             item.message.Topic,
			QoS:               sourceQoS,
			Retained:          retained,
			Duplicate:         item.message.Duplicate,
			Redelivered:       item.message.Redelivered,
			PayloadSHA256:     item.payloadHash,
		},
		Decision:        decision,
		Error:           bridgeError,
		BridgeLatencyMS: float64(time.Since(started).Microseconds()) / 1000,
	}
	payload, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode result envelope: %w", err)
	}
	publishContext, cancel := context.WithTimeout(ctx, b.config.PublishTimeout)
	err = b.publisher.Publish(publishContext, outputTopic, b.config.QoS, payload)
	cancel()
	if err != nil {
		return fmt.Errorf("publish result for %s: %w", item.correlation, err)
	}
	b.markPublished(item.correlation)
	if decision != nil {
		b.successfulDecisions.Add(1)
	} else {
		b.errorEnvelopes.Add(1)
	}
	// Acknowledge input only after the corresponding output publication completes.
	// A crash between those operations can still duplicate output; correlation_id enables dedupe.
	if err := item.message.Ack(); err != nil {
		return fmt.Errorf("acknowledge input after output publish: %w", err)
	}
	b.markAcknowledged()
	return nil
}

func validJSONObject(payload []byte) bool {
	var value map[string]any
	return json.Unmarshal(payload, &value) == nil && value != nil
}

func IsBridgeOutput(payload []byte) bool {
	var marker struct {
		Producer string `json:"producer"`
	}
	return json.Unmarshal(payload, &marker) == nil && marker.Producer == producer
}

func CorrelationID(payload []byte, digest [sha256.Size]byte) string {
	var event map[string]any
	if json.Unmarshal(payload, &event) == nil {
		for _, key := range []string{"correlation_id", "event_id", "id"} {
			if value, ok := event[key].(string); ok && strings.TrimSpace(value) != "" {
				return truncate(value, 128)
			}
		}
		if nested, ok := event["payload"].(map[string]any); ok {
			for _, key := range []string{"correlation_id", "event_id", "id"} {
				if value, ok := nested[key].(string); ok && strings.TrimSpace(value) != "" {
					return truncate(value, 128)
				}
			}
		}
	}
	return hex.EncodeToString(digest[:12])
}

func truncate(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	return value[:maximum]
}

func topicValue(value string, allowSlash bool) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || strings.ContainsRune(value, 0) || strings.ContainsAny(value, "+#*>") {
		return "", fmt.Errorf("unsafe empty or wildcard topic value %q", value)
	}
	if !allowSlash {
		value = strings.ReplaceAll(value, "/", "_")
	}
	return value, nil
}

func RenderTopic(template, route, correlationID, inputTopic string) (string, error) {
	return RenderTopicFor("mqtt", template, route, correlationID, inputTopic)
}

func RenderTopicFor(syntax, template, route, correlationID, inputTopic string) (string, error) {
	route, err := topicValue(route, false)
	if err != nil {
		return "", err
	}
	correlationID, err = topicValue(correlationID, false)
	if err != nil {
		return "", err
	}
	inputTopic, err = topicValue(inputTopic, true)
	if err != nil {
		return "", err
	}
	result := strings.NewReplacer(
		"{route}", route,
		"{correlation_id}", correlationID,
		"{input_topic}", inputTopic,
	).Replace(template)
	if strings.ContainsAny(result, "+#*>\x00") || strings.ContainsAny(result, "{}") {
		return "", fmt.Errorf("rendered publish topic contains a wildcard or unknown placeholder: %q", result)
	}
	if strings.TrimSpace(result) == "" {
		return "", errors.New("rendered publish topic is empty")
	}
	if syntax == "smf" {
		if len([]byte(result)) > 250 {
			return "", fmt.Errorf("SMF publish topic exceeds 250 bytes: %d", len([]byte(result)))
		}
		if levels := len(strings.Split(result, "/")); levels > 128 {
			return "", fmt.Errorf("SMF publish topic exceeds 128 levels: %d", levels)
		}
	} else if len([]byte(result)) > 65535 {
		return "", fmt.Errorf("MQTT publish topic exceeds 65535 bytes: %d", len([]byte(result)))
	}
	return result, nil
}

func ValidateTopicFilter(filter string) error {
	return ValidateTopicFilterFor("mqtt", filter)
}

func ValidateTopicFilterFor(syntax, filter string) error {
	if filter == "" || strings.ContainsRune(filter, 0) {
		return errors.New("topic filter is empty or contains NUL")
	}
	levels := strings.Split(filter, "/")
	if syntax == "smf" {
		if len([]byte(filter)) > 250 {
			return fmt.Errorf("SMF topic filter exceeds 250 bytes: %d", len([]byte(filter)))
		}
		if len(levels) > 128 {
			return fmt.Errorf("SMF topic filter exceeds 128 levels: %d", len(levels))
		}
	}
	switch syntax {
	case "mqtt", "offline":
		for index, level := range levels {
			if strings.ContainsAny(level, "*>") {
				return errors.New("MQTT filters use + and #, not * or >")
			}
			if strings.Contains(level, "#") && (level != "#" || index != len(levels)-1) {
				return errors.New("# must occupy the final MQTT filter level")
			}
			if strings.Contains(level, "+") && level != "+" {
				return errors.New("+ must occupy an entire MQTT filter level")
			}
		}
	case "smf":
		for index, level := range levels {
			if strings.ContainsAny(level, "+#") {
				return errors.New("SMF filters use * and >, not + or #")
			}
			if strings.Contains(level, ">") && (level != ">" || index != len(levels)-1) {
				return errors.New("> must occupy the final SMF filter level")
			}
			if strings.Contains(level, "*") && (strings.Count(level, "*") != 1 || !strings.HasSuffix(level, "*")) {
				return errors.New("SMF * must be the final character of one topic level")
			}
		}
	default:
		return fmt.Errorf("unsupported topic syntax %q", syntax)
	}
	return nil
}

func TopicMatches(filter, topic string) bool {
	return TopicMatchesFor("mqtt", filter, topic)
}

func TopicMatchesFor(syntax, filter, topic string) bool {
	filterLevels := strings.Split(filter, "/")
	topicLevels := strings.Split(topic, "/")
	single, multi := "+", "#"
	if syntax == "smf" {
		single, multi = "*", ">"
	}
	for index, level := range filterLevels {
		if level == multi {
			if index != len(filterLevels)-1 {
				return false
			}
			if syntax == "smf" {
				return index < len(topicLevels)
			}
			return true
		}
		if index >= len(topicLevels) {
			return false
		}
		if syntax == "smf" && strings.HasSuffix(level, "*") {
			if !strings.HasPrefix(topicLevels[index], strings.TrimSuffix(level, "*")) {
				return false
			}
		} else if level != single && level != topicLevels[index] {
			return false
		}
	}
	return len(filterLevels) == len(topicLevels)
}
