package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestStatusPreservesListenerDuringUpstreamFailure(t *testing.T) {
	var unavailable atomic.Bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if unavailable.Load() {
			w.WriteHeader(503)
			return
		}
		if r.URL.Path == "/api/v2/admin/sessions/command-capabilities" {
			respond(w, 200, object{"actions": []string{"terminate"}, "terminate_revokes_authority": true})
			return
		}
		respond(w, 200, object{"items": []any{}})
	}))
	defer upstream.Close()
	p := &pluginServer{config: Config{SiloURL: upstream.URL, SiloKey: "upstream", BridgeKey: "tracearr", Listen: "127.0.0.1:0"}}
	t.Cleanup(func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		if p.server != nil {
			p.server.Close()
		}
	})
	data, err := p.status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	success := obj(data["upstream"])["last_successful_session_poll_at"]
	if success == nil {
		t.Fatal("missing successful poll")
	}
	unavailable.Store(true)
	data, err = p.status(context.Background())
	if err == nil || !yes(data["running"]) {
		t.Fatalf("upstream failure lost listener state: %v %v", data, err)
	}
	diagnostics := obj(data["upstream"])
	if diagnostics["last_successful_session_poll_at"] != success || diagnostics["consecutive_session_poll_failures"] != 1 || diagnostics["session_poll_error"] == "" {
		t.Fatalf("missing failure diagnostics: %v", diagnostics)
	}
	unavailable.Store(false)
	data, err = p.status(context.Background())
	if err != nil || !yes(obj(data["upstream"])["session_poll_healthy"]) || obj(data["upstream"])["consecutive_session_poll_failures"] != 0 {
		t.Fatalf("did not recover: %v %v", data, err)
	}
}
