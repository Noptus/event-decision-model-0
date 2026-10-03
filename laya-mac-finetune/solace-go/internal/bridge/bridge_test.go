package bridge

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"laya.local/solacebridge/internal/protocol"
)

type fakeInferencer struct {
	decision *protocol.Decision
	err      error
	calls    atomic.Int32
	wait     bool
}

func (f *fakeInferencer) Infer(ctx context.Context, _ string, _ json.RawMessage) (*protocol.Decision, error) {
	f.calls.Add(1)
	if f.wait {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return f.decision, f.err
}

type publication struct {
	topic   string
	qos     byte
	payload []byte
}

type publisherFunc func(context.Context, string, byte, []byte) error

func (function publisherFunc) Publish(ctx context.Context, topic string, qos byte, payload []byte) error {
	return function(ctx, topic, qos, payload)
}

type fakePublisher struct {
	mu           sync.Mutex
	publications []publication
	err          error
}

func (p *fakePublisher) Publish(_ context.Context, topic string, qos byte, payload []byte) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.err != nil {
		return p.err
	}
	p.publications = append(p.publications, publication{topic: topic, qos: qos, payload: append([]byte(nil), payload...)})
	return nil
}

func testConfig() Config {
	return Config{
		InputFilter:      "business/events/#",
		OutputTopic:      "ai/routes/{route}/{correlation_id}",
		QoS:              1,
		QueueCapacity:    1,
		MaxEventBytes:    1024,
		EnqueueTimeout:   10 * time.Millisecond,
		InferenceTimeout: 20 * time.Millisecond,
		PublishTimeout:   20 * time.Millisecond,
	}
}

func runOne(t *testing.T, pipeline *Bridge, message Incoming) []error {
	t.Helper()
	go pipeline.Run(context.Background())
	if err := pipeline.Submit(message); err != nil {
		t.Fatalf("submit: %v", err)
	}
	pipeline.Close()
	<-pipeline.Done()
	var found []error
	for err := range pipeline.Errors() {
		found = append(found, err)
	}
	return found
}

func TestPublishesThenAcknowledges(t *testing.T) {
	inferencer := &fakeInferencer{decision: &protocol.Decision{
		SelectedRoute: "logistics",
		Probabilities: map[string]float64{"logistics": 0.8, "customer-support": 0.2},
		Confidence:    0.8,
	}}
	publisher := &fakePublisher{}
	pipeline, err := New(testConfig(), inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic:   "business/events/eu",
		Payload: []byte(`{"event_id":"evt-1","payload":{"message":"late parcel"}}`),
		QoS:     1,
		Ack:     func() error { acked.Store(true); return nil },
	})
	if len(errorsFound) != 0 {
		t.Fatalf("unexpected errors: %v", errorsFound)
	}
	if !acked.Load() {
		t.Fatal("input was not acknowledged after successful publish")
	}
	if len(publisher.publications) != 1 {
		t.Fatalf("got %d publications", len(publisher.publications))
	}
	got := publisher.publications[0]
	if got.topic != "ai/routes/logistics/evt-1" || got.qos != 1 {
		t.Fatalf("unexpected publication: %#v", got)
	}
	var envelope protocol.OutputEnvelope
	if err := json.Unmarshal(got.payload, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Producer != producer || envelope.CorrelationID != "evt-1" || envelope.Decision.SelectedRoute != "logistics" {
		t.Fatalf("unexpected envelope: %#v", envelope)
	}
}

