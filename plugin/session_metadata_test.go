package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestActiveMetadataIsTargetedCachedAndOptional(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/api/v2/admin/catalog/items" || r.URL.Query().Get("content_ids") != "ep" {
			t.Error("not a targeted lookup")
		}
		respond(w, 200, object{"items": []object{{"content_id": "ep", "type": "episode", "title": "Title", "year": 2024, "series_id": "show", "series_title": "Show", "season_number": 0, "episode_number": 2}}})
	}))
	defer upstream.Close()
	b := newBridge(Config{SiloURL: upstream.URL})
	for i := 0; i < 2; i++ {
		row := object{"content_id": "ep", "media_type": "series"}
		b.enrichSessionMetadata(context.Background(), []object{row})
		if row["media_type"] != "episode" || row["year"] != float64(2024) || row["season_number"] != float64(0) || row["series_name"] != "Show" {
			t.Fatalf("missing metadata: %v", row)
		}
	}
	if calls.Load() != 1 {
		t.Fatal("metadata was not cached")
	}
	upstream.Close()
	row := object{"content_id": "other", "media_type": "series", "season_number": 3, "episode_number": 5}
	b.enrichSessionMetadata(context.Background(), []object{row})
	if row["season_number"] != 3 || row["media_type"] != "series" {
		t.Fatal("failed lookup changed session")
	}
}
