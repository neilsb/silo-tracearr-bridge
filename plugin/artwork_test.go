package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestConcurrentArtworkSharesRateLimitedLookup(t *testing.T) {
	var metadata, images atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/profiles":
			respond(w, 200, object{"items": []object{{"id": "parent", "is_primary": true}}})
		case "/api/v2/catalog/items/movie":
			if metadata.Add(1) == 1 {
				w.Header().Set("Retry-After", "0")
				w.WriteHeader(429)
				return
			}
			respond(w, 200, object{"type": "movie", "poster_url": "/api/v2/artwork/movie"})
		case "/api/v2/artwork/movie":
			images.Add(1)
			w.Header().Set("Content-Type", "image/webp")
			w.Write([]byte("poster"))
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
			w.WriteHeader(500)
		}
	}))
	defer upstream.Close()
	b := newBridge(Config{SiloURL: upstream.URL})
	var group sync.WaitGroup
	for i := 0; i < 12; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			image, err := b.image(context.Background(), "/Items/"+rid("item", "movie")+"/Images/Primary", "")
			if err != nil || string(image.data) != "poster" {
				t.Errorf("poster lookup failed: %v", err)
			}
		}()
	}
	group.Wait()
	if metadata.Load() != 2 || images.Load() != 1 {
		t.Fatalf("duplicate lookups: metadata=%d images=%d", metadata.Load(), images.Load())
	}
}

func TestMetadataPacingAndCancelledWait(t *testing.T) {
	b := newBridge(Config{})
	start := time.Now()
	if err := b.metadataSlot(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := b.metadataSlot(ctx); err == nil {
		t.Fatal("cancelled wait succeeded")
	}
	if err := b.metadataSlot(context.Background()); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 100*time.Millisecond {
		t.Fatal("metadata requests were not paced")
	}
}

func TestLibraryArtworkWithoutPlaybackAndAfterRestart(t *testing.T) {
	var details, images atomic.Int32
	var upstream *httptest.Server
	upstream = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/v2/artwork/") {
			if r.Header.Get("Authorization") != "" {
				t.Error("credential sent to signed artwork")
			}
			images.Add(1)
			w.Header().Set("Content-Type", "image/webp")
			w.Write([]byte("poster"))
			return
		}
		if r.Header.Get("Authorization") != "Bearer upstream" {
			t.Error("missing native credential")
		}
		switch r.URL.Path {
		case "/api/v2/admin/sessions", "/api/v2/admin/sessions/command-capabilities":
			// Monitoring downtime must not prevent catalog artwork lookup.
			w.WriteHeader(503)
		case "/api/v2/profiles":
			respond(w, 200, object{"items": []object{{"id": "parent", "is_primary": true}}})
		case "/api/v2/catalog/items/episode:1":
			details.Add(1)
			if r.Header.Get("X-Profile-Id") != "parent" {
				t.Error("wrong catalog profile")
			}
			respond(w, 200, object{"type": "episode", "poster_url": "/api/v2/artwork/still", "series_poster_url": "/api/v2/artwork/show?signature=test"})
		case "/api/v2/catalog/items/unsafe":
			respond(w, 200, object{"type": "movie", "poster_url": "https://external.invalid/api/v2/artwork/image"})
		default:
			w.WriteHeader(404)
		}
	}))
	defer upstream.Close()
	config := Config{SiloURL: upstream.URL, SiloKey: "upstream", BridgeKey: "tracearr"}
	path := "/Items/" + rid("item", "episode:1") + "/Images/Primary"
	b := newBridge(config)
	request := httptest.NewRequest("GET", path, nil)
	response := httptest.NewRecorder()
	b.ServeHTTP(response, request)
	if response.Code != 401 || details.Load() != 0 {
		t.Fatal("unauthenticated artwork lookup")
	}
	for i := 0; i < 2; i++ {
		image, err := b.image(context.Background(), path, "")
		if err != nil || string(image.data) != "poster" || !strings.Contains(image.url, "/artwork/show?") {
			t.Fatalf("library artwork: %v %v", image, err)
		}
	}
	if details.Load() != 1 || images.Load() != 1 {
		t.Fatal("image cache not used")
	}
	// An expired cache entry must be rediscovered rather than return stale art.
	for _, image := range b.posters {
		image.at = time.Now().Add(-6 * time.Minute)
	}
	if _, err := b.image(context.Background(), path, ""); err != nil {
		t.Fatal(err)
	}
	if details.Load() != 2 {
		t.Fatal("expired entry not refreshed")
	}
	b = newBridge(config)
	if _, err := b.image(context.Background(), path, ""); err != nil {
		t.Fatal("fresh process cannot resolve artwork:", err)
	}
	if details.Load() != 3 {
		t.Fatal("restart did not rediscover poster")
	}
	for _, id := range []string{"missing", "unsafe"} {
		_, err := b.image(context.Background(), "/Items/"+rid("item", id)+"/Images/Primary", "")
		if e, ok := err.(*apiError); !ok || e.code != 404 {
			t.Fatalf("expected missing artwork for %s: %v", id, err)
		}
	}
}