func TestNativeOwnershipPublishesThenAcknowledgesThenReleases(t *testing.T) {
	var mu sync.Mutex
	var events []string
	record := func(value string) {
		mu.Lock()
		events = append(events, value)
		mu.Unlock()
	}
	inferencer := &fakeInferencer{decision: &protocol.Decision{
		SelectedRoute: "shipping-logistics",
		Probabilities: map[string]float64{"shipping-logistics": 1},
		Confidence:    1,
	}}
	var published []byte
	publisher := publisherFunc(func(_ context.Context, _ string, _ byte, payload []byte) error {
		published = append([]byte(nil), payload...)
		record("publish")
		return nil
	})
	cfg := testConfig()
	cfg.Transport = "smf"
	cfg.TopicSyntax = "smf"
	cfg.DeliverySemantics = "smf-persistent-guaranteed"
	cfg.InputFilter = "business/events/>"
	pipeline, err := New(cfg, inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	errorsFound := runOne(t, pipeline, Incoming{
		Topic:       "business/events/shipping/delayed",
		Payload:     []byte(`{"event_id":"native-1"}`),
		Redelivered: true,
		Ack:         func() error { record("ack"); return nil },
		Release:     func() { record("dispose") },
	})
	if len(errorsFound) != 0 {
		t.Fatalf("unexpected errors: %v", errorsFound)
	}
	want := []string{"publish", "ack", "dispose"}
	if len(events) != len(want) {
		t.Fatalf("events=%v", events)
	}
	for index := range want {
		if events[index] != want[index] {
			t.Fatalf("events=%v want=%v", events, want)
		}
	}
	var envelope protocol.OutputEnvelope
	if err := json.Unmarshal(published, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Source.Transport != "smf" || envelope.Source.DeliverySemantics != "smf-persistent-guaranteed" || !envelope.Source.Redelivered || envelope.Source.QoS != nil {
		t.Fatalf("unexpected native source metadata: %#v", envelope.Source)
	}
}

func TestReviewDecisionPublishesToReviewTopicWithoutChangingChoice(t *testing.T) {
	inferencer := &fakeInferencer{decision: &protocol.Decision{
		SelectedRoute:  "fraud-review",
		Probabilities:  map[string]float64{"fraud-review": 0.4, "payment-operations": 0.6},
		Confidence:     0.6,
		ReviewRequired: true,
		ReviewStatus:   "abstained",
	}}
	publisher := &fakePublisher{}
	pipeline, err := New(testConfig(), inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic: "business/events/eu", Payload: []byte(`{"id":"needs-review"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if len(errorsFound) != 0 || !acked.Load() {
		t.Fatalf("review envelope failed: errors=%v acked=%v", errorsFound, acked.Load())
	}
	if got := publisher.publications[0].topic; got != "ai/routes/review/needs-review" {
		t.Fatalf("review-required decision published to %q", got)
	}
	var envelope protocol.OutputEnvelope
	if err := json.Unmarshal(publisher.publications[0].payload, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Decision.SelectedRoute != "fraud-review" || !envelope.Decision.ReviewRequired {
		t.Fatalf("native decision was changed: %#v", envelope.Decision)
	}
}

func TestPublishFailureLeavesInputUnacknowledged(t *testing.T) {
	inferencer := &fakeInferencer{decision: &protocol.Decision{SelectedRoute: "logistics"}}
	publisher := &fakePublisher{err: errors.New("broker unavailable")}
	pipeline, _ := New(testConfig(), inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic: "business/events/eu", Payload: []byte(`{"id":"evt-2"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if acked.Load() {
		t.Fatal("input was acknowledged after output publish failure")
	}
	if len(errorsFound) != 1 {
		t.Fatalf("expected one processing error, got %v", errorsFound)
	}
}

func TestInferenceTimeoutPublishesErrorAndAcknowledges(t *testing.T) {
	inferencer := &fakeInferencer{wait: true}
	publisher := &fakePublisher{}
	pipeline, _ := New(testConfig(), inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic: "business/events/eu", Payload: []byte(`{"id":"evt-timeout"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if len(errorsFound) != 0 || !acked.Load() {
		t.Fatalf("error envelope should publish and then ack: errors=%v acked=%v", errorsFound, acked.Load())
	}
	var envelope protocol.OutputEnvelope
	if err := json.Unmarshal(publisher.publications[0].payload, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Error == nil || envelope.Error.Code != "inference_failed" || !envelope.Error.Retryable {
		t.Fatalf("unexpected error envelope: %#v", envelope.Error)
	}
}

func TestLoopPreventionDoesNotPublishOrAck(t *testing.T) {
	cfg := testConfig()
	cfg.InputFilter = "ai/routes/#"
	inferencer := &fakeInferencer{decision: &protocol.Decision{SelectedRoute: "logistics"}}
	publisher := &fakePublisher{}
	pipeline, _ := New(cfg, inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic: "ai/routes/input", Payload: []byte(`{"id":"evt-loop"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if len(errorsFound) != 1 || acked.Load() || len(publisher.publications) != 0 {
		t.Fatalf("loop guard failed: errors=%v acked=%v published=%d", errorsFound, acked.Load(), len(publisher.publications))
	}
}

func TestSelfProducedInputIsAcknowledgedWithoutInference(t *testing.T) {
	inferencer := &fakeInferencer{}
	publisher := &fakePublisher{}
	pipeline, _ := New(testConfig(), inferencer, publisher, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var acked atomic.Bool
	errorsFound := runOne(t, pipeline, Incoming{
		Topic: "business/events/loop", Payload: []byte(`{"producer":"laya-solace-bridge"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if len(errorsFound) != 0 || !acked.Load() || inferencer.calls.Load() != 0 || len(publisher.publications) != 0 {
		t.Fatal("self-produced event was not safely discarded")
	}
}

func TestUnexpectedTopicIsRejectedWithoutAcknowledgement(t *testing.T) {
	pipeline, _ := New(testConfig(), &fakeInferencer{}, &fakePublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var acked atomic.Bool
	err := pipeline.Submit(Incoming{
		Topic: "stale/subscription", Payload: []byte(`{"id":"stale"}`), Ack: func() error { acked.Store(true); return nil },
	})
	if !errors.Is(err, ErrUnexpectedTopic) || acked.Load() {
		t.Fatalf("unexpected-topic handling failed: err=%v acked=%v", err, acked.Load())
	}
	pipeline.Close()
}

func TestQueueCapacityIsBounded(t *testing.T) {
	pipeline, _ := New(testConfig(), &fakeInferencer{}, &fakePublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	message := Incoming{Topic: "business/events/eu", Payload: []byte(`{"id":"evt"}`)}
	if err := pipeline.Submit(message); err != nil {
		t.Fatal(err)
	}
	if err := pipeline.Submit(message); !errors.Is(err, ErrQueueFull) {
		t.Fatalf("expected ErrQueueFull, got %v", err)
	}
	pipeline.Close()
}

func TestConcurrentSubmitAndCloseNeverPanics(t *testing.T) {
	for iteration := 0; iteration < 100; iteration++ {
		cfg := testConfig()
		cfg.EnqueueTimeout = time.Millisecond
		pipeline, err := New(cfg, &fakeInferencer{}, &fakePublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
		if err != nil {
			t.Fatal(err)
		}
		start := make(chan struct{})
		var wait sync.WaitGroup
		for index := 0; index < 8; index++ {
			wait.Add(1)
			go func() {
				defer wait.Done()
				<-start
				err := pipeline.Submit(Incoming{Topic: "business/events/eu", Payload: []byte(`{"id":"race"}`)})
				if err != nil && !errors.Is(err, ErrClosed) && !errors.Is(err, ErrQueueFull) {
					t.Errorf("unexpected submit error: %v", err)
				}
			}()
		}
		close(start)
		pipeline.Close()
		wait.Wait()
	}
}

func TestCorrelationTrackingOnlyForFiniteAudit(t *testing.T) {
	unbounded, err := New(testConfig(), &fakeInferencer{}, &fakePublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	if stats := unbounded.Stats(); stats.CorrelationsTracked || stats.UniqueCorrelations != 0 {
		t.Fatalf("unbounded service retained correlations: %#v", stats)
	}
	unbounded.Close()

	cfg := testConfig()
	cfg.MaxMessages = 1
	finite, err := New(cfg, &fakeInferencer{decision: &protocol.Decision{
		SelectedRoute: "logistics", Probabilities: map[string]float64{"logistics": 1}, Confidence: 1,
	}}, &fakePublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	runOne(t, finite, Incoming{Topic: "business/events/eu", Payload: []byte(`{"id":"finite-audit"}`)})
	stats := finite.Stats()
	if !stats.CorrelationsTracked || stats.UniqueCorrelations != 1 {
		t.Fatalf("finite audit did not track one correlation: %#v", stats)
	}
}

func TestTopicMatchingAndTemplateSafety(t *testing.T) {
	cases := []struct {
		filter string
		topic  string
		match  bool
	}{
		{"a/+/c", "a/b/c", true},
		{"a/#", "a/b/c", true},
		{"a/+/c", "a/b/d", false},
		{"a/b", "a/b/c", false},
	}
	for _, test := range cases {
		if got := TopicMatches(test.filter, test.topic); got != test.match {
			t.Errorf("TopicMatches(%q, %q)=%v", test.filter, test.topic, got)
		}
	}
	smfCases := []struct {
		filter string
		topic  string
		match  bool
	}{
		{"a/*/c", "a/b/c", true},
		{"a/order*/c", "a/orders/c", true},
		{"a/order*/c", "a/payments/c", false},
		{"a/>", "a/b/c", true},
		{"a/>", "a", false},
		{"a/*/c", "a/b/d", false},
	}
	for _, test := range smfCases {
		if got := TopicMatchesFor("smf", test.filter, test.topic); got != test.match {
			t.Errorf("SMF TopicMatches(%q, %q)=%v", test.filter, test.topic, got)
		}
	}
	if err := ValidateTopicFilterFor("smf", "a/+/c"); err == nil {
		t.Fatal("MQTT wildcard was accepted in an SMF filter")
	}
	if err := ValidateTopicFilterFor("mqtt", "a/*/c"); err == nil {
		t.Fatal("SMF wildcard was accepted in an MQTT filter")
	}
	if err := ValidateTopicFilterFor("smf", "a/or*der/c"); err == nil {
		t.Fatal("unsupported embedded SMF wildcard was accepted")
	}
	if err := ValidateTopicFilterFor("smf", strings.Repeat("x", 251)); err == nil {
		t.Fatal("oversized SMF filter was accepted")
	}
	if err := ValidateTopicFilterFor("smf", strings.Repeat("/", 128)); err == nil {
		t.Fatal("SMF filter with more than 128 levels was accepted")
	}
	if _, err := RenderTopicFor("smf", strings.Repeat("x", 251), "route", "id", "in/topic"); err == nil {
		t.Fatal("oversized SMF publish topic was accepted")
	}
	if _, err := RenderTopic("out/{route}", "bad/#", "id", "in/topic"); err == nil {
		t.Fatal("wildcard route was accepted")
	}
}

func TestCorrelationIDPrefersPayloadEventID(t *testing.T) {
	payload := []byte(`{"payload":{"event_id":"business-42"}}`)
	if got := CorrelationID(payload, sha256.Sum256(payload)); got != "business-42" {
		t.Fatalf("got %q", got)
	}
}
