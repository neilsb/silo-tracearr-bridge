package main

import (
	"context"
	"net/url"
	"time"
)

// Session observations omit year and series identity. Resolve only the active
// content IDs, not the whole library. Failure never hides an active stream.
func (b *Bridge) enrichSessionMetadata(ctx context.Context, rows []object) {
	found := map[string]object{}
	missing := url.Values{"limit": {"200"}}
	seen := map[string]bool{}
	b.mu.Lock()
	if b.sessionMetadata == nil {
		b.sessionMetadata = map[string]cachedRows{}
	}
	for id, cached := range b.sessionMetadata {
		if time.Now().After(cached.until) {
			delete(b.sessionMetadata, id)
		}
	}
	for _, row := range rows {
		id := str(row["content_id"])
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		if cached, ok := b.sessionMetadata[id]; ok {
			if len(cached.rows) > 0 {
				found[id] = cached.rows[0]
			}
		} else if len(missing["content_ids"]) < 200 {
			missing.Add("content_ids", id)
		}
	}
	b.mu.Unlock()
	if len(missing["content_ids"]) > 0 {
		short, cancel := context.WithTimeout(ctx, 2*time.Second)
		response, err := b.native(short, "GET", "/admin/catalog/items", missing, nil, false)
		cancel()
		if err == nil {
			items, _ := response["items"].([]any)
			for _, item := range items {
				metadata := obj(item)
				id := str(metadata["content_id"])
				if seen[id] {
					found[id] = metadata
				}
			}
		}
		b.mu.Lock()
		for _, id := range missing["content_ids"] {
			if len(b.sessionMetadata) >= 256 {
				for key := range b.sessionMetadata {
					delete(b.sessionMetadata, key)
					break
				}
			}
			entry := cachedRows{until: time.Now().Add(15 * time.Second)}
			if item := found[id]; item != nil {
				entry = cachedRows{time.Now().Add(5 * time.Minute), []object{item}}
			}
			b.sessionMetadata[id] = entry
		}
		b.mu.Unlock()
	}
	for _, row := range rows {
		metadata := found[str(row["content_id"])]
		if metadata == nil {
			continue
		}
		for _, key := range []string{"year", "series_id", "season_number", "episode_number"} {
			if row[key] == nil && metadata[key] != nil {
				row[key] = metadata[key]
			}
		}
		if str(row["series_name"]) == "" {
			row["series_name"] = metadata["series_title"]
		}
		if str(metadata["type"]) == "episode" {
			row["media_type"] = "episode"
			if str(row["episode_name"]) == "" {
				row["episode_name"] = metadata["title"]
			}
		}
	}
}
