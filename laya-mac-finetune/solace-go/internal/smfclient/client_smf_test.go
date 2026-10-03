//go:build smf

package smfclient

import (
	"bytes"
	"testing"
)

type fakePayload struct {
	bytesValue  []byte
	bytesOK     bool
	stringValue string
	stringOK    bool
}

func (f fakePayload) GetPayloadAsBytes() ([]byte, bool) { return f.bytesValue, f.bytesOK }
func (f fakePayload) GetPayloadAsString() (string, bool) { return f.stringValue, f.stringOK }

func TestExtractPayloadAcceptsUTF8Bytes(t *testing.T) {
	want := []byte(`{"event_type":"payment.failed"}`)
	got, code, _ := extractPayload(fakePayload{bytesValue: want, bytesOK: true})
	if code != "" || !bytes.Equal(got, want) {
		t.Fatalf("got=%q code=%q", got, code)
	}
}

func TestExtractPayloadAcceptsString(t *testing.T) {
	want := `{"message":"paiement refusé"}`
	got, code, _ := extractPayload(fakePayload{stringValue: want, stringOK: true})
	if code != "" || string(got) != want {
		t.Fatalf("got=%q code=%q", got, code)
	}
}

func TestExtractPayloadRejectsUnsupportedAndInvalidUTF8(t *testing.T) {
	if _, code, _ := extractPayload(fakePayload{}); code != "unsupported_payload" {
		t.Fatalf("unsupported payload code=%q", code)
	}
	if _, code, _ := extractPayload(fakePayload{bytesValue: []byte{0xff}, bytesOK: true}); code != "unsupported_payload" {
		t.Fatalf("invalid UTF-8 code=%q", code)
	}
}
