package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestCatalogSharesScanAcrossCancelledAndConcurrentCallers(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Query().Get("cursor") == "" {
			close(started)
			<-release
			respond(w, 200, object{"items": []object{{"content_id": "first"}}, "page": object{"next_cursor": "next"}})
		} else {
			respond(w, 200, object{"items": []object{{"content_id": "second"}}})
		}
	}))
	defer upstream.Close()
	b := newBridge(Config{SiloURL: upstream.URL})
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, err := b.catalog(ctx); first <- err }()
	<-started
	cancel()
	if err := <-first; err == nil {
		t.Fatal("cancelled caller succeeded")
	}
	var callers sync.WaitGroup
	for i := 0; i < 8; i++ {
		callers.Add(1)
		go func() {
			defer callers.Done()
			rows, err := b.catalog(context.Background())
			if err != nil || len(rows) != 2 {
				t.Errorf("incomplete snapshot: %v %v", rows, err)
			}
		}()
	}
	close(release)
	callers.Wait()
	if requests.Load() != 2 {
		t.Fatalf("duplicate pagination: %d requests", requests.Load())
	}
}

func TestCatalogRateLimitRetryDoesNotPublishPartialRows(t *testing.T) {
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := requests.Add(1)
		if n == 1 {
			w.Header().Set("Retry-After", "0")
			w.WriteHeader(429)
			return
		}
		if r.URL.Query().Get("cursor") == "" {
			respond(w, 200, object{"items": []object{{"content_id": "first"}}, "page": object{"next_cursor": "next"}})
		} else {
			w.WriteHeader(500)
		}
	}))
	defer upstream.Close()
	b := newBridge(Config{SiloURL: upstream.URL})
	rows, err := b.catalog(context.Background())
	if err == nil || rows != nil {
		t.Fatal("failed scan returned partial rows")
	}
	if len(b.rows) != 0 || requests.Load() != 3 {
		t.Fatalf("retry/publication failure: %d", requests.Load())
	}
	_, _ = b.catalog(context.Background())
	if requests.Load() != 3 {
		t.Fatal("failure cooldown did not prevent rescan")
	}
}

func TestCatalogRetryAfterAndCancellation(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	for _, test := range []struct {
		value string
		want  time.Duration
	}{
		{"2", 2 * time.Second}, {now.Add(5 * time.Second).Format(http.TimeFormat), 5 * time.Second},
		{"bad", time.Second}, {"999999999999", 11 * time.Minute},
	} {
		if got := retryDelay(test.value, now, 0); got != test.want {
			t.Fatalf("Retry-After %q: %v", test.value, got)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if waitContext(ctx, time.Hour) == nil {
		t.Fatal("retry wait ignored cancellation")
	}
}
