package protocol

import (
	"encoding/json"
	"time"
)

const Version = 1

// WorkerRequest is one line written to the persistent Python process.
type WorkerRequest struct {
	ID    string          `json:"id"`
	Event json.RawMessage `json:"event"`
}

type WorkerError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type ModelIdentity struct {
	Path      string `json:"path"`
	Name      string `json:"name"`
	FineTuned bool   `json:"fine_tuned"`
}

type Usage struct {
	InputTokens        int            `json:"input_tokens"`
	OutputTokens       int            `json:"output_tokens"`
	StateTokens        int            `json:"state_tokens"`
	StateTokensDropped int            `json:"state_tokens_dropped"`
	Truncated          bool           `json:"truncated"`
	TruncatedQuestions []string       `json:"truncated_questions"`
	Options            map[string]any `json:"options,omitempty"`
}

type Decision struct {
	SelectedRoute   string             `json:"selected_route"`
	Probabilities   map[string]float64 `json:"probabilities"`
	Confidence      float64            `json:"confidence"`
	ReviewRequired  bool               `json:"review_required"`
	ReviewStatus    string             `json:"review_status"`
	ReviewThreshold *float64           `json:"review_threshold"`
	Model           ModelIdentity      `json:"model"`
	Device          string             `json:"device"`
	LatencyMS       float64            `json:"latency_ms"`
	Usage           Usage              `json:"usage"`
}

type WorkerResponse struct {
	Type            string         `json:"type,omitempty"`
	ProtocolVersion int            `json:"protocol_version,omitempty"`
	ID              string         `json:"id,omitempty"`
	OK              bool           `json:"ok,omitempty"`
	Decision        *Decision      `json:"decision,omitempty"`
	Error           *WorkerError   `json:"error,omitempty"`
	Model           *ModelIdentity `json:"model,omitempty"`
	Device          string         `json:"device,omitempty"`
}

type Source struct {
	Transport         string `json:"transport"`
	DeliverySemantics string `json:"delivery_semantics"`
	Topic             string `json:"topic"`
	QoS               *byte  `json:"qos,omitempty"`
	Retained          *bool  `json:"retained,omitempty"`
	Duplicate         bool   `json:"duplicate,omitempty"`
	Redelivered       bool   `json:"redelivered"`
	PayloadSHA256     string `json:"payload_sha256"`
}

type BridgeError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

type OutputEnvelope struct {
	SchemaVersion   string       `json:"schema_version"`
	Producer        string       `json:"producer"`
	CorrelationID   string       `json:"correlation_id"`
	ProcessedAt     time.Time    `json:"processed_at"`
	Source          Source       `json:"source"`
	Decision        *Decision    `json:"decision,omitempty"`
	Error           *BridgeError `json:"error,omitempty"`
	BridgeLatencyMS float64      `json:"bridge_latency_ms"`
}
